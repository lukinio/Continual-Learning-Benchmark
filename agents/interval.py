import os
from pathlib import Path
from types import MethodType
from typing import Union

import models
import torch
import torch.nn as nn
import torch.optim as opt
import wandb
from torch.utils.tensorboard import SummaryWriter
from utils.metric import AverageMeter, Timer, accuracy
from utils.wandb import is_wandb_on

from interval.hyperparam_scheduler import LinearScheduler
from interval.layers import IntervalLayerWithParameters, split_activation


def free_exp_name(runs_dir: Path, run_name: str):
    try_index = 0
    while (runs_dir / f'{run_name}#{try_index}').exists():
        try_index += 1
    return Path(runs_dir / f'{run_name}#{try_index}')


class IntervalNet(nn.Module):
    def __init__(self, agent_config):
        """
        :param agent_config (dict): lr=float,momentum=float,weight_decay=float,
                                    schedule=[int],  # The last number in the list is the end of epoch
                                    model_type=str,model_name=str,out_dim={task:dim},model_weights=str
                                    force_single_head=bool
                                    print_freq=int
                                    gpuid=[int]
        """
        super(IntervalNet, self).__init__()
        # Use a void function to replace the print
        self.log = print
        self.config = agent_config
        # If out_dim is a dict, there is a list of tasks. The model will have a head for each task.
        # A convenience flag to indicate multi-head/task
        self.multihead = True if len(self.config['out_dim']) > 1 else False
        self.model = self.create_model()
        self.criterion_fn = nn.CrossEntropyLoss()
        self.kappa_scheduler = LinearScheduler(start=1, end=0.5)
        self.eps_scheduler = LinearScheduler(start=0)
        self.prev_weight, self.prev_eps = {}, {}
        self.clipping = self.config['clipping']
        self.current_head = "All"
        self.current_task = 1
        self.schedule_stack = []
        runs_path = Path('runs')
        log_dir = str(free_exp_name(runs_path, f"{self.config['dataset_name']}_experiment"))
        self.tb = SummaryWriter(log_dir=log_dir)
        for s in self.config["schedule"][::-1]:
            self.schedule_stack.append(s)

        if agent_config['gpuid'][0] >= 0:
            self.cuda()
            self.gpu = True
        else:
            self.gpu = False
        self.init_optimizer()
        self.reset_optimizer = False
        self.valid_out_dim = 'ALL'
        # Default: 'ALL' means all output nodes are active
        # Set a interger here for the incremental class scenario
        self.epochs_completed = 0

    def init_optimizer(self):
        optimizer_arg = {
            'params': (p for p in self.model.parameters() if p.requires_grad),
            'lr': self.config['lr'],
            'weight_decay': self.config['weight_decay']
        }
        if self.config['optimizer'] in ['SGD', 'RMSprop']:
            optimizer_arg['momentum'] = self.config['momentum']
        elif self.config['optimizer'] in ['Rprop']:
            optimizer_arg.pop('weight_decay')
        elif self.config['optimizer'] == 'amsgrad':
            optimizer_arg['amsgrad'] = True
            self.config['optimizer'] = 'Adam'

        self.optimizer = opt.__dict__[self.config['optimizer']](**optimizer_arg)
        self.scheduler = opt.lr_scheduler.MultiStepLR(self.optimizer, milestones=self.config['milestones'], gamma=0.1)

    def create_model(self):
        cfg = self.config
        task_output_space = cfg['out_dim']

        # Define the backbone (MLP, LeNet, VGG, ResNet ... etc) of model
        model = models.__dict__[cfg['model_type']].__dict__[cfg['model_name']](task_output_space)

        # Load pre-trained weights
        if cfg['model_weights'] is not None:
            print('=> Load model weights:', cfg['model_weights'])
            model_state = torch.load(cfg['model_weights'], map_location=lambda storage, loc: storage)  # Load to CPU.
            model.load_state_dict(model_state)
            print('=> Load Done')
        return model

    def forward(self, x):
        return self.model.forward(x)

    def predict(self, inputs):
        self.model.eval()
        out = self.forward(inputs)
        for t in out.keys():
            out[t], _, _ = split_activation(out[t])
            out[t] = out[t].detach()
        return out

    def restore_weights(self):
        i = 0
        for m in self.model.modules():
            if isinstance(m, IntervalLayerWithParameters):
                m.weight.data = self.prev_weight[i].clone()
                i += 1

    def move_weights(self, sign):
        for m in self.model.modules():
            if isinstance(m, IntervalLayerWithParameters):
                m.weight.data += sign * m.eps

    def validation_with_move_weights(self, dataloader, val_id: Union[int, str]):
        # moves = (0.001, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1)
        moves = (1, )
        for move in moves:
            self.move_weights(-move)
            self.validation(dataloader, val_id=val_id, txt=f"Lower {move}", suffix=f'move-{move}/')
            self.restore_weights()

        for move in moves:
            self.move_weights(move)
            self.validation(dataloader, val_id=val_id, txt=f"Upper {move}", suffix=f'move+{move}/')
            self.restore_weights()

    def validation(self, dataloader, val_id: Union[int, str], txt='', suffix=''):
        # This function doesn't distinguish tasks.
        batch_timer = Timer()
        acc = AverageMeter()
        batch_timer.tic()

        orig_mode = self.training
        self.eval()
        for i, (inputs, target, task) in enumerate(dataloader):
            if self.gpu:
                with torch.no_grad():
                    inputs = inputs.cuda()
                    target = target.cuda()
            output = self.predict(inputs)

            # Summarize the performance of all tasks, or 1 task, depends on dataloader.
            # Calculated by total number of data.
            acc = accumulate_acc(output, target, task, acc)

        self.train(orig_mode)

        self.log(' * {txt} Val Acc {acc.avg:.3f}, time {time:.2f}'.format(txt=txt, acc=acc, time=batch_timer.toc()))
        if is_wandb_on:
            wandb.log({
                f'validation/accuracy/{suffix}{val_id}': acc.avg,
            },
                commit=False
            )
        return acc.avg

    def criterion(self, logits, targets, tasks, **kwargs):
        # The inputs and targets could come from single task or a mix of tasks
        # The network always makes the predictions with all its heads
        # The criterion will match the head and task to calculate the loss.
        if self.multihead:
            standard_loss, robust_loss = torch.tensor(
                0.0, device=targets.device), torch.tensor(0.0, device=targets.device)
            robust_err = torch.tensor(0.0)  # TODO
            # TODO this seems to be unnecessary? we always get one task for entire batch from the loader
            for t, t_logits in logits.items():
                # The index of inputs that matched specific task
                inds = [i for i in range(len(tasks)) if tasks[i] == t]
                # print(f'tasks: {tasks} inds: {inds}')
                if len(inds) > 0:
                    t_logits = t_logits[inds]
                    t_target = targets[inds]
                    m_logits, l_logits, u_logits = split_activation(t_logits)
                    standard_loss += self.criterion_fn(m_logits, t_target) * len(inds)
                    if self.eps_scheduler.current:
                        targets_oh = nn.functional.one_hot(targets, m_logits.size(-1))
                        z_logits = torch.where(targets_oh.bool(), l_logits, u_logits)
                        robust_loss += self.criterion_fn(z_logits, targets) * len(inds)
            standard_loss /= len(targets)
            robust_loss /= len(targets)
            if self.eps_scheduler.current:
                kappa = self.kappa_scheduler.current
                loss = kappa * standard_loss + (1 - kappa) * robust_loss
            else:
                loss = standard_loss
        else:
            key = 'All'
            logits = logits[key]
            # (Not 'ALL') Mask out the outputs of unseen classes for incremental class scenario
            m_logits, l_logits, u_logits = split_activation(logits)
            if isinstance(self.valid_out_dim, int):
                m_logits = m_logits[:, :self.valid_out_dim]
                l_logits = l_logits[:, :self.valid_out_dim]
                u_logits = u_logits[:, :self.valid_out_dim]
            standard_loss = self.criterion_fn(m_logits, targets)
            if self.eps_scheduler.current:
                targets_oh = nn.functional.one_hot(targets, m_logits.size(-1))
                z_logits = torch.where(targets_oh.bool(), l_logits, u_logits)
                robust_loss = self.criterion_fn(z_logits, targets)
                kappa = self.kappa_scheduler.current
                loss = kappa * standard_loss + (1 - kappa) * robust_loss
                robust_err = torch.tensor(0.0)  # TODO
            else:
                loss, robust_err, robust_loss = standard_loss, torch.tensor(0.0), torch.tensor(0.0)
        return loss, robust_err, robust_loss, standard_loss

    def save_params(self):
        self.prev_weight, self.prev_eps = {}, {}
        i = 0
        for block in self.model.modules():
            if isinstance(block, IntervalLayerWithParameters):
                self.prev_weight[i] = block.weight.data.detach().clone()
                self.prev_eps[i] = block.eps.detach().clone()
                i += 1

        self.tb.add_histogram("importances", self.model.importances, self.current_task)

        # self.tb.add_histogram("c1/0/weight", self.model.c1[0].weight, self.current_task)
        # self.tb.add_histogram("c1/0/eps", self.model.c1[0].eps, self.current_task)

        # self.tb.add_histogram("c1/2/weight", self.model.c1[2].weight, self.current_task)
        # self.tb.add_histogram("c1/2/eps", self.model.c1[2].eps, self.current_task)

        # self.tb.add_histogram("c2/0/weight", self.model.c2[0].weight, self.current_task)
        # self.tb.add_histogram("c2/0/eps", self.model.c2[0].eps, self.current_task)

        # self.tb.add_histogram("c2/2/weight", self.model.c2[2].weight, self.current_task)
        # self.tb.add_histogram("c2/2/eps", self.model.c2[2].eps, self.current_task)

        # self.tb.add_histogram("c3/0/weight", self.model.c3[0].weight, self.current_task)
        # self.tb.add_histogram("c3/0/eps", self.model.c3[0].eps, self.current_task)

        # self.tb.add_histogram("c3/2/weight", self.model.c3[2].weight, self.current_task)
        # self.tb.add_histogram("c3/2/eps", self.model.c3[2].eps, self.current_task)

        # self.tb.add_histogram('fc1/weight', self.model.fc1[0].weight, self.current_task)
        # self.tb.add_histogram("fc1/eps", self.model.fc1[0].eps, self.current_task)

        # self.tb.add_histogram('last/weight', self.model.last[self.current_head].weight, self.current_task)
        # self.tb.add_histogram("last/eps", self.model.last[self.current_head][0].eps, self.current_task)
        # self.tb.flush()

    def clip_weights(self, i, weights):
        low_old = self.prev_weight[i] - self.prev_eps[i]
        upp_old = self.prev_weight[i] + self.prev_eps[i]
        weights = torch.max(low_old, weights)
        weights = torch.min(upp_old, weights)
        return weights

    def clip_intervals(self, i, layer_weight, layer_eps):
        eps_old = self.prev_eps[i]
        assert (eps_old >= 0).all()

        low_old = self.prev_weight[i] - eps_old
        upp_old = self.prev_weight[i] + eps_old
        assert (low_old <= layer_weight).all()
        assert (upp_old >= layer_weight).all()

        low_new = layer_weight - layer_eps
        upp_new = layer_weight + layer_eps

        low = torch.max(low_old, low_new)
        upp = torch.min(upp_old, upp_new)
        assert (low <= upp).all()

        weight_new = (low + upp) / 2
        eps_new = torch.abs(low - upp) / 2
        eps_new = torch.where(eps_new > eps_old, eps_old, eps_new)
        assert (eps_old >= eps_new
                ).all(), print(f'eps assert i: {i}, bad: {(eps_old < eps_new).sum()}, all: {(eps_new >= 0).sum()}')

        return eps_new, weight_new

    def clip_params(self):
        i = 0
        for m in self.model.modules():
            if isinstance(m, IntervalLayerWithParameters):
                m.weight.data = self.clip_weights(i, m.weight.data.detach())
                # m.eps, m.weight.data = self.clip_intervals(i, m.weight.data.detach(), m.eps.detach())
                i += 1

    def update_model(self, inputs, targets, tasks):
        self.model.importances_to_eps(self.eps_scheduler.current)
        out = self.forward(inputs)
        loss, robust_err, robust_loss, ce_loss = self.criterion(out, targets, tasks)
        self.zero_grad()
        loss.backward()
        # TODO add cmd argument for gradient clipping?
        # nn.utils.clip_grad_norm_(self.model.parameters(), 1)
        # nn.utils.clip_grad_norm_(self.model.parameters(), 1, norm_type=float('inf'))
        self.optimizer.step()

        self.kappa_scheduler.step()
        self.eps_scheduler.step()
        self.tb.add_scalar(f"Kappa/train - task {self.current_task}", self.kappa_scheduler.current, self.current_batch)
        self.tb.add_scalar(f"Epsilon/train - task {self.current_task}", self.eps_scheduler.current, self.current_batch)
        if self.clipping and self.prev_eps:
            self.clip_params()
        for t in out.keys():
            out[t], _, _ = split_activation(out[t])
            out[t] = out[t].detach()
        return loss.item(), robust_err, robust_loss.item(), ce_loss.item(), out

    def learn_batch(self, train_loader, val_loader=None):
        if self.reset_optimizer:  # Reset optimizer before learning each task
            self.log('Optimizer is reset!')
            self.init_optimizer()

        schedule = self.schedule_stack.pop()
        self.current_batch = 0
        for epoch in range(schedule):
            data_timer = Timer()
            batch_timer = Timer()
            batch_time = AverageMeter()
            data_time = AverageMeter()
            losses = AverageMeter()
            acc = AverageMeter()
            robust_err, robust_loss = -1, -1

            # Config the model and optimizer
            self.log('Epoch:{0}'.format(epoch))
            self.model.train()
            for param_group in self.optimizer.param_groups:
                self.log('LR:', param_group['lr'])

            # Learning with mini-batch
            data_timer.tic()
            batch_timer.tic()

            for i, (inputs, target, task) in enumerate(train_loader):
                data_time.update(data_timer.toc())  # measure data loading time
                if self.gpu:
                    inputs = inputs.cuda()
                    target = target.cuda()

                loss, robust_err, robust_loss, ce_loss, output = self.update_model(inputs, target, task)
                inputs = inputs.detach()
                target = target.detach()
                self.tb.add_scalar(f"Loss/train - task {self.current_task}", loss, self.current_batch)
                self.tb.add_scalar(f"CE Loss/train - task {self.current_task}", ce_loss, self.current_batch)
                self.tb.add_scalar(f"Robust loss/train - task {self.current_task}", robust_loss, self.current_batch)
                self.tb.add_scalar(f"Robust error/train - task {self.current_task}", robust_err, self.current_batch)
                # measure accuracy and record loss
                acc = accumulate_acc(output, target, task, acc)
                losses.update(loss, inputs.size(0))
                self.current_batch += 1

                batch_time.update(batch_timer.toc())  # measure elapsed time
                data_timer.toc()

            self.log(' * Train Acc {acc.avg:.3f}, Loss {loss.avg:.3f}'.format(loss=losses, acc=acc))
            self.log(f" * robust loss: {robust_loss:.10f} robust error: {robust_err:.10f}")
            # self.log(f"  * model: {self.model.features_loss_term}")

            if is_wandb_on:
                wandb.log({
                    'epoch': self.epochs_completed,
                    'task': self.current_task,
                    'task/epoch': epoch,
                    'train/accuracy': acc.avg,
                    'train/loss': losses.avg,
                    'robust/loss': robust_loss,
                    'robust/error': robust_err,
                },
                    step=self.epochs_completed
                )

            # Evaluate the performance of current task
            if val_loader is not None:
                self.validation(val_loader, val_id=self.current_task)

            self.scheduler.step()
            self.epochs_completed += 1
            # self.tb.flush()

    def add_valid_output_dim(self, dim=0):
        # This function is kind of ad-hoc, but it is the simplest way to support incremental class learning
        self.log('Incremental class: Old valid output dimension:', self.valid_out_dim)
        if self.valid_out_dim == 'ALL':
            self.valid_out_dim = 0  # Initialize it with zero
        self.valid_out_dim += dim
        self.log('Incremental class: New Valid output dimension:', self.valid_out_dim)
        return self.valid_out_dim

    def count_parameter(self):
        return sum(p.numel() for p in self.model.parameters())

    def save_model(self, filename):
        model_state = self.model.state_dict()
        if isinstance(self.model, torch.nn.DataParallel):
            # Get rid of 'module' before the name of states
            model_state = self.model.module.state_dict()
        for key in model_state.keys():  # Always save it to cpu
            model_state[key] = model_state[key].cpu()
        print('=> Saving model to:', filename)
        torch.save(model_state, filename + '.pth')
        print('=> Save Done')

    def cuda(self):
        # torch.cuda.set_device(self.config['gpuid'][0])
        self.model = self.model.cuda()
        self.criterion_fn = self.criterion_fn.cuda()
        # Multi-GPU
        if len(self.config['gpuid']) > 1:
            self.model = torch.nn.DataParallel(self.model,
                                               device_ids=self.config['gpuid'],
                                               output_device=self.config['gpuid'][0])
        return self


def accumulate_acc(output, target, task, meter):
    if 'All' in output.keys():  # Single-headed model
        meter.update(accuracy(output['All'], target), len(target))
    else:  # outputs from multi-headed (multi-task) model
        for t, t_out in output.items():
            inds = [i for i in range(len(task)) if task[i] == t]  # The index of inputs that matched specific task
            if len(inds) > 0:
                t_out = t_out[inds]
                t_target = target[inds]
                meter.update(accuracy(t_out, t_target), len(inds))
    return meter
