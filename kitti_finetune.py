from __future__ import print_function 
import argparse
import os
import random
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
from torch.autograd import Variable
import torch.nn.functional as F
import skimage
import skimage.io
import skimage.transform
import numpy as np
import time
import math
import logging
from utils.common import load_loss_scheme, logger, formatter
from dataloader import KITTIloader2015 as ls
from dataloader import KITTILoader as DA

from networks.DispNetCSRes import DispNetCSRes
from losses.multiscaleloss import multiscaleloss

parser = argparse.ArgumentParser(description='PSMNet')
parser.add_argument('--maxdisp', type=int ,default=192,
                    help='maxium disparity')
parser.add_argument('--model', default='stackhourglass',
                    help='select model')
parser.add_argument('--datatype', default='2015',
                    help='datapath')
parser.add_argument('--datapath', default='/media/jiaren/ImageNet/data_scene_flow_2015/training/',
                    help='datapath')
parser.add_argument('--epochs', type=int, default=300,
                    help='number of epochs to train')
parser.add_argument('--loadmodel', default=None,
                    help='load model')
parser.add_argument('--savemodel', default='./',
                    help='save model')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='enables CUDA training')
parser.add_argument('--devices', type=str, help='indicates CUDA devices, e.g. 0,1,2', default='0')
parser.add_argument('--seed', type=int, default=1, metavar='S',
                    help='random seed (default: 1)')
parser.add_argument('--loss', type=str, help='indicates the loss scheme', default='simplenet_flying')
args = parser.parse_args()

if not os.path.exists(args.savemodel):
    os.makedirs(args.savemodel)

args.cuda = not args.no_cuda and torch.cuda.is_available()
torch.manual_seed(args.seed)
if args.cuda:
    torch.cuda.manual_seed(args.seed)

if args.datatype == '2015':
   from dataloader import KITTIloader2015 as ls
elif args.datatype == '2012':
   from dataloader import KITTIloader2012 as ls

all_left_img, all_right_img, all_left_disp, test_left_img, test_right_img, test_left_disp = ls.dataloader(args.datapath)

TrainImgLoader = torch.utils.data.DataLoader(
         DA.myImageFloder(all_left_img,all_right_img,all_left_disp, True), 
         batch_size= 32, shuffle= True, num_workers= 16, drop_last=False)

TestImgLoader = torch.utils.data.DataLoader(
         DA.myImageFloder(test_left_img,test_right_img,test_left_disp, False), 
         batch_size= 100, shuffle= False, num_workers= 4, drop_last=False)

devices = [int(item) for item in args.devices.split(',')]
ngpus = len(devices)

if args.model == 'dispnetcres':
    model = DispNetCSRes(ngpus, False, True)
else:
    logger.info('no model')

if args.cuda:
    model = nn.DataParallel(model, device_ids=devices)
    model.cuda()

if args.loadmodel is not None:
    state_dict = torch.load(args.loadmodel)
    model.load_state_dict(state_dict['state_dict'])

logger.info('Number of model parameters: {}'.format(sum([p.data.nelement() for p in model.parameters()])))

optimizer = optim.Adam(model.parameters(), lr=0.1, betas=(0.9, 0.999))

loss_json = load_loss_scheme(args.loss)
train_round = loss_json["round"]
loss_scale = loss_json["loss_scale"]
loss_weights = loss_json["loss_weights"]
criterion = multiscaleloss(loss_scale, 1, loss_weights[0], loss='L1', mask=True)

def train(imgL,imgR,disp_L):
        model.train()
        imgL   = Variable(torch.FloatTensor(imgL))
        imgR   = Variable(torch.FloatTensor(imgR))   
        disp_L = Variable(torch.FloatTensor(disp_L))

        if args.cuda:
            imgL, imgR, disp_true = imgL.cuda(), imgR.cuda(), disp_L.cuda()

        #---------
        mask = (disp_true > 0)
        mask.detach_()
        #----

        optimizer.zero_grad()
        
        if args.model == 'stackhourglass':
            output1, output2, output3 = model(imgL,imgR)
            output1 = torch.squeeze(output1,1)
            output2 = torch.squeeze(output2,1)
            output3 = torch.squeeze(output3,1)
            loss = 0.5*F.smooth_l1_loss(output1[mask], disp_true[mask], size_average=True) + 0.7*F.smooth_l1_loss(output2[mask], disp_true[mask], size_average=True) + F.smooth_l1_loss(output3[mask], disp_true[mask], size_average=True) 
        elif args.model == 'basic':
            output = model(imgL,imgR)
            output = torch.squeeze(output3,1)
            loss = F.smooth_l1_loss(output3[mask], disp_true[mask], size_average=True)
        elif args.model == 'dispnetcres':
            output_net1, output_net2 = model(torch.cat((imgL, imgR), 1))

            # multi-scale loss
            disp_true = disp_true.unsqueeze(1)
            loss_net1 = criterion(output_net1, disp_true)
            loss_net2 = criterion(output_net2, disp_true)
            loss = loss_net1 + loss_net2 

            # only the last scale
            #output1 = output_net1[0].squeeze(1)
            #output2 = output_net2[0].squeeze(1)
            #loss = 0.5*F.smooth_l1_loss(output1[mask], disp_true[mask], size_average=True) + F.smooth_l1_loss(output2[mask], disp_true[mask], size_average=True) 

        loss.backward()
        optimizer.step()

        return loss.data.item()

def test(imgL,imgR,disp_true):
        model.eval()
        imgL   = Variable(torch.FloatTensor(imgL))
        imgR   = Variable(torch.FloatTensor(imgR))   
        if args.cuda:
            imgL, imgR = imgL.cuda(), imgR.cuda()

        #logger.info(imgL.size())
        #imgL = F.pad(imgL, (0, 48, 0, 16), "constant", 0)
        #imgR = F.pad(imgR, (0, 48, 0, 16), "constant", 0)
        #logger.info(imgL.size())

        with torch.no_grad():
            output_net1, output_net2 = model(torch.cat((imgL, imgR), 1))

        pred_disp = output_net2.squeeze(1)
        pred_disp = pred_disp.data.cpu()
        #pred_disp = pred_disp[:, :368, :1232]

        #computing 3-px error#
        true_disp = disp_true.clone()
        index = np.argwhere(true_disp>0)
        disp_true[index[0][:], index[1][:], index[2][:]] = np.abs(true_disp[index[0][:], index[1][:], index[2][:]]-pred_disp[index[0][:], index[1][:], index[2][:]])
        correct = (disp_true[index[0][:], index[1][:], index[2][:]] < 3)|(disp_true[index[0][:], index[1][:], index[2][:]] < true_disp[index[0][:], index[1][:], index[2][:]]*0.05)      
        #torch.cuda.empty_cache()

        return 1-(float(torch.sum(correct))/float(len(index[0])))

def adjust_learning_rate(optimizer, epoch):
    if epoch <= 600:
       #lr = 5e-6
       lr = 1e-4*2
    elif epoch <= 1000:
       lr = 1e-5*5
    elif epoch <= 1500:
       lr = 1e-5*2
    elif epoch <= 2000:
       lr = 1e-5
    logger.info(lr)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def main():
	min_acc=1000
	min_epo=0
	start_full_time = time.time()

        # test on the loaded model
	total_test_loss = 0
        for batch_idx, (imgL, imgR, disp_L) in enumerate(TestImgLoader):
            test_loss = test(imgL,imgR, disp_L)
            logger.info('Iter %d 3-px error in val = %.3f' %(batch_idx, test_loss*100))
            total_test_loss += test_loss
        min_acc=total_test_loss/len(TestImgLoader)*100
	logger.info('MIN epoch %d total test error = %.3f' %(min_epo, min_acc))

	for epoch in range(1, args.epochs+1):
	   total_train_loss = 0
	   total_test_loss = 0
	   adjust_learning_rate(optimizer,epoch)
           
               ## training ##
           for batch_idx, (imgL_crop, imgR_crop, disp_crop_L) in enumerate(TrainImgLoader):
               start_time = time.time() 

               loss = train(imgL_crop,imgR_crop, disp_crop_L)
	       logger.info('Iter %d training loss = %.3f , time = %.2f' %(batch_idx, loss, time.time() - start_time))
	       total_train_loss += loss
	   logger.info('epoch %d total training loss = %.3f' %(epoch, total_train_loss/len(TrainImgLoader)))
	   
               ## Test ##

           for batch_idx, (imgL, imgR, disp_L) in enumerate(TestImgLoader):
               test_loss = test(imgL,imgR, disp_L)
               logger.info('Iter %d 3-px error in val = %.3f' %(batch_idx, test_loss*100))
               total_test_loss += test_loss


	   logger.info('epoch %d total 3-px error in val = %.3f' %(epoch, total_test_loss/len(TestImgLoader)*100))
	   if total_test_loss/len(TestImgLoader)*100 < min_acc:
		min_acc = total_test_loss/len(TestImgLoader)*100
		min_epo = epoch
	        savefilename = args.savemodel+'best.tar'
	        torch.save({
	              'epoch': epoch,
	              'state_dict': model.state_dict(),
	              'train_loss': total_train_loss/len(TrainImgLoader),
	              'test_loss': total_test_loss/len(TestImgLoader)*100,
	          }, savefilename)
	   logger.info('MIN epoch %d total test error = %.3f' %(min_epo, min_acc))

	   #SAVE
           if epoch % 100 == 0:
	       savefilename = args.savemodel+'finetune_'+str(epoch)+'.tar'
	       torch.save({
	             'epoch': epoch,
	             'state_dict': model.state_dict(),
	             'train_loss': total_train_loss/len(TrainImgLoader),
	             'test_loss': total_test_loss/len(TestImgLoader)*100,
	         }, savefilename)
	
        logger.info('full finetune time = %.2f HR' %((time.time() - start_full_time)/3600))
	logger.info(min_epo)
	logger.info(min_acc)


if __name__ == '__main__':
   hdlr = logging.FileHandler('logs/kitti_finetune.log')
   hdlr.setFormatter(formatter)
   logger.addHandler(hdlr) 
   main()