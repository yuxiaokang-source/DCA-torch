import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.nn import Parameter
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
from .layers import ZINBLoss, MeanAct, DispAct
import numpy as np
from sklearn.cluster import KMeans
import math, os
from sklearn import metrics

### weight initization
def init_weights(m):
    """ initialize weights of fully connected layer
    """
    if type(m) == nn.Linear:
        nn.init.xavier_uniform_(m.weight)
        m.bias.data.fill_(0.01)

def buildNetwork(layers, type, activation="relu",batch_norm=False):
    net = []
    for i in range(1, len(layers)):
        net.append(nn.Linear(layers[i-1], layers[i]))
        if(batch_norm):
            net.append(nn.BatchNorm1d(layers[i],affine=False))
        if activation=="relu":
            net.append(nn.ReLU())
        elif activation=="sigmoid":
            net.append(nn.Sigmoid())
    return nn.Sequential(*net)

def euclidean_dist(x, y):
    return torch.sum(torch.square(x - y), dim=1)

class zinbAutoencoder(nn.Module):
    def __init__(self, input_dim, z_dim, encodeLayer=[], decodeLayer=[], 
            activation="relu", device="cuda"):
        super(zinbAutoencoder, self).__init__()
        self.z_dim = z_dim
        self.activation = activation
        self.device = device
        self.encoder = buildNetwork([input_dim]+encodeLayer, type="encode", activation=activation)
        #self.encoder.apply(init_weights)
        self.decoder = buildNetwork([z_dim]+decodeLayer, type="decode", activation=activation)
        #self.decoder.apply(init_weights)
        self._enc_mu = nn.Linear(encodeLayer[-1], z_dim)
        self._dec_mean = nn.Sequential(nn.Linear(decodeLayer[-1], input_dim), MeanAct())
        self._dec_disp = nn.Sequential(nn.Linear(decodeLayer[-1], input_dim), DispAct())
        self._dec_pi = nn.Sequential(nn.Linear(decodeLayer[-1], input_dim), nn.Sigmoid())

        self.zinb_loss = ZINBLoss().to(self.device)
        self.to(device)
    
    def save_model(self, path):
        torch.save(self.state_dict(), path)

    def load_model(self, path):
        pretrained_dict = torch.load(path, map_location=lambda storage, loc: storage)
        model_dict = self.state_dict()
        pretrained_dict = {k: v for k, v in pretrained_dict.items() if k in model_dict}
        model_dict.update(pretrained_dict) 
        self.load_state_dict(model_dict)


    def forwardAE(self, x):
        h = self.encoder(x)
        z = self._enc_mu(h)
        h = self.decoder(z)
        
        _mean = self._dec_mean(h)
        _disp = self._dec_disp(h)
        _pi = self._dec_pi(h)

        return z, _mean, _disp, _pi

    def forward(self, x):
        x = x.double()
        h = self.encoder(x)
        z = self._enc_mu(h)
        h = self.decoder(z)
        _mean = self._dec_mean(h)
        _disp = self._dec_disp(h)
        _pi = self._dec_pi(h)
        return z, _mean
    
    def imputeX(self,X,size_factor,batch_size = 256):
        self.eval()
        with torch.no_grad():
            imputed = []
            num = X.shape[0]
            num_batch = int(math.ceil(1.0*X.shape[0]/batch_size))
            for batch_idx in range(num_batch):
                xbatch = X[batch_idx*batch_size : min((batch_idx+1)*batch_size, num)]
                sfbatch = size_factor[batch_idx*batch_size : min((batch_idx+1)*batch_size, num)]
                inputs = Variable(xbatch).to(self.device).double()

                _, impute, _, _= self.forwardAE(inputs)
                imputed.append(impute.data* sfbatch[:,None])

            imputed = torch.cat(imputed, dim=0)
            return imputed.to(self.device)

    def encodeBatch(self, X, batch_size=256):
        self.eval()
        encoded = []
        num = X.shape[0]
        num_batch = int(math.ceil(1.0*X.shape[0]/batch_size))
        for batch_idx in range(num_batch):
            xbatch = X[batch_idx*batch_size : min((batch_idx+1)*batch_size, num)]
            inputs = Variable(xbatch).to(self.device).double()
            z, _, _, _= self.forwardAE(inputs)
            encoded.append(z.data)

        encoded = torch.cat(encoded, dim=0)
        return encoded.to(self.device)

    def fit(self, X, X_raw, size_factor, batch_size=256, lr=0.001, epochs=400, ae_save=True, ae_weights='AE_weights.pth.tar'):
        self.train()
        dataset = TensorDataset(torch.Tensor(X), torch.Tensor(X_raw), torch.Tensor(size_factor))
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
        print("Pretraining stage")
        #optimizer = optim.RMSprop(filter(lambda p: p.requires_grad, self.parameters()))
        optimizer = optim.Adam(filter(lambda p: p.requires_grad, self.parameters()), lr=lr, amsgrad=True)

        for epoch in range(epochs):
            loss_val = 0
            for batch_idx, (x_batch, x_raw_batch, sf_batch) in enumerate(dataloader):
                x_tensor = Variable(x_batch).to(self.device)
                x_raw_tensor = Variable(x_raw_batch).to(self.device)
                sf_tensor = Variable(sf_batch).to(self.device)
                _, mean_tensor, disp_tensor, pi_tensor = self.forwardAE(x_tensor)
                loss = self.zinb_loss(x=x_raw_tensor, mean=mean_tensor, disp=disp_tensor, pi=pi_tensor, scale_factor=sf_tensor)
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                loss_val += loss.item() * len(x_batch)
            print('Pretrain epoch %3d, ZINB loss: %.8f' % (epoch+1, loss_val/X.shape[0]))

        self.emb_X = self.encodeBatch(torch.Tensor(X))
        #self.imputeX = self.imputeX(torch.Tensor(X))
        self.imputeX = self.imputeX(torch.Tensor(X),torch.Tensor(size_factor))
        # if ae_save:
        #     torch.save({'ae_state_dict': self.state_dict(),
        #             'optimizer_state_dict': optimizer.state_dict()}, ae_weights)

    def save_checkpoint(self, state, index, filename):
        newfilename = os.path.join(filename, 'FTcheckpoint_%d.pth.tar' % index)
        torch.save(state, newfilename)

