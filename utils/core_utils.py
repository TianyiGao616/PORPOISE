from argparse import Namespace
from collections import OrderedDict
import os
import pickle 

from lifelines.utils import concordance_index
import numpy as np
from sksurv.metrics import concordance_index_censored

import torch

from datasets.dataset_generic import save_splits
from models.model_genomic import SNN
from models.model_set_mil import MIL_Sum_FC_surv, MIL_Attention_FC_surv, MIL_Cluster_FC_surv
from models.model_coattn import MCAT_Surv
from models.model_porpoise import PorpoiseMMF, PorpoiseAMIL
# PorpoiseMMF_Fast was removed or not implemented
from utils.utils import *
from utils.loss_func import NLLSurvLoss

from utils.coattn_train_utils import *
from utils.cluster_train_utils import *

class EarlyStopping:
    """Early stops the training if validation loss doesn't improve after a given patience."""
    def __init__(self, warmup=5, patience=15, stop_epoch=20, verbose=False):
        """
        Args:
            patience (int): How long to wait after last time validation loss improved.
                            Default: 20
            stop_epoch (int): Earliest epoch possible for stopping
            verbose (bool): If True, prints a message for each validation loss improvement. 
                            Default: False
        """
        self.warmup = warmup
        self.patience = patience
        self.stop_epoch = stop_epoch
        self.verbose = verbose
        self.counter = 0
        self.best_score = None
        self.early_stop = False
        self.val_loss_min = np.Inf

    def __call__(self, epoch, val_loss, model, ckpt_name = 'checkpoint.pt'):

        score = -val_loss

        if epoch < self.warmup:
            pass
        elif self.best_score is None:
            self.best_score = score
            self.save_checkpoint(val_loss, model, ckpt_name)
        elif score < self.best_score:
            self.counter += 1
            print(f'EarlyStopping counter: {self.counter} out of {self.patience}')
            if self.counter >= self.patience and epoch > self.stop_epoch:
                self.early_stop = True
        else:
            self.best_score = score
            self.save_checkpoint(val_loss, model, ckpt_name)
            self.counter = 0

    def save_checkpoint(self, val_loss, model, ckpt_name):
        '''Saves model when validation loss decrease.'''
        if self.verbose:
            print(f'Validation loss decreased ({self.val_loss_min:.6f} --> {val_loss:.6f}).  Saving model ...')
        torch.save(model.state_dict(), ckpt_name)
        self.val_loss_min = val_loss


class Monitor_CIndex:
    """Early stops the training if validation loss doesn't improve after a given patience."""
    def __init__(self):
        """
        Args:
            patience (int): How long to wait after last time validation loss improved.
                            Default: 20
            stop_epoch (int): Earliest epoch possible for stopping
            verbose (bool): If True, prints a message for each validation loss improvement. 
                            Default: False
        """
        self.best_score = None

    def __call__(self, val_cindex, model, ckpt_name:str='checkpoint.pt'):

        score = val_cindex

        if self.best_score is None:
            self.best_score = score
            self.save_checkpoint(model, ckpt_name)
        elif score > self.best_score:
            self.best_score = score
            self.save_checkpoint(model, ckpt_name)
        else:
            pass

    def save_checkpoint(self, model, ckpt_name):
        '''Saves model when validation loss decrease.'''
        torch.save(model.state_dict(), ckpt_name)


def train(datasets: tuple, cur: int, args: Namespace):
    """   
        train for a single fold
    """
    print('\nTraining Fold {}!'.format(cur))
    writer_dir = os.path.join(args.results_dir, str(cur))
    if not os.path.isdir(writer_dir):
        os.mkdir(writer_dir)

    if args.log_data:
        from tensorboardX import SummaryWriter
        writer = SummaryWriter(writer_dir, flush_secs=15)

    else:
        writer = None

    print('\nInit train/val/test splits...', end=' ')
    train_split, val_split = datasets
    save_splits(datasets, ['train', 'val'], os.path.join(args.results_dir, 'splits_{}.csv'.format(cur)))
    print('Done!')
    print("Training on {} samples".format(len(train_split)))
    print("Validating on {} samples".format(len(val_split)))

    print('\nInit loss function...', end=' ')
    if args.task_type == 'survival':
        if args.bag_loss == 'ce_surv':
            loss_fn = CrossEntropySurvLoss(alpha=args.alpha_surv)
        elif args.bag_loss == 'nll_surv':
            loss_fn = NLLSurvLoss(alpha=args.alpha_surv)
        else:
            raise NotImplementedError
    else:
        raise NotImplementedError

    if args.reg_type == 'omic':
        reg_fn = l1_reg_omic
    elif args.reg_type == 'pathomic':
        reg_fn = l1_reg_modules
    else:
        reg_fn = None

    print('Done!')
    
    print('\nInit Model...', end=' ')
    args.fusion = None if args.fusion == 'None' else args.fusion

    if args.model_type == 'porpoise_mmf':
        model_dict = {'omic_input_dim': args.omic_input_dim, 'fusion': args.fusion, 'n_classes': args.n_classes, 
        'gate_path': args.gate_path, 'gate_omic': args.gate_omic, 'scale_dim1': args.scale_dim1, 'scale_dim2': args.scale_dim2, 
        'skip': args.skip, 'dropinput': args.dropinput, 'path_input_dim': args.path_input_dim, 'use_mlp': args.use_mlp,
        }
        model = PorpoiseMMF(**model_dict)
    elif args.model_type == 'porpoise_amil':
        model_dict = {'n_classes': args.n_classes}
        model = PorpoiseAMIL(**model_dict)
    elif args.model_type =='snn':
        model_dict = {'omic_input_dim': args.omic_input_dim, 'model_size_omic': args.model_size_omic, 'n_classes': args.n_classes}
        model = SNN(**model_dict)
    elif args.model_type == 'deepset':
        model_dict = {'omic_input_dim': args.omic_input_dim, 'fusion': args.fusion, 'n_classes': args.n_classes}
        model = MIL_Sum_FC_surv(**model_dict)
    elif args.model_type =='amil':
        model_dict = {'omic_input_dim': args.omic_input_dim, 'fusion': args.fusion, 'n_classes': args.n_classes}
        model = MIL_Attention_FC_surv(**model_dict)
    elif args.model_type == 'mi_fcn':
        model_dict = {'omic_input_dim': args.omic_input_dim, 'fusion': args.fusion, 'num_clusters': 10, 'n_classes': args.n_classes}
        model = MIL_Cluster_FC_surv(**model_dict)
    elif args.model_type == 'mcat':
        model_dict = {'fusion': args.fusion, 'omic_sizes': args.omic_sizes, 'n_classes': args.n_classes}
        model = MCAT_Surv(**model_dict)
    else:
        raise NotImplementedError
    
    if hasattr(model, "relocate"):
        model.relocate()
    else:
        model = model.to(torch.device('cuda'))
    print('Done!')
    print_network(model)

    print('\nInit optimizer ...', end=' ')
    optimizer = get_optim(model, args)
    print('Done!')
    
    print('\nInit Loaders...', end=' ')
    train_loader = get_split_loader(train_split, training=True, testing = args.testing, 
        weighted = args.weighted_sample, mode=args.mode, batch_size=args.batch_size)
    val_loader = get_split_loader(val_split,  testing = args.testing, mode=args.mode, batch_size=args.batch_size)
    print('Done!')

    print('\nSetup EarlyStopping...', end=' ')
    if args.early_stopping:
        early_stopping = EarlyStopping(warmup=0, patience=10, stop_epoch=20, verbose = True)
    else:
        early_stopping = None

    print('\nSetup Validation C-Index Monitor...', end=' ')
    monitor_cindex = Monitor_CIndex()
    print('Done!')

    for epoch in range(args.max_epochs):
        if args.task_type == 'survival':
            if args.mode == 'coattn':
                train_loop_survival_coattn(epoch, model, train_loader, optimizer, args.n_classes, writer, loss_fn, reg_fn, args.lambda_reg, args.gc)
                stop = validate_survival_coattn(cur, epoch, model, val_loader, args.n_classes, early_stopping, monitor_cindex, writer, loss_fn, reg_fn, args.lambda_reg, args.results_dir)
            else:
                train_loop_survival(epoch, model, train_loader, optimizer, args.n_classes, writer, loss_fn, reg_fn, args.lambda_reg, args.gc)
                stop = validate_survival(cur, epoch, model, val_loader, args.n_classes, early_stopping, monitor_cindex, writer, loss_fn, reg_fn, args.lambda_reg, args.results_dir)

    torch.save(model.state_dict(), os.path.join(args.results_dir, "s_{}_checkpoint.pt".format(cur)))
    model.load_state_dict(torch.load(os.path.join(args.results_dir, "s_{}_checkpoint.pt".format(cur))))

    if args.mode == 'coattn':
        results_val_dict, val_cindex = summary_survival_coattn(model, val_loader, args.n_classes)
    else:
        results_val_dict, val_cindex = summary_survival(model, val_loader, args.n_classes)

    print('Val c-Index: {:.4f}'.format(val_cindex))
    writer.close()
    return results_val_dict, val_cindex


def train_loop_survival(epoch, model, loader, optimizer, n_classes,
                        writer=None, loss_fn=None, reg_fn=None,
                        lambda_reg=0., gc=16):

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.train()

    train_loss_surv, train_loss = 0., 0.
    all_risk_scores, all_censorships, all_event_times = [], [], []

    print()

    for batch_idx, (data_WSI, data_omic,
                    y_disc, event_time, censor) in enumerate(loader):

        # 1) move to device
        data_WSI   = data_WSI.to(device)
        data_omic  = data_omic.to(device)
        y_disc     = y_disc.to(device)
        event_time = event_time.to(device)
        censor     = censor.to(device)

        # 2) forward
        out = model(x_path=data_WSI, x_omic=data_omic)

        # ---- unify to hazards, S, Y_hat ----
        if not isinstance(out, tuple):                 # single tensor
            hazards, S, Y_hat = out, None, None
        elif len(out) == 5:                            # (hazards, S, Y_hat, A_raw, dict)
            hazards, S, Y_hat = out[:3]
        else:                                          # len==3 (h_path, h_omic, h_mm)
            _, _, h_mm = out
            hazards, S, Y_hat = h_mm, None, None

        # 3) loss
        loss = loss_fn(h=hazards, y=y_disc, t=event_time, c=censor)
        loss_value = loss.item()

        # 4) L1 reg (optional)
        loss_reg = 0 if reg_fn is None else reg_fn(model) * lambda_reg

        # 5) risk score for c-index
        if isinstance(loss_fn, NLLSurvLoss):
            survival = torch.cumprod(1 - torch.sigmoid(hazards), dim=1)
            risk = -torch.sum(survival, dim=1).detach().cpu().numpy()
        else:
            risk = hazards.detach().cpu().numpy().squeeze()

        # bookkeeping
        train_loss_surv += loss_value
        train_loss      += loss_value + loss_reg
        all_risk_scores.append(risk)
        all_censorships.append(censor.detach().cpu().numpy())
        all_event_times.append(event_time.detach().cpu().numpy())

        # backward
        (loss + loss_reg).div_(gc).backward()
        if (batch_idx + 1) % gc == 0:
            optimizer.step()
            optimizer.zero_grad()

    # --- epoch-level metrics ---
    train_loss_surv /= len(loader)
    train_loss      /= len(loader)

    all_risk_scores = np.concatenate(all_risk_scores)
    all_censorships = np.concatenate(all_censorships)
    all_event_times = np.concatenate(all_event_times)

    c_index = concordance_index_censored(
        (1-all_censorships).astype(bool),
        all_event_times,
        all_risk_scores
    )[0]

    print(f'Epoch {epoch}: loss_surv={train_loss_surv:.4f}, '
          f'loss={train_loss:.4f}, c_index={c_index:.4f}')

    if writer:
        writer.add_scalar('train/loss_surv', train_loss_surv, epoch)
        writer.add_scalar('train/loss',      train_loss,      epoch)
        writer.add_scalar('train/c_index',   c_index,         epoch)


def validate_survival(cur, epoch, model, loader, n_classes,
                      early_stopping=None, monitor_cindex=None, writer=None,
                      loss_fn=None, reg_fn=None, lambda_reg=0.,
                      results_dir=None):

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.eval()

    val_loss_surv, val_loss = 0., 0.
    all_risk_scores = np.zeros(len(loader))
    all_censorships = np.zeros(len(loader))
    all_event_times = np.zeros(len(loader))

    for batch_idx, (data_WSI, data_omic,
                    y_disc, event_time, censor) in enumerate(loader):

        # ─── 1. 送到设备 ──────────────────────────
        data_WSI   = data_WSI.to(device)
        data_omic  = data_omic.to(device)
        y_disc     = y_disc.to(device)
        event_time = event_time.to(device)
        censor     = censor.to(device)

        # ─── 2. 前向 (无梯度) ─────────────────────
        with torch.no_grad():
            out = model(x_path=data_WSI, x_omic=data_omic)

        # ─── 3. 统一拆包 → hazards ────────────────
        if not isinstance(out, tuple):                 # 单张量
            hazards = out
        elif len(out) == 5:                            # (hazards, S, Y_hat, A_raw, dict)
            hazards = out[0]
        else:                                          # len==3 → (h_path, h_omic, h_mm)
            hazards = out[2]                           # 取融合分支 h_mm

        # ─── 4. 损失 ─────────────────────────────
        loss = loss_fn(h=hazards, y=y_disc, t=event_time, c=censor)
        loss_value = loss.item()

        # ─── 5. 正则 ─────────────────────────────
        loss_reg = 0 if reg_fn is None else reg_fn(model) * lambda_reg

        # ─── 6. 风险分数 (c-index 用) ──────────────
        if isinstance(loss_fn, NLLSurvLoss):
            surv = torch.cumprod(1 - torch.sigmoid(hazards), dim=1)
            risk = -torch.sum(surv, dim=1).detach().cpu().numpy()
        else:
            risk = hazards.detach().cpu().numpy().squeeze()

        all_risk_scores[batch_idx] = risk
        all_censorships[batch_idx] = censor.detach().cpu().numpy()
        all_event_times[batch_idx] = event_time.detach().cpu().numpy()

        val_loss_surv += loss_value
        val_loss      += loss_value + loss_reg

    # ─── 7. 计算 epoch 级指标 ─────────────────────
    val_loss_surv /= len(loader)
    val_loss      /= len(loader)

    c_index = concordance_index_censored(
        (1 - all_censorships).astype(bool),
        all_event_times,
        all_risk_scores,
        tied_tol=1e-8
    )[0]

    if writer:
        writer.add_scalar('val/loss_surv', val_loss_surv, epoch)
        writer.add_scalar('val/loss',      val_loss,      epoch)
        writer.add_scalar('val/c-index',   c_index,       epoch)

    # ─── 8. Early-Stopping & c-index 监控 ─────────
    if early_stopping:
        assert results_dir
        early_stopping(epoch, val_loss_surv, model,
                       ckpt_name=os.path.join(
                           results_dir, f"s_{cur}_minloss_checkpoint.pt"))
        if early_stopping.early_stop:
            print("Early stopping")
            return True

    if monitor_cindex:
        monitor_cindex(c_index, model,
                       ckpt_name=os.path.join(
                           results_dir, f"s_{cur}_bestcidx_checkpoint.pt"))

    return False


def summary_survival(model, loader, n_classes):
    device=torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.eval()
    test_loss = 0.

    all_risk_scores = np.zeros((len(loader)))
    all_censorships = np.zeros((len(loader)))
    all_event_times = np.zeros((len(loader)))

    slide_ids = loader.dataset.slide_data['slide_id']
    patient_results = {}

    for batch_idx, (data_WSI, data_omic, y_disc, event_time, censor) in enumerate(loader):
        data_WSI, data_omic = data_WSI.to(device), data_omic.to(device)
        slide_id = slide_ids.iloc[batch_idx]

        with torch.no_grad():
            h = model(x_path=data_WSI, x_omic=data_omic)
        
        if isinstance(h, tuple):
            h = h[2]

        if h.shape[1] > 1:
            hazards = torch.sigmoid(h)
            survival = torch.cumprod(1 - hazards, dim=1)
            risk = -torch.sum(survival, dim=1).detach().cpu().numpy()
        else:
            risk = h.detach().cpu().numpy().squeeze()

        event_time = event_time.item()
        censor = censor.item()
        all_risk_scores[batch_idx] = risk
        all_censorships[batch_idx] = censor
        all_event_times[batch_idx] = event_time
        patient_results.update({slide_id: {'slide_id': np.array(slide_id), 'risk': risk, 'disc_label': y_disc.item(), 'survival': event_time, 'censorship': censor}})

    c_index = concordance_index_censored((1-all_censorships).astype(bool), all_event_times, all_risk_scores, tied_tol=1e-08)[0]
    return patient_results, c_index