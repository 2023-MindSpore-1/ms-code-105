import mindspore
from mindspore.common.parameter import Parameter
from mindspore.ops import functional as F
from mindspore import nn
import mindspore.ops as ops
import mindspore.numpy as np
from functools import partial

class EQLv2PCBLoss(nn.Cell):
    def __init__(self,
                 use_sigmoid=True,
                 reduction='mean',
                 class_weight=None,
                 loss_weight=1.0,
                 num_classes=1203,  # 1203 for lvis v1.0, 1230 for lvis v0.5
                 gamma=12,
                 mu=0.8,
                 alpha=4.0,
                 vis_grad=False,
                 momentum=0.99,
                 start_epoch=17,
                 alpha_pcb=0.0,
                 n_iter=3):
        super().__init__()
        self.use_sigmoid = True
        self.reduction = reduction
        self.loss_weight = loss_weight
        self.class_weight = class_weight
        self.num_classes = num_classes
        self.group = True

        # cfg for eqlv2
        self.vis_grad = vis_grad
        self.gamma = gamma
        self.mu = mu
        self.alpha = alpha

        # initial variables
        self._pos_grad = None
        self._neg_grad = None
        self.pos_neg = None

        def _func(x, gamma, mu):
            return 1 / (1 + np.exp(-gamma * (x - mu)))

        self.map_func = partial(_func, gamma=self.gamma, mu=self.mu)
        self.custom_cls_channels = True

        _epoch = ops.zeros(1, mindspore.float32)
        _epoch.requires_grad = False
        self._epoch = Parameter(_epoch, name='_epoch')
        # self.register_buffer('_epoch', _epoch)

        # collect confusion matrix for PCB
        fg_confusion_matrix = ops.zeros((self.num_classes, self.num_classes), mindspore.float32)
        fg_confusion_matrix.requires_grad = False
        # self.register_buffer('fg_confusion_matrix', fg_confusion_matrix)
        self.fg_confusion_matrix = Parameter(fg_confusion_matrix, name='fg_confusion_matrix')

        # collect the information of instance per class
        num_inst_cnt = ops.zeros((self.num_classes,), mindspore.float32)
        num_inst_cnt.requires_grad = False
        # self.register_buffer('num_inst_cnt', num_inst_cnt)
        self.num_inst_cnt = Parameter(num_inst_cnt, name='num_inst_cnt')

        self.momentum = momentum
        self.start_epoch = start_epoch
        self.alpha_pcb = alpha_pcb
        self.n_iter = n_iter

    def construct(self,
                cls_score,
                label,
                weight=None,
                avg_factor=None,
                reduction_override=None,
                iter_id=2,
                **kwargs):
        self.n_i, self.n_c = cls_score.size()

        self.gt_classes = label
        self.pred_class_logits = cls_score

        pos_inds = label < self.num_classes
        neg_inds = label == self.num_classes
        if pos_inds.sum() > 0:
            pred_fg_distri = F.sigmoid(cls_score[pos_inds, :self.num_classes])

            fg_confusion_matrix_tmp = ops.zeros_like(self.fg_confusion_matrix).scatter_add(label[pos_inds].view(-1,1).repeat(1, self.num_classes),
                                                                                            pred_fg_distri)

            fg_confusion_matrix_tmp_pool = [ops.zeros_like(fg_confusion_matrix_tmp) for i in
                                            range(1)]
            ops.AllGather(fg_confusion_matrix_tmp_pool, fg_confusion_matrix_tmp)
            fg_confusion_matrix_tmp = sum(fg_confusion_matrix_tmp_pool)

            num_inst_cnt_tmp = ops.zeros_like(self.num_inst_cnt).scatter_add(label[pos_inds],
                                                                             ops.Ones(pos_inds.sum()).to(
                                                                                 self.num_inst_cnt.device))

            num_inst_cnt_tmp_pool = [ops.zeros_like(num_inst_cnt_tmp) for i in
                                     range(1)]
            ops.AllGather(num_inst_cnt_tmp_pool, num_inst_cnt_tmp)
            num_inst_cnt_tmp = sum(num_inst_cnt_tmp_pool)

            fg_confusion_matrix_tmp[num_inst_cnt_tmp != 0] = fg_confusion_matrix_tmp[num_inst_cnt_tmp != 0] / \
                                                             num_inst_cnt_tmp[num_inst_cnt_tmp != 0].view(-1, 1)

            # Note: fix the BUG that update the non-appear classes in this batch
            self.fg_confusion_matrix[num_inst_cnt_tmp != 0] = self.fg_confusion_matrix[
                                                                  num_inst_cnt_tmp != 0] * self.momentum + \
                                                              fg_confusion_matrix_tmp[num_inst_cnt_tmp != 0] * (
                                                                      1 - self.momentum)

            # Note: one step to ensure that each row is 1-sum
            self.fg_confusion_matrix[num_inst_cnt_tmp != 0] /= self.fg_confusion_matrix[num_inst_cnt_tmp != 0].sum(1,
                                                                                                                   keepdim=True)

            self.num_inst_cnt = self.num_inst_cnt + num_inst_cnt_tmp

        def expand_label(pred, gt_classes):
            target = pred.new_zeros(self.n_i, self.n_c)
            target[ops.arange(self.n_i), gt_classes] = 1
            return target

        target = expand_label(cls_score, label)

        if self._epoch >= self.start_epoch and (self.num_inst_cnt == 0).sum() == 0:
            alpha_pcb = self.alpha_pcb / (self.n_iter - 1) * iter_id

            cm = self.fg_confusion_matrix.clone().detach()
            p_t = cm[:self.num_classes, label[pos_inds]].t()
            target[pos_inds, :self.num_classes] = p_t * alpha_pcb + target[pos_inds, :self.num_classes] * (
                        1 - alpha_pcb)

        pos_w, neg_w = self.get_weight(cls_score)

        weight = pos_w * target + neg_w * (1 - target)

        cls_loss = F.binary_cross_entropy_with_logits(cls_score, target, weight, pos_w,
                                                      reduction='none')
        cls_loss = np.sum(cls_loss * weight) / self.n_i

        self.collect_grad(cls_score.detach(), target.detach(), weight.detach())

        return self.loss_weight * cls_loss

    def get_cls_channels(self, num_classes):
        num_channel = num_classes + 1
        return num_channel

    def get_activation(self, cls_score):
        cls_score = ops.sigmoid(cls_score)
        n_i, n_c = cls_score.size()
        bg_score = cls_score[:, -1].view(n_i, 1)
        cls_score[:, :-1] *= (1 - bg_score)
        return cls_score

    def collect_grad(self, cls_score, target, weight):
        prob = ops.sigmoid(cls_score)
        grad = target * (prob - 1) + (1 - target) * prob
        grad = np.abs(grad)

        # do not collect grad for objectiveness branch [:-1]
        pos_grad = np.sum(grad * target * weight)[:-1]
        neg_grad = np.sum(grad * (1 - target) * weight)[:-1]
        self._pos_grad += pos_grad
        self._neg_grad += neg_grad
        self.pos_neg = self._pos_grad / (self._neg_grad + 1e-10)

    def get_weight(self, cls_score):
        # we do not have information about pos grad and neg grad at beginning
        if self._pos_grad is None:
            self._pos_grad = cls_score.new_zeros(self.num_classes)
            self._neg_grad = cls_score.new_zeros(self.num_classes)
            neg_w = cls_score.new_ones((self.n_i, self.n_c))
            pos_w = cls_score.new_ones((self.n_i, self.n_c))
        else:
            # the negative weight for objectiveness is always 1
            neg_w = ops.concat([self.map_func(self.pos_neg), cls_score.new_ones(1)])
            pos_w = 1 + self.alpha * (1 - neg_w)
            neg_w = neg_w.view(1, -1).expand(self.n_i, self.n_c)
            pos_w = pos_w.view(1, -1).expand(self.n_i, self.n_c)
        return pos_w, neg_w