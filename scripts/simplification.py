import os
import argparse
import random
import numpy as np

import torch
from torch.utils.data import DataLoader, Dataset
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM, AutoConfig
from transformers.optimization import get_linear_schedule_with_warmup, Adafactor
import nlp
from rouge_score import rouge_scorer
import sacrebleu

import pytorch_lightning as pl
from pytorch_lightning.logging import TestTubeLogger
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.overrides.data_parallel import LightningDistributedDataParallel
from pytorch_lightning.callbacks.early_stopping import EarlyStopping


from longformer import LongformerEncoderDecoderForConditionalGeneration, LongformerEncoderDecoderConfig
from longformer.sliding_chunks import pad_to_window_size

import logging
from transformers import MBartTokenizer
from transformers import MBartForConditionalGeneration
from transformers.modeling_bart import shift_tokens_right
from longformer.longformer_encoder_decoder import LongformerSelfAttentionForBart
from longformer.longformer_encoder_decoder_mbart import MLongformerEncoderDecoderForConditionalGeneration, MLongformerEncoderDecoderConfig
import datasets

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def label_smoothed_nll_loss(lprobs, target, epsilon, ignore_index=-100):
    """From fairseq"""
    if target.dim() == lprobs.dim() - 1:
        target = target.unsqueeze(-1)
    nll_loss = -lprobs.gather(dim=-1, index=target)
    smooth_loss = -lprobs.sum(dim=-1, keepdim=True)
    if ignore_index is not None:
        pad_mask = target.eq(ignore_index)
        nll_loss.masked_fill_(pad_mask, 0.0)
        smooth_loss.masked_fill_(pad_mask, 0.0)
        count = (~pad_mask).sum()
    else:
        nll_loss = nll_loss.squeeze(-1)
        smooth_loss = smooth_loss.squeeze(-1)
        count = nll_loss.numel()

    nll_loss = nll_loss.sum() / count
    smooth_loss = smooth_loss.sum() / count
    eps_i = epsilon / lprobs.size(-1)
    loss = (1.0 - epsilon) * nll_loss + eps_i * smooth_loss
    return loss, nll_loss


class SimplificationDataset(Dataset):
    def __init__(self, inputs, labels, name, tokenizer, max_input_len, max_output_len, src_lang, tgt_lang):
        self.inputs = inputs
        self.labels = labels
        self.name = name # train, val, test
        self.tokenizer = tokenizer
        self.max_input_len = max_input_len
        self.max_output_len = max_output_len
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang

    def __len__(self):
        return len(self.inputs)

    def __getitem__(self, idx):
        source = self.inputs[idx]['text']
        target = self.labels[idx]['text'] 
        sample = self.tokenizer.prepare_seq2seq_batch(src_texts=[source], src_lang=self.src_lang, tgt_texts=[target], tgt_lang=self.tgt_lang , max_length=self.max_input_len, max_target_length=self.max_output_len, truncation=True, padding=False, return_tensors="pt") # TODO move this to _get_dataloader, preprocess everything at once? 
        ## changed set_tgt_lang_special_tokens in transformers.tokenization_mbart to prefix the target id instead of adding it at the end. See https://arxiv.org/pdf/2001.08210.pdf, "A language id symbol <LID> is usedas the initial token to predict the sentence." 

        input_ids = sample['input_ids'].squeeze()
        output_ids = sample['labels'].squeeze()
      
        return input_ids, output_ids

    @staticmethod
    def collate_fn(batch):
        
        pad_token_id = 1
    
        input_ids, output_ids = list(zip(*batch))
        input_ids = torch.nn.utils.rnn.pad_sequence(input_ids, batch_first=True, padding_value=pad_token_id)
        output_ids = torch.nn.utils.rnn.pad_sequence(output_ids, batch_first=True, padding_value=pad_token_id)
        return input_ids, output_ids


class Simplifier(pl.LightningModule):

    def __init__(self, params):
        super().__init__()
        self.args = params
        self.hparams = params
        self.tokenizer = MBartTokenizer.from_pretrained(self.args.tokenizer, use_fast=True)
        self.src_lang = "de_DE"
        self.tgt_lang = "de_DE" ## TODO makes this more generic

        config = MLongformerEncoderDecoderConfig.from_pretrained(self.args.model_path)
        config.attention_dropout = self.args.attention_dropout
        config.dropout = self.args.dropout
        config.activation_dropout = self.args.activation_dropout
        config.gradient_checkpointing = self.args.grad_ckpt
        config.attention_mode = self.args.attention_mode
        config.attention_window = [self.args.attention_window] * config.encoder_layers
        self.model = MLongformerEncoderDecoderForConditionalGeneration.from_pretrained(self.args.model_path, config=config)
        self.train_dataloader_object = self.val_dataloader_object = self.test_dataloader_object = None
        self.current_checkpoint =0

    def _prepare_input(self, input_ids):
        attention_mask = torch.ones(input_ids.shape, dtype=torch.long, device=input_ids.device)
        attention_mask[input_ids == self.tokenizer.pad_token_id] = 0
        if isinstance(self.model, MLongformerEncoderDecoderForConditionalGeneration):
            attention_mask[:, -1] = 2  # put global attention on last token (target language tag)
            if self.args.attention_mode == 'sliding_chunks':
                half_padding_mod = self.model.config.attention_window[0]
            elif self.args.attention_mode == 'sliding_chunks_no_overlap':
                half_padding_mod = self.model.config.attention_window[0] / 2
            else:
                raise NotImplementedError
            input_ids, attention_mask = pad_to_window_size(  # ideally, should be moved inside the LongformerModel
                input_ids, attention_mask, half_padding_mod, self.tokenizer.pad_token_id)
        return input_ids, attention_mask

    def forward(self, input_ids, output_ids):
        input_ids, attention_mask = self._prepare_input(input_ids)
        decoder_input_ids = output_ids[:, :-1]
        decoder_attention_mask = (decoder_input_ids != self.tokenizer.pad_token_id)
        labels = output_ids[:, 1:].clone()

        outputs = self.model(
                input_ids,
                attention_mask=attention_mask,
                decoder_input_ids=decoder_input_ids,
                decoder_attention_mask=decoder_attention_mask,
                use_cache=False,)
        lm_logits = outputs[0]
        if self.args.label_smoothing == 0:
            # Same behavior as modeling_bart.py, besides ignoring pad_token_id
            ce_loss_fct = torch.nn.CrossEntropyLoss(ignore_index=self.tokenizer.pad_token_id)
            assert lm_logits.shape[-1] == self.model.config.vocab_size
            loss = ce_loss_fct(lm_logits.view(-1, lm_logits.shape[-1]), labels.view(-1))
        else:
            lprobs = torch.nn.functional.log_softmax(lm_logits, dim=-1)
            loss, nll_loss = label_smoothed_nll_loss(
                lprobs, labels, self.args.label_smoothing, ignore_index=self.tokenizer.pad_token_id
            )
        return [loss]

    def training_step(self, batch, batch_nb):
        output = self.forward(*batch)
        loss = output[0]
        lr = loss.new_zeros(1) + self.trainer.optimizers[0].param_groups[0]['lr']
        tensorboard_logs = {'train_loss': loss, 'lr': lr,
                            'input_size': batch[0].numel(),
                            'output_size': batch[1].numel(),
                            'mem': torch.cuda.memory_allocated(loss.device) / 1024 ** 3 if torch.cuda.is_available() else 0}
        return {'loss': loss, 'log': tensorboard_logs}

    def validation_step(self, batch, batch_nb):
        for p in self.model.parameters():
            p.requires_grad = False

        outputs = self.forward(*batch)
        vloss = outputs[0]
        input_ids, output_ids = batch
        input_ids, attention_mask = self._prepare_input(input_ids)
        generated_ids = self.model.generate(input_ids=input_ids, attention_mask=attention_mask,
                                            use_cache=True, max_length=self.args.max_output_len,
                                            num_beams=self.args.beam_size, pad_token_id=self.tokenizer.pad_token_id, decoder_start_token_id=self.tokenizer.lang_code_to_id[self.tgt_lang]) # mBART source: X eso src_tag -> target: trg_tag X eos (no bos)

        generated_str = self.tokenizer.batch_decode(generated_ids.tolist(), skip_special_tokens=True)
        
        gold_str = self.tokenizer.batch_decode(output_ids.tolist(), skip_special_tokens=True, clean_up_tokenization_spaces=True)
        scorer = rouge_scorer.RougeScorer(rouge_types=['rouge1', 'rouge2', 'rougeL', 'rougeLsum'], use_stemmer=False)
        rouge1 = rouge2 = rougel = rougelsum = 0.0
        for ref, pred in zip(gold_str, generated_str):
            score = scorer.score(ref, pred)
            rouge1 += score['rouge1'].fmeasure
            rouge2 += score['rouge2'].fmeasure
            rougel += score['rougeL'].fmeasure
            rougelsum += score['rougeLsum'].fmeasure
        rouge1 /= len(generated_str)
        rouge2 /= len(generated_str)
        rougel /= len(generated_str)
        rougelsum /= len(generated_str)
        bleu = sacrebleu.corpus_bleu(generated_str, [gold_str])
        
        outfile = self.args.save_dir + "/" + args.save_prefix + "/_val_out_checkpoint_" + str(self.current_checkpoint)
        if self.args.test:
            outfile = self.args.decoded
        with open(outfile, 'a') as f:
            for sample in generated_str:
                f.write(sample + "\n")
        return {'vloss': vloss,
                'rouge1': vloss.new_zeros(1) + rouge1,
                'rouge2': vloss.new_zeros(1) + rouge2,
                'rougeL': vloss.new_zeros(1) + rougel,
                'rougeLsum': vloss.new_zeros(1) + rougelsum, 
                'bleu' : vloss.new_zeros(1) + bleu.score,
                'decoded' : generated_str}

    def validation_epoch_end(self, outputs):
        for p in self.model.parameters():
            p.requires_grad = True

        names = ['vloss', 'rouge1', 'rouge2', 'rougeL', 'rougeLsum', 'bleu']
        metrics = []
        for name in names:
            metric = torch.stack([x[name] for x in outputs]).mean()
            if self.trainer.use_ddp:
                torch.distributed.all_reduce(metric, op=torch.distributed.ReduceOp.SUM)
                metric /= self.trainer.world_size
            metrics.append(metric)
        logs = dict(zip(*[names, metrics]))
        print("Evaluation on checkpoint [{}] ".format(self.current_checkpoint))
        print(logs)
        
        self.current_checkpoint +=1
        return {'val_loss': logs['vloss'], 'log': logs, 'progress_bar': logs}

    def test_step(self, batch, batch_nb):
        return self.validation_step(batch, batch_nb)

    def test_epoch_end(self, outputs):
        result = self.validation_epoch_end(outputs)
        print(result)
        
    def configure_optimizers(self):
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.args.lr)
        self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer=self.optimizer, mode=self.lr_mode, factor=self.args.lr_reduce_factor, patience=self.args.lr_reduce_patience)
        return {
       'optimizer': self.optimizer,
       'lr_scheduler': self.scheduler,
       'monitor': self.args.early_stopping_metric ## TODO change to args.early_stopping_metric
        }

    def _get_dataloader(self, current_dataloader, split_name, is_train):
        if current_dataloader is not None:
            return current_dataloader
       
        dataset = SimplificationDataset(inputs=self.datasets[split_name + "_source"], labels=self.datasets[split_name + "_target"] , name=split_name, tokenizer=self.tokenizer,
                                       max_input_len=self.args.max_input_len, max_output_len=self.args.max_output_len, src_lang=self.src_lang, tgt_lang=self.tgt_lang)
      
        sampler = torch.utils.data.distributed.DistributedSampler(dataset, shuffle=is_train) if self.trainer.use_ddp else None

        return DataLoader(dataset, batch_size=self.args.batch_size, shuffle=(sampler is None),
                          num_workers=self.args.num_workers, sampler=sampler,
                          collate_fn=SimplificationDataset.collate_fn)

    @pl.data_loader
    def train_dataloader(self):
        self.train_dataloader_object = self._get_dataloader(self.train_dataloader_object, 'train', is_train=True)
        return self.train_dataloader_object

    @pl.data_loader
    def val_dataloader(self):
        self.val_dataloader_object = self._get_dataloader(self.val_dataloader_object, 'val', is_train=False)
        return self.val_dataloader_object

    @pl.data_loader
    def test_dataloader(self):
        self.test_dataloader_object = self._get_dataloader(self.test_dataloader_object, 'test', is_train=False)
        return self.test_dataloader_object

    def configure_ddp(self, model, device_ids):
        model = LightningDistributedDataParallel(
            model,
            device_ids=device_ids,
            find_unused_parameters=False
        )
        return model


    @staticmethod
    def add_model_specific_args(parser, root_dir):
        parser.add_argument("--model_path", type=str, help="Path to the checkpoint directory or model name")
        parser.add_argument("--tokenizer", type=str, help="Path to the tokenizer directory.")
        parser.add_argument("--save_dir", type=str, default='simplification', help="Directory to save models.")
        parser.add_argument("--save_prefix", type=str, default='test', help="subfolder in save_dir for this model")
        parser.add_argument("--resume_ckpt", type=str, help="Path of a checkpoint to resume from")
        parser.add_argument("--from_pretrained", type=str, default=None,  help="Path to a checkpoint to load model weights but not training state")
        
        #data
        parser.add_argument("--train_source", type=str, default=None,  help="Path to the source train file.")
        parser.add_argument("--train_target", type=str, default=None, help="Path to the target train file.")
        parser.add_argument("--val_source", type=str, default=None, help="Path to the source validation file.")
        parser.add_argument("--val_target", type=str, default=None, help="Path to the target validation file.")
        parser.add_argument("--test_source", type=str, default=None, help="Path to the source test file (to evaluate after training is finished).")
        parser.add_argument("--test_target", type=str, default=None, help="Path to the target test file (to evaluate after training is finished).")
        parser.add_argument("--max_output_len", type=int, default=256, help="maximum num of wordpieces/summary. Used for training and testing")
        parser.add_argument("--max_input_len", type=int, default=512, help="maximum num of wordpieces/summary. Used for training and testing")
        
        parser.add_argument("--batch_size", type=int, default=16, help="Batch size")
        parser.add_argument("--num_workers", type=int, default=0, help="Number of data loader workers")
        parser.add_argument("--grad_accum", type=int, default=1, help="Number of gradient accumulation steps.")
        parser.add_argument("--gpus", type=int, default=-1, help="Number of gpus. 0 for CPU")
        parser.add_argument("--seed", type=int, default=1234, help="Seed")
        
        ## model params:
        parser.add_argument("--attention_dropout", type=float, default=0.1, help="attention dropout")
        parser.add_argument("--dropout", type=float, default=0.1, help="dropout")
        parser.add_argument("--activation_dropout", type=float, default=0.0, help="activation_dropout")
        parser.add_argument("--attention_mode", type=str, default='sliding_chunks', help="Longformer attention mode")
        parser.add_argument("--attention_window", type=int, default=512, help="Attention window")
        parser.add_argument("--label_smoothing", type=float, default=0.0, required=False)
        
        # Optimization params:
        #parser.add_argument("--warmup", type=int, default=1000, help="Number of warmup steps")
        parser.add_argument("--lr", type=float, default=0.00003, help="Initial learning rate")
        parser.add_argument("--val_every", type=float, default=1.0, help="Number of training steps between validations")
        parser.add_argument("--val_percent_check", default=1.00, type=float, help='Percent of validation data used')
        parser.add_argument("--max-epochs", type=int, default=100000, help="Maximum number of epochs (will stop training even if patience for early stopping has not been reached).")
        parser.add_argument("--early_stopping_metric", type=str, default='rougeL', help="Metric to be used for early stopping: vloss, rouge1, rouge2, rougeL, rougeLsum, bleu")
        parser.add_argument("--patience", type=int, default=10, help="Patience for early stopping.")
        parser.add_argument("--lr_reduce_patience", type=int, default=8, help="Patience for LR reduction in Plateau scheduler.")
        parser.add_argument("--lr_reduce_factor", type=float, default=0.5, help="Learning rate reduce factor for Plateau scheduler.")
        parser.add_argument("--disable_checkpointing", action='store_true', help="No logging or checkpointing")
        parser.add_argument('--grad_ckpt', action='store_true', help='Enable gradient checkpointing to save memory')
        
        ## inference params
        parser.add_argument("--test", action='store_true', help="Test only, no training")
        parser.add_argument("--decoded", type=str, default='decoded.out', help="Output file to write decoded sequence to.")
        parser.add_argument("--beam_size", type=int, default=4, help="Beam size for inference when testing/validating. Default: 4.")
        parser.add_argument("--test_percent_check", default=1.00, type=float, help='Percent of test data used')
        
        #logging params
        parser.add_argument("--no_progress_bar", action='store_true', help="no progress bar. Good for printing")
        parser.add_argument("--fp32", action='store_true', help="default is fp16. Use --fp32 to switch to fp32")
        parser.add_argument("--debug", action='store_true', help="debug run")
        parser.add_argument("--print_params", action='store_true', help="Print parameter names and shapes.")
        
        return parser


def main(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    if args.from_pretrained is not None:
        model = Simplifier.load_from_checkpoint(args.from_pretrained, args)
    else:
        model = Simplifier(args)
    
    if args.print_params:
        for name, param in model.named_parameters():
            if param.requires_grad:
                print(name + ":" + str(param.data.shape))
        exit(0)
    
    if args.test:
        model.datasets = datasets.load_dataset('text', data_files={'test_source': args.test_source, 'test_target': args.test_target })
    else:
        model.datasets = datasets.load_dataset('text', data_files={'train_source': args.train_source, 'train_target': args.train_target, 'val_source': args.val_source, 'val_target': args.val_target, 'test_source': args.test_source, 'test_target': args.test_target })

    logger = TestTubeLogger(
        save_dir=args.save_dir,
        name=args.save_prefix,
        version=0  # always use version=0
    )

    print(args)

    model.lr_mode='max'
    if args.early_stopping_metric == 'val_loss':
        model.lr_mode='min'
    early_stop_callback = EarlyStopping(monitor=args.early_stopping_metric, min_delta=0.00, patience=args.patience, verbose=True, mode=model.lr_mode) # metrics: val_loss, bleu, rougeL
    
    checkpoint_callback = ModelCheckpoint(
    filepath=os.path.join(args.save_dir, args.save_prefix, "checkpoints"),
                        save_top_k=5,
                        verbose=True,
                        monitor=args.early_stopping_metric,
                        mode=model.lr_mode,
                        period=-1,
                        prefix='')

    trainer = pl.Trainer(gpus=args.gpus, distributed_backend='ddp' if torch.cuda.is_available() else None,
                         track_grad_norm=-1,
                         max_epochs=args.max_epochs if not args.debug else 100,
                         max_steps=None if not args.debug else 1,
                         replace_sampler_ddp=False,
                         accumulate_grad_batches=args.grad_accum,
                         val_check_interval=args.val_every if not args.debug else 1,
                         num_sanity_val_steps=2 if not (args.debug or args.test) else 0,
                         check_val_every_n_epoch=1 if not (args.debug or args.test) else 1,
                         val_percent_check=args.val_percent_check,
                         test_percent_check=args.test_percent_check,
                         logger=logger,
                         checkpoint_callback=checkpoint_callback if not args.disable_checkpointing else False,
                         show_progress_bar=not args.no_progress_bar,
                         use_amp=not args.fp32, amp_level='O2',
                         resume_from_checkpoint=args.resume_ckpt,
                         callbacks=[early_stop_callback]
                         )
    if not args.test:
        trainer.fit(model)
    trainer.test(model)


if __name__ == "__main__":
    main_arg_parser = argparse.ArgumentParser(description="summarization")
    parser = Simplifier.add_model_specific_args(main_arg_parser, os.getcwd())
    args = parser.parse_args()
    main(args)

