
import os
import sys
import time
import math
import pandas as pd
import numpy as np
import json

import numpy as np
import tensorflow as tf
from tensorflow.keras import layers
from tensorflow.keras.utils import SequenceEnqueuer
from tensorflow.keras.utils import OrderedEnqueuer

import pickle  
import SimpleITK as sitk

from scipy import ndimage

import pickle
from sklearn.metrics import classification_report

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
        

class Features(tf.keras.layers.Layer):
    def __init__(self):
        super(Features, self).__init__()

        self.efficient = tf.keras.applications.EfficientNetV2S(include_top=False, weights='imagenet', input_tensor=tf.keras.Input(shape=[448, 448, 3]), pooling=None)
        
        self.center_crop = tf.keras.layers.CenterCrop(448, 448)        
        self.conv = layers.Conv2D(512, (2, 2), strides=(2, 2))
        self.avg = layers.GlobalAveragePooling2D()        

    def compute_output_shape(self, input_shape):
        return (None, 512)


    def call(self, x, training=True):
        
        x = self.center_crop(x)        
        x = self.efficient(x)
        x = self.conv(x)
        x = self.avg(x)

        return x

class TTModelPatch(tf.keras.Model):
    def __init__(self):
        super(TTModelPatch, self).__init__()

        self.features = Features()
        self.P = layers.Dense(3, activation='softmax', name='predictions')
        
    def call(self, x):

        x_f = self.features(x)
        x = self.P(x_f)

        return x


class Attention(tf.keras.layers.Layer):
    def __init__(self, units, w_units):
        super(Attention, self).__init__()
        self.W1 = tf.keras.layers.Dense(units)
        self.V = tf.keras.layers.Dense(w_units)

    def call(self, query, values):        

        # score shape == (batch_size, max_length, 1)
        # we get 1 at the last axis because we are applying score to self.V
        # the shape of the tensor before applying self.V is (batch_size, max_length, units)
        score = tf.nn.sigmoid(self.V(tf.nn.tanh(self.W1(query))))
        
        attention_weights = score/tf.reduce_sum(score, axis=1, keepdims=True)

        context_vector = attention_weights * values
        context_vector = tf.reduce_sum(context_vector, axis=1)

        return context_vector, score

class TTModel(tf.keras.Model):
    def __init__(self, features = None):
        super(TTModel, self).__init__()

        self.features = Features()        

        self.TD = layers.TimeDistributed(self.features)
        self.R = layers.Reshape((-1, 512))

        self.V = layers.Dense(256)
        self.A = Attention(128, 1)        
        self.P = layers.Dense(2, activation='softmax', name='predictions')
        
    def call(self, x):

        x = self.TD(x)
        x = self.R(x)

        x_v = self.V(x)
        x_a, x_s = self.A(x, x_v)
        
        x = self.P(x_a)
        x_v_p = self.P(x_v)

        return x, x_a, x_s, x_v, x_v_p


class DatasetGenerator(tf.keras.utils.Sequence):
    def __init__(self, df):
        self.df = df

    def __len__(self):
        return len(self.df)
    
    def __getitem__(self, idx):
            
        row = self.df.loc[idx]
        img = os.path.join("/work/jprieto/data/remote/EGower/hinashah/", row["image"])
        sev = row["class"]

        img_np = sitk.GetArrayFromImage(sitk.ReadImage(img))

        t, xs, ys, _ = img_np.shape
        xo = (xs - 448)//2
        yo = (ys - 448)//2
        img_np = img_np[:,xo:xo + 448, yo:yo + 448,:]
        
        one_hot = np.zeros(2)
        one_hot[sev] = 1

        return img_np, one_hot



checkpoint_path = "/work/jprieto/data/remote/EGower/jprieto/train/train_stack_efficientv2s_25042022_weights/train_stack_efficientv2s_25042022"

model = TTModel()
model.load_weights(checkpoint_path)
model.build(input_shape=(None, None, 448, 448, 3))
model.summary()



csv_path_stacks = "/work/jprieto/data/remote/EGower/hinashah/Analysis_Set_20220326/trachoma_bsl_mtss_besrat_field_test_20220326_stacks.csv"

test_df = pd.read_csv(csv_path_stacks).replace("/work/jprieto/data/remote/EGower/", "", regex=True)
test_df['class'] = (test_df['class'] >= 1).astype(int)

dg_test = DatasetGenerator(test_df)

def test_generator():

    enqueuer = OrderedEnqueuer(dg_test, use_multiprocessing=True)
    enqueuer.start(workers=8, max_queue_size=128)

    datas = enqueuer.get()

    for idx in range(len(dg_test)):
        yield next(datas)

    enqueuer.stop()

dataset = tf.data.Dataset.from_generator(test_generator,
    output_signature=(tf.TensorSpec(shape = (None, 448, 448, 3), dtype = tf.float32), 
        tf.TensorSpec(shape = (2,), dtype = tf.int32))
    )

dataset = dataset.batch(1)
dataset = dataset.prefetch(16)



dataset_stacks_predict = model.predict(dataset, verbose=True)


with open(csv_path_stacks.replace(".csv", "_25042022_prediction.pickle"), 'wb') as f:
    pickle.dump(dataset_stacks_predict, f)

print(classification_report(test_df["class"], np.argmax(dataset_stacks_predict[0], axis=1)))



class DatasetGenerator(tf.keras.utils.Sequence):
    def __init__(self, df):
        self.df = df

    def __len__(self):
        return len(self.df)
    
    def __getitem__(self, idx):
            
        row = self.df.loc[idx]
        img = row["image"]
        sev = row["class"]

        img_np = sitk.GetArrayFromImage(sitk.ReadImage(img))

        t, xs, ys, _ = img_np.shape
        xo = (xs - 448)//2
        yo = (ys - 448)//2
        img_np = img_np[:,xo:xo + 448, yo:yo + 448,:]
        
        one_hot = np.zeros(2)
        one_hot[sev] = 1

        return img_np, one_hot


csv_path_stacks = "/work/jprieto/data/remote/EGower/jprieto/feb_field_photos_missed_by_app.csv"

test_df = pd.read_csv(csv_path_stacks)
test_df["image"] = test_df["image"].replace({"TTScreenerFieldPhotos": "feb_field_photos_missed_by_app_seg_stack/TTScreenerFieldPhotos", ".jpg": ".nrrd"}, regex=True)
test_df['class'] = (test_df['class'] >= 1).astype(int)
print(test_df)

dg_test = DatasetGenerator(test_df)

def test_generator_0():

    enqueuer = OrderedEnqueuer(dg_test, use_multiprocessing=True)
    enqueuer.start(workers=8, max_queue_size=128)

    datas = enqueuer.get()

    for idx in range(len(dg_test)):
        yield next(datas)

    enqueuer.stop()

dataset = tf.data.Dataset.from_generator(test_generator_0,
    output_signature=(tf.TensorSpec(shape = (None, 448, 448, 3), dtype = tf.float32), 
        tf.TensorSpec(shape = (2,), dtype = tf.int32))
    )

dataset = dataset.batch(1)
dataset = dataset.prefetch(16)



dataset_stacks_predict = model.predict(dataset, verbose=True)

test_df["prediction"] = np.argmax(dataset_stacks_predict[0], axis=1)
test_df.to_csv(csv_path_stacks.replace(".csv", "_prediction.csv"), index=False)

with open(csv_path_stacks.replace(".csv", "_25042022_prediction.pickle"), 'wb') as f:
    pickle.dump(dataset_stacks_predict, f)

print(classification_report(test_df["class"], np.argmax(dataset_stacks_predict[0], axis=1)))


