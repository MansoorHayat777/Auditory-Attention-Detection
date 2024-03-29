#python version = 3.9.12
from scipy.io import loadmat
from sklearn.utils import shuffle
import numpy as np
import pandas as pd
import os
import gc
import math

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.utils.data import DataLoader, Dataset, TensorDataset

"""Data loading"""
X_train = np.array(np.load('X_train_cnn.npz')['arr_0'], dtype=np.float16)
X_val = np.array(np.load('X_val_cnn.npz')['arr_0'], dtype=np.float16)
y_train = np.array(np.load('y_train_cnn.npz')['arr_0'])
y_val = np.array(np.load('y_val_cnn.npz')['arr_0'])

print("X_train", X_train.shape)
print("y_train", y_train.shape)
print("X_val", X_val.shape)
print("y_val", y_val.shape)

# set parameters
window_time = 2  # 0.1/0.5/1/2 seconds
is_64 = True
is_se = True
is_se_band = is_se and True  # band attention
is_se_channel = is_se and True  # channel attention
wav_band = 5
eeg_band = 5

max_epoch = 100
dataset_name = 'KUL'
data_special = 'END'
se_channel_type = 'avg'
se_band_type = 'max'

wav_channel = 1
eeg_channel = 64
eeg_channel_new = 64 if is_64 else 16

eeg_s_band = 0
eeg_pool_num = 1
eeg_start = wav_channel * wav_band + eeg_channel * eeg_s_band
eeg_end = eeg_start + eeg_channel * eeg_band
label_text = 'ADN3_' + dataset_name + '_' + data_special

trail_channel_nor = False
window_nor = False
is_use_wav = True

# # data path
# oriDataPath = '../dataset_csv/' + dataset_name + '/Band6'  # matlab后处理文件路径
# csv_label_path = '../dataset_csv/' + dataset_name + '_label/'
# npyDataPath = '../dataset_npy/' + dataset_name + '/Time_' + str(window_time) + 's'
# cnnFile = './CNN_base.py'
# splitFile = './CNN_split.py'

time_sleep = 60
min_epoch = 50
is_early_stop = True
is_data_export = False
ConType = ["No"]  # No/Low/High

window_time = 1
overlap = 0.5


delay = 0  # the delay between the wav and eeg channels

time_split = 60
lrRate = 1e-3
fs_data = 128
subject_number = 16
trial_number = 8
cell_number = fs_data * 360
names = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9",
            "S10", "S11", "S12", "S13", "S14", "S15", "S16"]
classLabel = 0  # classification label, direction of KUL

window_length = window_time*fs_data  # the length of each samples
vali_percent = 0.2
test_percent = 0.2
file_num_each_trail = 1  # the data format
cnn_ken_num = 10  # the number of the conv
fcn_input_num = cnn_ken_num

is_beyond_trail = False  # is cross trail
is_all_train = False  # is cross subject
isDS = False  # is use the wav data
channel_number = eeg_channel * eeg_band + 2 * wav_channel * wav_band 

# device = torch.device('cuda:' + str(gpu_random)) # gpu

label = label_text + \
        '_seB' + str(is_se_band)[0] + se_band_type + \
        '_seC' + str(is_se_channel)[0] + se_channel_type + \
        '_is64' + str(is_64)[0] + \
        '_winL' + str(window_time) + 's_' + \
        '_wav' + str(is_use_wav)[0] + \
        '_maxE' + str(max_epoch) + '_' + 'lenT' + str(window_length)  # 训练标识


class mySE(nn.Module):
    def __init__(self, se_weight_num, se_type, se_fcn_squeeze, conv_num):
        super(mySE, self).__init__()
        se_fcn_num_dict = {'avg': se_weight_num, 'max': se_weight_num, 'mix': se_weight_num * 2}
        se_fcn_num = se_fcn_num_dict.get(se_type)

        self.se_conv = nn.Sequential(
            nn.Conv3d(1, 1, (1, conv_num, 1), stride=(1, 1, 1)),
            nn.ELU(),
        )

        self.se_fcn = nn.Sequential(
            nn.Linear(se_fcn_num, se_fcn_squeeze),
            nn.Tanh(),
            nn.Dropout(0.5),
            nn.Linear(se_fcn_squeeze, se_weight_num),
            nn.Tanh(),
        )

    def forward(self, se_data, se_type):
        se_weight = se_data
        se_weight = self.se_conv(se_weight.unsqueeze(0).unsqueeze(0))
        se_weight = se_weight.squeeze(0)

        avg_data = torch.mean(se_weight, axis=-1)
        max_data = torch.max(se_weight, axis=-1)[0]

        mix_data = torch.cat((avg_data, max_data), dim=1)
        data_dict = {'avg': avg_data, 'max': max_data, 'mix': mix_data}
        se_weight = data_dict.get(se_type)
        se_weight = torch.mean(se_weight, axis=0).squeeze(0).transpose(0, 1)

        se_weight = self.se_fcn(se_weight)

        # mask
        se_weight = (se_weight - torch.min(se_weight)) / (torch.max(se_weight) - torch.min(se_weight))

        # weighted
        output = ((se_data.transpose(0, 2)) * se_weight).transpose(0, 2)

        return output


# the main model
class CNN(nn.Module):
    def __init__(self):
        super(CNN, self).__init__()

        self.se_band = mySE(eeg_band, se_band_type, 5, eeg_channel_new)
        self.se_channel = mySE(eeg_channel_new, se_channel_type, 8, eeg_band)
        self.cnn_conv_eeg = nn.Sequential(
            nn.Conv2d(eeg_band, cnn_ken_num, (eeg_channel_new, 9), stride=(eeg_channel_new, 1)),
            nn.ReLU(),
            nn.AdaptiveMaxPool2d((1, 1 * window_time)),
        )

        self.cnn_fcn = nn.Sequential(
            nn.Linear(fcn_input_num * window_time, fcn_input_num),
            nn.Sigmoid(),
            nn.Dropout(0.5),
            nn.Linear(fcn_input_num, 2),
            nn.Softmax(dim=1),
        )

    def forward(self, x):
        # split the wav and eeg data
        # eeg = x[:, :, eeg_start:eeg_end, :]
        # wav_a = x[:, :, 0:wav_band, :]
        # wav_b = x[:, :, -wav_band:, :]
        eeg = x
        # frequency attention
        if is_se_band:
            eeg = eeg.view(eeg_band, eeg_channel_new, window_length)
            eeg = self.se_band(eeg, se_band_type)

        # channel attention
        if is_se_channel:
            eeg = eeg.view(eeg_band, eeg_channel_new, window_length).transpose(0, 1)
            eeg = self.se_channel(eeg, se_channel_type).transpose(0, 1)

        # normalization
        eeg = eeg.view(1, eeg_band, eeg_channel_new, window_length)
        wav_a = wav_a.view(1, wav_band, wav_channel, window_length)
        wav_b = wav_b.view(1, wav_band, wav_channel, window_length)

        # convolution
        y = torch.cat([wav_a, eeg, wav_b], dim=2) if isDS else eeg
        y = self.cnn_conv_eeg(y)
        y = y.view(1, -1)

        # classification
        output = self.cnn_fcn(y)

        return output


# initialization
def weights_init_uniform(m):
    class_name = m.__class__.__name__
    if class_name.find('Linear') != -1:
        m.weight.data.uniform_(-1.0, 1.0)
        m.bias.data.fill_(0)


# initialization
myNet = CNN()
myNet.apply(weights_init_uniform)
optimizer = torch.optim.SGD([
    {'params': myNet.cnn_fcn.parameters(), 'lr': lrRate},
    {'params': myNet.cnn_conv_eeg.parameters(), 'lr': lrRate},
    {'params': myNet.se_band.parameters(), 'lr': lrRate * 1e1},
    {'params': myNet.se_channel.parameters(), 'lr': lrRate * 1e1},
])
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.1, patience=5, verbose=True,
                                                       threshold=0.0001, threshold_mode='rel', cooldown=5, min_lr=0,
                                                       eps=0.001)

batch_size = 1
classes = {'left', 'right'}
loss_func = nn.CrossEntropyLoss()

# y_train = np.reshape(y_train, (y_train.shape[0], 1))
# y_val = np.reshape(y_val, (y_val.shape[0], 1))
X_train = torch.tensor(X_train)
y_train = torch.tensor(y_train)
X_val = torch.tensor(X_val)
y_val = torch.tensor(y_val)

train_dataset = TensorDataset(X_train, y_train)
train_dataloader = DataLoader(train_dataset, batch_size=batch_size)

test_dataset = TensorDataset(X_val, y_val)
test_dataloader = DataLoader(test_dataset, batch_size=batch_size)


# Function to save the model
def saveModel():
    path = "./paper_model_cnn.pth"
    torch.save(myNet.state_dict(), path)
    
# Function to test the model with the test dataset and print the accuracy for the test images
def testAccuracy():
    
    myNet.eval()
    accuracy = 0.0
    total = 0.0
    
    with torch.no_grad():
        for data in test_dataloader:
            eeg, labels = data
            # run the model on the test set to predict labels
            outputs = myNet(eeg)
            # the label with the highest energy will be our prediction
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            accuracy += (predicted == labels).sum().item()
    
    # compute the accuracy over all test images
    accuracy = (100 * accuracy / total)
    return(accuracy)


# Training function. We simply have to loop over our data iterator and feed the inputs to the network and optimize.
def train(num_epochs):
    
    best_accuracy = 0.0

    # Define your execution device
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    print("The model will be running on", device, "device")
    # Convert model parameters and buffers to CPU or Cuda
    myNet.to(device)

    for epoch in range(num_epochs):  # loop over the dataset multiple times
        running_loss = 0.0
        running_acc = 0.0

        for i, data in enumerate(train_dataloader, 0):
            
            # get the inputs
            print("I Value", i)
            eeg, labels = data
            eeg = Variable(eeg.to(device))
            labels = Variable(labels.to(device))
            print("EEG DATA", eeg.size())
            # zero the parameter gradients
            optimizer.zero_grad()
            # predict classes using images from the training set
            outputs = myNet(eeg)
            # compute the loss based on model output and real labels
            loss = loss_func(outputs, labels)
            # backpropagate the loss
            loss.backward()
            # adjust parameters based on the calculated gradients
            optimizer.step()

            # Let's print statistics for every 1,000 images
            running_loss += loss.item()     # extract the loss value
            if i % 1000 == 999:    
                # print every 1000 (twice per epoch) 
                print('[%d, %5d] loss: %.3f' %
                      (epoch + 1, i + 1, running_loss / 1000))
                # zero the loss
                running_loss = 0.0

        # Compute and print the average accuracy fo this epoch when tested over all 10000 test images
        accuracy = testAccuracy()
        print('For epoch', epoch+1,'the test accuracy over the whole test set is %d %%' % (accuracy))
        
        # we want to save the model if the accuracy is the best
        if accuracy > best_accuracy:
            saveModel()
            best_accuracy = accuracy

# Function to test the model with a batch of images and show the labels predictions
def testBatch():
    # get batch of images from the test DataLoader  
    eeg, labels = next(iter(test_dataloader))

   
    # Show the real labels on the screen 
    print('Real labels: ', ' '.join('%5s' % classes[labels[j]] 
                               for j in range(batch_size)))
  
    # Let's see what if the model identifiers the  labels of those example
    outputs = myNet(eeg)
    
    # We got the probability for every 10 labels. The highest (max) probability should be correct label
    _, predicted = torch.max(outputs, 1)
    
    # Let's show the predicted labels on the screen to compare with the real ones
    print('Predicted: ', ' '.join('%5s' % classes[predicted[j]] 
                              for j in range(batch_size)))

if __name__ == "__main__":
    
    # Let's build our model
    train(1)
    print('Finished Training')

    # Test which classes performed well
    testAccuracy()
    
    # Let's load the model we just created and test the accuracy per label
    myNet = CNN()
    path = "paper_model_cnn.pth"
    myNet.load_state_dict(torch.load(path))
    
    # Test with batch of images
    testBatch()
    
    