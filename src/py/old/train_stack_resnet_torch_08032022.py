import logging
import os
import sys
import tempfile
from glob import glob
import math

from sklearn.model_selection import train_test_split
import pandas as pd
import SimpleITK as sitk 
import numpy as np
from tqdm import tqdm

import torch
import torch.multiprocessing as mp
import torch.distributed as dist
from torch import nn
from torch.utils.data import Dataset
from torch.utils.data import DataLoader
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data.distributed import DistributedSampler

import torchvision.models as models
from torchvision import transforms as T

import monai
from monai.data import create_test_image_2d, list_data_collate, decollate_batch
from monai.inferers import sliding_window_inference
from monai.metrics import ConfusionMatrixMetric
from monai.transforms import (
    AddChanneld,
    AsChannelFirstd,
    Compose,
    RandRotated,
    ScaleIntensityd,
    ToTensord,
    EnsureType,
    Activations, 
    AsDiscrete,
    Lambdad
)
from monai.visualize import plot_2d_or_3d_image

from sklearn.utils import class_weight

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

class EarlyStopping:
    """Early stops the training if validation loss doesn't improve after a given patience."""
    def __init__(self, patience=7, verbose=False, delta=0, path='checkpoint.pt', trace_func=print):
        """
        Args:
            patience (int): How long to wait after last time validation loss improved.
                            Default: 7
            verbose (bool): If True, prints a message for each validation loss improvement. 
                            Default: False
            delta (float): Minimum change in the monitored quantity to qualify as an improvement.
                            Default: 0
            path (str): Path for the checkpoint to be saved to.
                            Default: 'checkpoint.pt'
            trace_func (function): trace print function.
                            Default: print            
        """
        self.patience = patience
        self.verbose = verbose
        self.counter = 0
        self.best_score = None
        self.early_stop = False
        self.val_loss_min = np.Inf
        self.delta = delta
        self.path = path
        self.trace_func = trace_func
    def __call__(self, val_loss, model):

        score = -val_loss

        if self.best_score is None:
            self.best_score = score
            self.save_checkpoint(val_loss, model)
        elif score < self.best_score + self.delta:
            self.counter += 1
            self.trace_func(f'EarlyStopping counter: {self.counter} out of {self.patience}. Best validation loss: {self.val_loss_min:.6f} <-- {val_loss:.6f}')
            if self.counter >= self.patience:
                self.early_stop = True
        else:
            self.best_score = score
            self.save_checkpoint(val_loss, model)
            self.counter = 0

    def save_checkpoint(self, val_loss, model):
        '''Saves model when validation loss decrease.'''
        if self.verbose:
            self.trace_func(f'Validation loss decreased ({self.val_loss_min:.6f} --> {val_loss:.6f}).  Saving model ...')
       
        torch.save(model.state_dict(), self.path)
        self.val_loss_min = val_loss

class DatasetGenerator(Dataset):
    def __init__(self, df):
        self.df = df

    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):

        row = self.df.loc[idx]            
        img = os.path.join("/work/jprieto/data/remote/EGower/", row["img"])
        sev = row["class"]

        img_np = sitk.GetArrayFromImage(sitk.ReadImage(img))
        img_np = np.transpose(img_np, (0, 3, 1, 2))

        return {"img": img_np, "class": sev}
    

def cleanup():
    dist.destroy_process_group()

class Identity(nn.Module):
    def __init__(self):
        super(Identity, self).__init__()
    def forward(self, x):
        return x

class SelfAttention(nn.Module):
    def __init__(self, in_units, out_units):
        super(SelfAttention, self).__init__()
        self.W1 = nn.Linear(in_units, out_units)
        self.V = nn.Linear(out_units, 1)

    def forward(self, query, values):
        
        score = self.V(nn.Tanh()(self.W1(query)))
        
        # score = nn.Sigmoid()(score)
        # sum_score = torch.sum(score, 1, keepdim=True)
        # attention_weights = score / sum_score
        attention_weights = nn.Softmax(dim=1)(score)

        # context_vector shape after sum == (batch_size, hidden_size)
        context_vector = attention_weights * values
        context_vector = torch.sum(context_vector, dim=1)

        return context_vector, attention_weights, score


class TimeDistributed(nn.Module):
    def __init__(self, module):
        super(TimeDistributed, self).__init__()
        self.module = module
 
    def forward(self, input_seq):
        assert len(input_seq.size()) > 2
 
        # reshape input data --> (samples * timesteps, input_size)
        # squash timesteps

        size = input_seq.size()

        batch_size = size[0]
        time_steps = size[1]

        size_reshape = [batch_size*time_steps] + list(size[2:])
        reshaped_input = input_seq.contiguous().view(size_reshape)
 
        output = self.module(reshaped_input)
        
        output_size = output.size()
        output_size = [batch_size, time_steps] + list(output_size[1:])
        output = output.contiguous().view(output_size)

        return output


class Features(nn.Module):
    def __init__(self):
        super(Features, self).__init__()
        self.feat = models.resnet50(pretrained=True)
        self.feat.fc = Identity()
        self.conv = nn.Conv2d(2048, 512, (2, 2), stride=2)
        self.avg = nn.AdaptiveAvgPool2d(output_size=(1, 1))
    def forward(self, x):
        x = self.feat.conv1(x)
        x = self.feat.bn1(x)
        x = self.feat.relu(x)
        x = self.feat.maxpool(x)
        x = self.feat.layer1(x)
        x = self.feat.layer2(x)
        x = self.feat.layer3(x)
        x = self.feat.layer4(x)
        x = self.conv(x)
        x = self.avg(x)
        x = torch.flatten(x, start_dim=1)
        return x

class PositionalEncoding(nn.Module):
    def __init__(self, d_model: int, dropout: float = 0.1, max_len: int = 5000):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)
        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) * (-math.log(10000.0) / d_model))
        pe = torch.zeros(max_len, 1, d_model)
        pe[:, 0, 0::2] = torch.sin(position * div_term)
        pe[:, 0, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe)
    def forward(self, x):
        # Args:
        #     x: Tensor, shape [seq_len, batch_size, embedding_dim]
        x = x + self.pe[:x.size(0)]
        return self.dropout(x)

class TTNet(nn.Module):
    def __init__(self):
        super(TTNet, self).__init__()

        
        self.Features = Features()
        self.TimeDistributed = TimeDistributed(self.Features)
        self.WV = nn.Linear(512, 256)
        self.PE = PositionalEncoding(d_model=256, max_len=16)
        self.Attention = SelfAttention(512, 128)
        self.Prediction = nn.Linear(256, 2)
        
 
    def forward(self, x):

        x = self.TimeDistributed(x)

        x_v = self.WV(x)
        x_v = self.PE(x_v)

        x_a, w_a, w_s = self.Attention(x, x_v)

        x = self.Prediction(x_a)
        x = nn.LogSoftmax(dim=1)(x)

        return x

class TrainTransforms(nn.Module):
    def __init__(self):
        super(TrainTransforms, self).__init__()

        t = T.Compose([
            T.GaussianBlur((7, 7), sigma=(0.01, 2.0)),
            T.RandomRotation(math.pi/2.0),
            T.RandomResizedCrop(448, scale=(0.08, 1.0), ratio=(0.75, 1.3333333333333333)),
            T.ColorJitter(brightness=(0.0, 3.0), contrast=(0.0, 3.0), saturation=(0, 3.0), hue=0.25)
        ])

        self.TimeDistributed = TimeDistributed(t)
        
 
    def forward(self, x):

        return self.TimeDistributed(x)

def main(rank, world_size):
    
    # logging.basicConfig(stream=sys.stdout, level=logging.INFO)

    dist.init_process_group("nccl", init_method='env://', rank=rank, world_size=world_size)
    print(
        f"Rank {rank + 1}/{world_size} process initialized.\n"
    )
    
    device = torch.device(f"cuda:{rank}")
    torch.cuda.set_device(device)

    csv_path_stacks_train = "/work/jprieto/data/remote/EGower/hinashah/Analysis_Set_01132022/trachoma_normals_healthy_sev123_epi_stack_16_768_train_train.csv"
    train_df = pd.read_csv(csv_path_stacks_train)
    train_df['class'] = (train_df['class'] >= 1).astype(int)

    csv_path_stacks_valid = "/work/jprieto/data/remote/EGower/hinashah/Analysis_Set_01132022/trachoma_normals_healthy_sev123_epi_stack_16_768_train_valid.csv"
    valid_df = pd.read_csv(csv_path_stacks_valid)
    valid_df['class'] = (valid_df['class'] >= 1).astype(int)
    

    # define transforms for image and segmentation
    train_transforms = Compose(
        [
            ScaleIntensityd(keys=["img"]),
            ToTensord(keys=["img"])
        ]
    )
    val_transforms = Compose(
        [
            ScaleIntensityd(keys=["img"]),
            ToTensord(keys=["img"])
        ]
    )

    # create a training data loader
    train_ds = monai.data.Dataset(data=DatasetGenerator(train_df), transform=train_transforms)
    # use batch_size=2 to load images and use RandCropByPosNegLabeld to generate 2 x 4 images for network training

    train_sampler = DistributedSampler(train_ds, num_replicas=world_size, rank=rank)

    train_loader = DataLoader(
        train_ds,
        batch_size=4,
        sampler=train_sampler,
        num_workers=8,
        collate_fn=list_data_collate
    )
    
    # create a validation data loader
    val_ds = monai.data.Dataset(data=DatasetGenerator(valid_df), transform=val_transforms)

    val_sampler = DistributedSampler(val_ds, shuffle=False, num_replicas=world_size, rank=rank)
    val_loader = DataLoader(val_ds, sampler=val_sampler, batch_size=1, num_workers=8, collate_fn=list_data_collate)

    val_metric = ConfusionMatrixMetric(include_background=True, metric_name="accuracy", reduction="mean", get_not_nans=False)
    

    unique_classes = np.unique(train_df['class'])
    unique_class_weights = np.array(class_weight.compute_class_weight(class_weight='balanced', classes=unique_classes, y=train_df['class']))

    print("Unique classes:", unique_classes, unique_class_weights)

    model = TTNet().to(device)
    model = DDP(model, device_ids=[device])

    model_transforms = TrainTransforms().to(device)

    loss_function = nn.NLLLoss(weight=torch.Tensor(unique_class_weights)).cuda(device)
    optimizer = torch.optim.Adam(model.parameters(), 1e-4)

    # start a typical PyTorch training
    val_interval = 1
    best_metric = -1
    best_metric_epoch = -1
    epoch_loss_values = list()
    metric_values = list()
    # writer = SummaryWriter()

    num_epochs = 100
    early_stop = EarlyStopping(patience=10, verbose=True,
        path='train/torch_stack_resnet_torch_08032022/model.pt')

    for epoch in range(num_epochs):
        train_sampler.set_epoch(epoch)
        if rank == 0:
            print("-" * 10)
            print(f"epoch {epoch + 1}/{num_epochs}")

        model.train()
        epoch_loss = 0
        step = 0
        pbar = tqdm(train_loader)
        for batch_data in pbar:

            step += 1
            inputs, labels = batch_data["img"].to(device), batch_data["class"].to(device)
            
            optimizer.zero_grad()

            inputs = model_transforms(inputs)
            outputs = model(inputs)
            loss = loss_function(outputs, labels)
            loss.backward()
            optimizer.step()
            epoch_loss += loss
            if rank == 0:
                epoch_len = len(train_loader)
                pbar.set_description(f"{step}/{epoch_len}, train_loss: {loss.item():.4f}")
            # writer.add_scalar("train_loss", loss.item(), epoch_len * epoch + step)

        dist.all_reduce(epoch_loss)
        
        if rank == 0:
            epoch_loss = epoch_loss.item()/(step*world_size)
            epoch_loss_values.append(epoch_loss)
            print(f"epoch {epoch + 1} average loss: {epoch_loss:.4f}")

        
        with torch.no_grad():
            val_images = None
            val_labels = None
            val_outputs = None
            val_loss = 0.0
            step = 0
            pbar = tqdm(val_loader)
            for val_data in pbar:
                step += 1
                val_images, val_labels = val_data["img"].to(device), val_data["class"].to(device)
                
                val_outputs = model(val_images)
                val_loss += loss_function(outputs, labels)
                
                val_labels = nn.functional.one_hot(val_labels, num_classes=2)
                val_outputs = nn.functional.one_hot(torch.argmax(val_outputs, dim=1), num_classes=2)
                val_metric(y_pred=val_outputs, y=val_labels)
            # aggregate the final mean dice result
            metric = val_metric.aggregate()
            # reset the status for next validation round
            val_metric.reset()
            # dist.all_reduce(metric)

            dist.all_reduce(val_loss)
            val_loss = val_loss.cpu().item()/(step*world_size)

            print("Val confusion matrix", metric)

            if rank == 0:
                early_stop(val_loss, model.module)            
                if early_stop.early_stop:
                    early_stop_indicator = torch.tensor([1.0]).to(device)
                else:
                    early_stop_indicator = torch.tensor([0.0]).cuda()
            else:
                early_stop_indicator = torch.tensor([0.0]).to(device)

            dist.all_reduce(early_stop_indicator)

            if early_stop_indicator.cpu().item() == 1.0:
                print("Early stopping")            
                break

    if rank == 0:
        print(f"train completed")

    cleanup()



WORLD_SIZE = torch.cuda.device_count()
if __name__ == "__main__":
    
    os.environ['MASTER_ADDR'] = '127.0.0.1'
    os.environ['MASTER_PORT'] = '9999'

    mp.spawn(
        main, args=(WORLD_SIZE,),
        nprocs=WORLD_SIZE, join=True
    )


