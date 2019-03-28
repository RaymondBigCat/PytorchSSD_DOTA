from __future__ import print_function

import argparse
import pickle
import time

import numpy as np
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "1" #设置GPU1可见
import torch
import torch.backends.cudnn as cudnn
import torch.nn.init as init
import torch.optim as optim
import torch.utils.data as data
from torch.autograd import Variable
import sys
from data import VOCroot, COCOroot, VOC_300, VOC_512, COCO_300, COCO_512, COCO_mobile_300, AnnotationTransform, \
    COCODetection, VOCDetection, detection_collate, BaseTransform, preproc, DOTADetection, DOTAroot,DotaAnnTrans
from layers.functions import Detect, PriorBox
from layers.modules import MultiBoxLoss
from utils.nms_wrapper import nms
from utils.timer import Timer


def str2bool(v):
    return v.lower() in ("yes", "true", "t", "1")


parser = argparse.ArgumentParser(
    description='Receptive Field Block Net Training')
parser.add_argument('-v', '--version', default='RFB_vgg',
                    help='RFB_vgg ,RFB_E_vgg RFB_mobile SSD_vgg version.')
parser.add_argument('-s', '--size', default='300',
                    help='300 or 512 input size.')
parser.add_argument('-d', '--dataset', default='DOTA',
                    help='VOC or COCO or DOTA dataset (Default is DOTA)')
parser.add_argument(
    '--basenet', default='weights/vgg16_reducedfc.pth', help='pretrained base model')
parser.add_argument('--jaccard_threshold', default=0.5,
                    type=float, help='Min Jaccard index for matching')
parser.add_argument('-b', '--batch_size', default=16,
                    type=int, help='Batch size for training')
parser.add_argument('--num_workers', default=4,
                    type=int, help='Number of workers used in dataloading')
parser.add_argument('--cuda', default=True,
                    type=bool, help='Use cuda to train model')
parser.add_argument('--ngpu', default=1, type=int, help='gpus')
parser.add_argument('--lr', '--learning-rate',
                    default=4e-3, type=float, help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, help='momentum')

parser.add_argument('--resume_net', default=False, help='resume net for retraining')
parser.add_argument('--resume_epoch', default=0,
                    type=int, help='resume iter for retraining')

parser.add_argument('-max', '--max_epoch', default=300,
                    type=int, help='max epoch for retraining')
parser.add_argument('--weight_decay', default=5e-4,
                    type=float, help='Weight decay for SGD')
parser.add_argument('-we', '--warm_epoch', default=1,
                    type=int, help='max epoch for retraining')
parser.add_argument('--gamma', default=0.1,
                    type=float, help='Gamma update for SGD')
parser.add_argument('--log_iters', default=True,
                    type=bool, help='Print the loss at each iteration')
parser.add_argument('--save_folder', default='weights/',
                    help='Location to save checkpoint models')
parser.add_argument('--date', default='1213')
parser.add_argument('--save_frequency', default=10)
parser.add_argument('--retest', default=False, type=bool,
                    help='test cache results')
parser.add_argument('--test_frequency', default=10)
parser.add_argument('--visdom', default=False, type=str2bool, help='Use visdom to for loss visualization')
parser.add_argument('--send_images_to_visdom', type=str2bool, default=False,
                    help='Sample a random image from each 10th batch, send it to visdom after augmentations step')
args = parser.parse_args()

save_folder = os.path.join(args.save_folder, args.version + '_' + args.size, args.date)
if not os.path.exists(save_folder):
    os.makedirs(save_folder)
test_save_dir = os.path.join(save_folder, 'ss_predict')
if not os.path.exists(test_save_dir):
    os.makedirs(test_save_dir)

log_file_path = save_folder + '/train' + time.strftime('_%Y-%m-%d-%H-%M', time.localtime(time.time())) + '.log'

"""
输入进行train的数据集
"""  
# add DOTA dataset
if args.dataset == 'DOTA':
    train_sets = [('train_test')]
    cfg = (VOC_300, VOC_512)[args.size == '512']
"""
选择网络
"""
if args.version == 'RFB_vgg':
    from models.RFB_Net_vgg import build_net
elif args.version == 'RFB_E_vgg':
    from models.RFB_Net_E_vgg import build_net
elif args.version == 'RFB_mobile':
    from models.RFB_Net_mobile import build_net

    cfg = COCO_mobile_300
elif args.version == 'SSD_vgg':
    from models.SSD_vgg import build_net
elif args.version == 'FSSD_vgg':
    from models.FSSD_vgg import build_net
elif args.version == 'FRFBSSD_vgg':
    from models.FRFBSSD_vgg import build_net
else:
    print('Unkown version!')

"""
归一化参数
"""
rgb_std = (1, 1, 1)
img_dim = (300, 512)[args.size == '512']
if 'vgg' in args.version:
    rgb_means = (104, 117, 123)
elif 'mobile' in args.version:
    rgb_means = (103.94, 116.78, 123.68)

p = (0.6, 0.2)[args.version == 'RFB_mobile']
"""
选择总类别数
"""
if args.dataset == 'DOTA':
    num_classes = 2
"""
超参数
"""
batch_size = args.batch_size
weight_decay = 0.0005
gamma = 0.1
momentum = 0.9
"""
visdom可视化部分
"""
if args.visdom:
    import visdom

    viz = visdom.Visdom()
"""
生成网络,models/SSD_vgg.py
net是nn.Module类的一个实例
"""
net = build_net(img_dim, num_classes)
print(net)

"""
如果不需要恢复训练，则开始从头训练
"""
if not args.resume_net:
    # 加载预训练模型
    base_weights = torch.load(args.basenet)
    print('Loading base network...')
    # 向网络输入预训练模型的参数
    net.base.load_state_dict(base_weights)


    def xavier(param):
        init.xavier_uniform(param)

    #解析预训练模型权值
    def weights_init(m):
        for key in m.state_dict():
            if key.split('.')[-1] == 'weight':
                if 'conv' in key:
                    init.kaiming_normal(m.state_dict()[key], mode='fan_out')
                if 'bn' in key:
                    m.state_dict()[key][...] = 1
            elif key.split('.')[-1] == 'bias':
                m.state_dict()[key][...] = 0


    print('Initializing weights...')
    # initialize newly added layers' weights with kaiming_normal method
    # .extras,.loc,.conf都是nn.ModuleList
    net.extras.apply(weights_init)
    net.loc.apply(weights_init)
    net.conf.apply(weights_init)
    if args.version == 'FSSD_vgg' or args.version == 'FRFBSSD_vgg':
        net.ft_module.apply(weights_init)
        net.pyramid_ext.apply(weights_init)
    if 'RFB' in args.version:
        net.Norm.apply(weights_init)
    if args.version == 'RFB_E_vgg':
        net.reduce.apply(weights_init)
        net.up_reduce.apply(weights_init)
else: #恢复训练
    # load resume network
    resume_net_path = os.path.join(save_folder, args.version + '_' + args.dataset + '_epoches_' + \
                                   str(args.resume_epoch) + '.pth')
    print('Loading resume network', resume_net_path)
    state_dict = torch.load(resume_net_path)
    # create new OrderedDict that does not contain `module.`
    from collections import OrderedDict

    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        head = k[:7]
        if head == 'module.':
            name = k[7:]  # remove `module.`
        else:
            name = k
        new_state_dict[name] = v
    net.load_state_dict(new_state_dict)

# 支持多GPU
if args.ngpu > 1:
    net = torch.nn.DataParallel(net, device_ids=list(range(args.ngpu)))
# 支持GPU加速
if args.cuda:
    net.cuda()
    cudnn.benchmark = True

detector = Detect(num_classes, 0, cfg)
optimizer = optim.SGD(net.parameters(), lr=args.lr,
                      momentum=args.momentum, weight_decay=args.weight_decay)
# optimizer = optim.RMSprop(net.parameters(), lr=args.lr,alpha = 0.9, eps=1e-08,
#                      momentum=args.momentum, weight_decay=args.weight_decay)

criterion = MultiBoxLoss(num_classes, 0.5, True, 0, True, 3, 0.5, False)
priorbox = PriorBox(cfg)
priors = Variable(priorbox.forward(), volatile=True)
# dataset
print('Loading Dataset...')
if args.dataset == 'VOC':
    testset = VOCDetection(
        VOCroot, [('2007', 'test')], None, AnnotationTransform())
    train_dataset = VOCDetection(VOCroot, train_sets, preproc(
        img_dim, rgb_means, rgb_std, p), AnnotationTransform())
elif args.dataset == 'COCO':
    testset = COCODetection(
        COCOroot, [('2017', 'val')], None)
    train_dataset = COCODetection(COCOroot, train_sets, preproc(
        img_dim, rgb_means, rgb_std, p))
elif args.dataset == 'DOTA':
    train_dataset = DOTADetection(DOTAroot, image_sets='splittest',preproc=preproc(
        img_dim, rgb_means, rgb_std, p), target_transform=DotaAnnTrans(),catNms=['plane', 'small-vehicle', 'large-vehicle', 'ship'])
else:
    print('Only VOC and COCO are supported now!')
    print('Now, DOTA is supported.')
    exit()


def train():
    net.train()
    # loss counters
    epoch = 0
    if args.resume_net:
        epoch = 0 + args.resume_epoch
    epoch_size = len(train_dataset) // args.batch_size
    max_iter = args.max_epoch * epoch_size

    stepvalues_VOC = (150 * epoch_size, 200 * epoch_size, 250 * epoch_size)
    stepvalues_COCO = (90 * epoch_size, 120 * epoch_size, 140 * epoch_size)
    stepvalues = (stepvalues_VOC, stepvalues_COCO)[args.dataset == 'COCO']
    print('Training', args.version, 'on', train_dataset.name)
    step_index = 0

    if args.visdom:
        # initialize visdom loss plot
        lot = viz.line(
            X=torch.zeros((1,)).cpu(),
            Y=torch.zeros((1, 3)).cpu(),
            opts=dict(
                xlabel='Iteration',
                ylabel='Loss',
                title='Current SSD Training Loss',
                legend=['Loc Loss', 'Conf Loss', 'Loss']
            )
        )
        epoch_lot = viz.line(
            X=torch.zeros((1,)).cpu(),
            Y=torch.zeros((1, 3)).cpu(),
            opts=dict(
                xlabel='Epoch',
                ylabel='Loss',
                title='Epoch SSD Training Loss',
                legend=['Loc Loss', 'Conf Loss', 'Loss']
            )
        )
    if args.resume_epoch > 0:
        start_iter = args.resume_epoch * epoch_size
    else:
        start_iter = 0

    log_file = open(log_file_path, 'w')
    batch_iterator = None
    mean_loss_c = 0
    mean_loss_l = 0
    epoch_time = 0
    for iteration in range(start_iter, max_iter + 10):
        if (iteration % epoch_size == 0):
            # create batch iterator
            # 当上一个epoch全部训练完后
            # 生成一个新的epoch（迭代器）（所有图；乱序）
            batch_iterator = iter(data.DataLoader(train_dataset, batch_size,
                                                  shuffle=True, num_workers=args.num_workers,
                                                  collate_fn=detection_collate))
            loc_loss = 0
            conf_loss = 0
            # epoch_time
            print('Epoch time: %.4f sec'%(epoch_time))
            epoch_time = 0
            if epoch % args.save_frequency == 0 and epoch > 0:
                torch.save(net.state_dict(), os.path.join(save_folder, args.version + '_' + args.dataset + '_epoches_' +
                                                          repr(epoch) + '.pth'))
            # 一个epoch训练完后，测试网络
            if epoch % args.test_frequency == 0 and epoch > 0:
                net.eval()
                top_k = (300, 200)[args.dataset == 'COCO']
                if args.dataset == 'VOC':
                    APs, mAP = test_net(test_save_dir, net, detector, args.cuda, testset,
                                        BaseTransform(net.module.size, rgb_means, rgb_std, (2, 0, 1)),
                                        top_k, thresh=0.01)
                    APs = [str(num) for num in APs]
                    mAP = str(mAP)
                    log_file.write(str(iteration) + ' APs:\n' + '\n'.join(APs))
                    log_file.write('mAP:\n' + mAP + '\n')
                else:
                    """
                    test_net(test_save_dir, net, detector, args.cuda, testset,
                             BaseTransform(net.module.size, rgb_means, rgb_std, (2, 0, 1)),
                             top_k, thresh=0.01)
                    """
                    pass
                net.train()
            epoch += 1

        load_t0 = time.time()
        if iteration in stepvalues:
            step_index = stepvalues.index(iteration) + 1
            if args.visdom:
                viz.line(
                    X=torch.ones((1, 3)).cpu() * epoch,
                    Y=torch.Tensor([mean_loss_l, mean_loss_c,
                                    mean_loss_l + mean_loss_c]).unsqueeze(0).cpu() / epoch_size,
                    win=epoch_lot,
                    update='append'
                )
        lr = adjust_learning_rate(optimizer, args.gamma, epoch, step_index, iteration, epoch_size)

        # load train data
        images, targets = next(batch_iterator)

        # print(np.sum([torch.sum(anno[:,-1] == 2) for anno in targets]))

        if args.cuda:
            images = Variable(images.cuda())
            targets = [Variable(anno.cuda(), volatile=True) for anno in targets]
        else:
            images = Variable(images)
            targets = [Variable(anno, volatile=True) for anno in targets]
        # forward
        out = net(images)
        # backprop
        optimizer.zero_grad()
        # arm branch loss
        loss_l, loss_c = criterion(out, priors, targets)
        # odm branch loss

        mean_loss_c += loss_c.data[0]
        mean_loss_l += loss_l.data[0]

        loss = loss_l + loss_c
        loss.backward()
        optimizer.step()
        load_t1 = time.time()
        epoch_time += load_t1 - load_t0
        if iteration % 10 == 0:
            print('Epoch:' + repr(epoch) + ' || epochiter: ' + repr(iteration % epoch_size) + '/' + repr(epoch_size)
                  + '|| Totel iter ' +
                  repr(iteration) + ' || L: %.4f C: %.4f||' % (
                      mean_loss_l / 10, mean_loss_c / 10) +
                  'Batch time: %.4f sec. ||' % (load_t1 - load_t0) + 'LR: %.8f' % (lr))
            log_file.write(
                'Epoch:' + repr(epoch) + ' || epochiter: ' + repr(iteration % epoch_size) + '/' + repr(epoch_size)
                + '|| Totel iter ' +
                repr(iteration) + ' || L: %.4f C: %.4f||' % (
                    mean_loss_l / 10, mean_loss_c / 10) +
                'Batch time: %.4f sec. ||' % (load_t1 - load_t0) + 'LR: %.8f' % (lr) + '\n')

            mean_loss_c = 0
            mean_loss_l = 0
            if args.visdom and args.send_images_to_visdom:
                random_batch_index = np.random.randint(images.size(0))
                viz.image(images.data[random_batch_index].cpu().numpy())
    log_file.close()
    torch.save(net.state_dict(), os.path.join(save_folder,
                                              'Final_' + args.version + '_' + args.dataset + '.pth'))


def adjust_learning_rate(optimizer, gamma, epoch, step_index, iteration, epoch_size):
    """Sets the learning rate
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    if epoch < args.warm_epoch:
        lr = 1e-6 + (args.lr - 1e-6) * iteration / (epoch_size * args.warm_epoch)
    else:
        lr = args.lr * (gamma ** (step_index))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


if __name__ == '__main__':
    train()
