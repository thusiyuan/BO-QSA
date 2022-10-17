import math
import torch
import pytorch_lightning as pl
from torch import optim
from torchvision import utils as vutils
from torch.nn import functional as F

import os
import sys
root_path = os.path.abspath(__file__)
root_path = '/'.join(root_path.split('/')[:-2])
sys.path.append(root_path)

from methods.utils import to_rgb_from_tensor, average_ari, iou_and_dice, average_segcover


class SlotAttentionMethod(pl.LightningModule):
    def __init__(self, model, datamodule: pl.LightningDataModule, args):
        super().__init__()
        self.model = model
        self.datamodule = datamodule
        self.args = args
        self.val_iter = iter(self.datamodule.val_dataloader())
        self.sample_num = 0
        self.empty_cache = True
        self.tau = 1
        self.sigma = 0
        self.evaluate = args.evaluate

    def forward(self, input, **kwargs):
        return self.model(input, self.tau, self.hard, **kwargs)

    def training_step(self, batch, batch_idx):
        batch_img = batch['image']
        self.tau = self.cosine_anneal(self.global_step, self.args.tau_steps, start_value=self.args.tau_start, final_value=self.args.tau_final)
        self.sigma = self.cosine_anneal(self.global_step, self.args.sigma_steps, start_value=self.args.sigma_start, final_value=self.args.sigma_final)
        loss_dict = self.model.forward(batch_img, tau=self.tau, sigma=self.sigma)['loss']
        loss = 0
        logs = {'tau': self.tau, 'sigma': self.sigma}
        for k, v in loss_dict.items():
            self.log_dict({k: v})
            loss += v
            logs[k] = v.item()
        self.log_dict(logs, sync_dist=True)
        return {'loss': loss}

    def sample_images(self):
        if self.sample_num % (len(self.val_iter) - 1) == 0:
            self.val_iter = iter(self.datamodule.val_dataloader())
        self.sample_num += 1

        batch = next(self.val_iter)
        batch_img = batch['image'][:self.args.n_samples]
        mask_gt = batch['mask'][:self.args.n_samples]
        if self.args.gpus > 0:
            batch_img = batch_img.to(self.device)

        out = self.model.forward(batch_img, tau=0.1, test=True, sigma=0)
        recon, m = out['recon'], out['attns']
        pred_image = out['pred_image']

        if self.args.use_rescale:
            out = to_rgb_from_tensor(
                torch.cat(
                    [
                        batch_img.unsqueeze(1),  # original images
                        recon.unsqueeze(1),  # reconstructions
                        pred_image.unsqueeze(1),  # predictions
                    ],
                    dim=1,
                )
            ).cpu()
        else:
            out = torch.cat(
                    [
                        batch_img.unsqueeze(1),  # original images
                        recon.unsqueeze(1),  # reconstructions
                        pred_image.unsqueeze(1),  # predictions
                    ],
                    dim=1,
                ).cpu()
        # visualize the masks
        m_i = (m * batch_img.unsqueeze(1) + 1 - m).cpu() 
        m = (1 - m).expand(-1, -1, 3, -1, -1).cpu()
        out = torch.cat([out, m, m_i], dim=1) # add masks
        out = torch.cat([out, mask_gt.unsqueeze(1).expand(-1, -1, 3, -1, -1).cpu()], dim=1) # add gt masks

        batch_size, C, H, W = batch_img.shape
        images = vutils.make_grid(
            out.reshape(out.shape[0] * out.shape[1], C, H, W), normalize=False, nrow=out.shape[1],
            padding=3, pad_value=0,
        )

        return images

    def validation_step(self, batch, batch_idx):
        if self.empty_cache:
            torch.cuda.empty_cache()
            self.empty_cache = False

        batch_img = batch['image']
        masks_gt = batch['mask']
        out = self.model.forward(batch_img, tau=self.tau, test=True, sigma=self.sigma)
        loss_dict = out['loss']
        masks = out['attns'] 
        output = {}
        for k, v in loss_dict.items():
            self.log_dict({k: v})
            output[k] = v

        if self.evaluate == 'ari':
            m = masks.detach().argmax(dim=1)
            ari, _ = average_ari(m, masks_gt)
            ari_fg, _ = average_ari(m, masks_gt, True)
            msc_fg, _ = average_segcover(masks_gt, m, True)
            output['ARI'] = ari.to(self.device)
            output['ARI_FG'] = ari_fg.to(self.device)
            output['MSC_FG'] = msc_fg.to(self.device)
        elif self.evaluate == 'iou':
            K = self.args.num_slots
            m = F.one_hot(masks.argmax(dim=1), K).permute(0, 4, 1, 2, 3)
            iou, dice = iou_and_dice(m[:, 0], masks_gt)
            for i in range(1, K):
                iou1, dice1 = iou_and_dice(m[:, i], masks_gt)
                iou = torch.max(iou, iou1)
                dice = torch.max(dice, dice1)
            output['IoU'] = iou.mean()
            output['Dice'] = dice.mean()
        return output

    def validation_epoch_end(self, outputs):
        self.empty_cache = True
        keys = outputs[0].keys()
        logs = {}
        for k in keys:
            v = torch.stack([x[k] for x in outputs]).mean()
            logs['avg_' + k] = v
        self.log_dict(logs, sync_dist=True)
        print("; ".join([f"{k}: {v.item():.6f}" for k, v in logs.items()]))
    
    def test_step(self, batch, batch_idx):
        if self.empty_cache:
            torch.cuda.empty_cache()
            self.empty_cache = False

        batch_img = batch['image']
        masks_gt = batch['mask']
        out = self.model.forward(batch_img, tau=0.1, test=True, sigma=0)
        loss_dict = out['loss']
        masks = out['attns'] 
        output = {}
        for k, v in loss_dict.items():
            self.log_dict({k: v})
            output[k] = v

        if self.evaluate == 'ari':
            m = masks.detach().argmax(dim=1)
            ari, _ = average_ari(m, masks_gt)
            ari_fg, _ = average_ari(m, masks_gt, True)
            msc_fg, _ = average_segcover(masks_gt, m, True)
            output['ARI'] = ari.to(self.device)
            output['ARI_FG'] = ari_fg.to(self.device)
            output['MSC_FG'] = msc_fg.to(self.device)
        elif self.evaluate == 'iou':
            K = self.args.num_slots
            m = F.one_hot(masks.argmax(dim=1), K).permute(0, 4, 1, 2, 3)
            iou, dice = iou_and_dice(m[:, 0], masks_gt)
            for i in range(1, K):
                iou1, dice1 = iou_and_dice(m[:, i], masks_gt)
                iou = torch.max(iou, iou1)
                dice = torch.max(dice, dice1)
            output['IoU'] = iou.mean()
            output['Dice'] = dice.mean()
        return output

    def test_epoch_end(self, outputs):
        self.empty_cache = True
        keys = outputs[0].keys()
        logs = {}
        for k in keys:
            v = torch.stack([x[k] for x in outputs]).mean()
            logs['avg_' + k] = v
        self.log_dict(logs)
        print("; ".join([f"{k}: {v.item():.6f}" for k, v in logs.items()]))


    def configure_optimizers(self):
        params = [
        {'params': (x[1] for x in self.model.named_parameters() if 'dvae' in x[0]), 'lr': self.args.lr_dvae},
        {'params': (x[1] for x in self.model.named_parameters() if 'dvae' not in x[0]), 'lr': self.args.lr_main},
        ]
        optimizer = optim.Adam(params)
        
        warmup_steps = self.args.warmup_steps
        decay_steps = self.args.decay_steps

        def lr_scheduler_dave(step: int):
            factor = 0.5 ** (step / decay_steps)
            return factor

        def lr_scheduler_main(step: int):
            if step < warmup_steps:
                factor = step / warmup_steps
            else:
                factor = 1
            factor *= 0.5 ** (step / decay_steps)
            return factor

        scheduler = optim.lr_scheduler.LambdaLR(optimizer=optimizer, lr_lambda=[lr_scheduler_dave, lr_scheduler_main])

        return (
            [optimizer],
            [{"scheduler": scheduler, "interval": "step",}],
        )

    def cosine_anneal(self, step, final_step, start_step=0, start_value=1.0, final_value=0.1):
    
        assert start_value >= final_value
        assert start_step <= final_step
        
        if step < start_step:
            value = start_value
        elif step >= final_step:
            value = final_value
        else:
            a = 0.5 * (start_value - final_value)
            b = 0.5 * (start_value + final_value)
            progress = (step - start_step) / (final_step - start_step)
            value = a * math.cos(math.pi * progress) + b
        return value
