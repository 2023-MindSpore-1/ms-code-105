import mindspore
from mindspore import Tensor
from mindspore.common.parameter import Parameter
from mindspore.ops import functional as F
from mindspore import nn
import mindspore.ops as ops
import mindspore.numpy as np


def nonzero(input):
    """Return indexes of all nonzero/True elements.
    """
    temp = Tensor(np.zeros(input.shape))
    if input.dtype == mindspore.bool_:
        for ri in input:
            for ci in ri:
                if ci == True:
                    temp[ri.index_of_parent_, ci.index_of_parent_] = 1
                else:
                    temp[ri.index_of_parent_, ci.index_of_parent_] = 0
    else:
        temp = input
    output = Tensor(np.zeros((ops.count_nonzero(temp).asnumpy().item(), 2)))

    r = -1
    ind = -1
    for rows in temp:
        c = -1
        r = r + 1
        for elem in rows:
            c = c + 1
            if elem != 0:
                ind = ind + 1
                output[ind, :] = Tensor([r, c])
    return output


def reduce_loss(loss, reduction):
    """Reduce loss as specified.
    Args:
        loss (Tensor): Elementwise loss tensor.
    Return:
        Tensor: Reduced loss tensor.
    """
    if reduction == "mean":
        return loss.mean()
    elif reduction == "sum":
        return loss.sum()
    return loss


def weight_reduce_loss(loss, weight=None, reduction='mean', avg_factor=None):
    if weight is not None:
        loss = loss * weight
    # if avg_factor is not specified, just reduce the loss
    if avg_factor is None:
        loss = reduce_loss(loss,  reduction)
    else:
        # if reduction is mean, then average the loss by avg_factor
        if reduction == 'mean':
            loss = loss.sum() / avg_factor
        # if reduction is 'none', then do nothing, otherwise raise an error
        elif reduction != 'none':
            raise ValueError('avg_factor can not be used with reduction="sum"')
    return loss

def cross_entropy(pred,
                  label,
                  weight=None,
                  reduction='mean',
                  avg_factor=None,
                  class_weight=None,
                  ignore_index=-100):
    """Calculate the CrossEntropy loss.
    Args:
        pred (torch.Tensor): The prediction with shape (N, C), C is the number
            of classes.
        label (torch.Tensor): The learning label of the prediction.
        weight (torch.Tensor, optional): Sample-wise loss weight.
        reduction (str, optional): The method used to reduce the loss.
        avg_factor (int, optional): Average factor that is used to average
            the loss. Defaults to None.
        class_weight (list[float], optional): The weight for each class.
        ignore_index (int | None): The label index to be ignored.
            If None, it will be set to default value. Default: -100.
    Returns:
        torch.Tensor: The calculated loss
    """
    # The default value of ignore_index is the same as F.cross_entropy
    ignore_index = -100 if ignore_index is None else ignore_index
    # element-wise losses
    loss = F.cross_entropy(
        pred,
        label,
        weight=class_weight,
        reduction='none',
        ignore_index=ignore_index)

    # apply weights and do the reduction
    if weight is not None:
        weight = weight.float()
    loss = weight_reduce_loss(
        loss, weight=weight, reduction=reduction, avg_factor=avg_factor)

    return loss


def _expand_onehot_labels(labels, label_weights, label_channels, ignore_index):
    """Expand onehot labels to match the size of prediction."""
    bin_labels = labels.new_full((labels.size(0), label_channels), 0)
    valid_mask = (labels >= 0) & (labels != ignore_index)
    inds = nonzero(
        valid_mask & (labels < label_channels))

    if inds.numel() > 0:
        bin_labels[inds, labels[inds]] = 1

    valid_mask = valid_mask.view(-1, 1).expand(labels.size(0),
                                               label_channels).float()
    if label_weights is None:
        bin_label_weights = valid_mask
    else:
        bin_label_weights = label_weights.view(-1, 1).repeat(1, label_channels)
        bin_label_weights *= valid_mask

    return bin_labels, bin_label_weights


def binary_cross_entropy(pred,
                         label,
                         weight=None,
                         reduction='mean',
                         avg_factor=None,
                         class_weight=None,
                         ignore_index=-100):

    # The default value of ignore_index is the same as F.cross_entropy
    ignore_index = -100 if ignore_index is None else ignore_index
    if pred.dim() != label.dim():
        label, weight = _expand_onehot_labels(label, weight, pred.size(-1),
                                              ignore_index)

    # weighted element-wise losses
    if weight is not None:
        weight = weight.float()
    loss = F.binary_cross_entropy_with_logits(
        pred, label.float(), pos_weight=class_weight, reduction='none')
    # do the reduction for the weighted loss
    loss = weight_reduce_loss(
        loss, weight, reduction=reduction, avg_factor=avg_factor)

    return loss


def mask_cross_entropy(pred,
                       target,
                       label,
                       reduction='mean',
                       avg_factor=None,
                       class_weight=None,
                       ignore_index=None):
    """Calculate the CrossEntropy loss for masks.
    Args:
        pred (torch.Tensor): The prediction with shape (N, C, *), C is the
            number of classes. The trailing * indicates arbitrary shape.
        target (torch.Tensor): The learning label of the prediction.
        label (torch.Tensor): ``label`` indicates the class label of the mask
            corresponding object. This will be used to select the mask in the
            of the class which the object belongs to when the mask prediction
            if not class-agnostic.
        reduction (str, optional): The method used to reduce the loss.
            Options are "none", "mean" and "sum".
        avg_factor (int, optional): Average factor that is used to average
            the loss. Defaults to None.
        class_weight (list[float], optional): The weight for each class.
        ignore_index (None): Placeholder, to be consistent with other loss.
            Default: None.
    Returns:
        torch.Tensor: The calculated loss
    Example:
        >>> N, C = 3, 11
        >>> H, W = 2, 2
        >>> pred = torch.randn(N, C, H, W) * 1000
        >>> target = torch.rand(N, H, W)
        >>> label = torch.randint(0, C, size=(N,))
        >>> reduction = 'mean'
        >>> avg_factor = None
        >>> class_weights = None
        >>> loss = mask_cross_entropy(pred, target, label, reduction,
        >>>                           avg_factor, class_weights)
        >>> assert loss.shape == (1,)
    """
    assert ignore_index is None, 'BCE loss does not support ignore_index'
    # TODO: handle these two reserved arguments
    assert reduction == 'mean' and avg_factor is None
    num_rois = pred.size()[0]
    inds = mindspore.numpy.arange(0, num_rois, dtype=Tensor.long)
    pred_slice = pred[inds, label].squeeze(1)
    return F.binary_cross_entropy_with_logits(
        pred_slice, target, weight=class_weight, reduction='mean')[None]


class CrossEntropyPCBLoss(nn.Cell):

    def __init__(self,
                 use_sigmoid=False,
                 use_mask=False,
                 reduction='mean',
                 class_weight=None,
                 ignore_index=None,
                 loss_weight=1.0,
                 momentum=0.99,
                 start_epoch=17,
                 alpha=0.0,
                 num_classes=1230,
                 custom_cls_channels=False,
                 n_iter=3):
        """CrossEntropyLoss.
        Args:
            use_sigmoid (bool, optional): Whether the prediction uses sigmoid
                of softmax. Defaults to False.
            use_mask (bool, optional): Whether to use mask cross entropy loss.
                Defaults to False.
            reduction (str, optional): . Defaults to 'mean'.
                Options are "none", "mean" and "sum".
            class_weight (list[float], optional): Weight of each class.
                Defaults to None.
            ignore_index (int | None): The label index to be ignored.
                Defaults to None.
            loss_weight (float, optional): Weight of the loss. Defaults to 1.0.
        """
        super(CrossEntropyPCBLoss, self).__init__()
        assert (use_sigmoid is False) or (use_mask is False)
        self.use_sigmoid = use_sigmoid
        self.use_mask = use_mask
        self.reduction = reduction
        self.loss_weight = loss_weight
        self.class_weight = class_weight
        self.ignore_index = ignore_index

        if self.use_sigmoid:
            self.cls_criterion = binary_cross_entropy
        elif self.use_mask:
            self.cls_criterion = mask_cross_entropy
        else:
            self.cls_criterion = cross_entropy

        self.num_classes = num_classes

        # record epoch
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
        self.alpha = alpha
        self.custom_cls_channels = custom_cls_channels
        self.n_iter = n_iter
        self.scatter_add = ops.TensorScatterAdd()

    def construct(self,
                cls_score,
                label,
                weight=None,
                avg_factor=None,
                reduction_override=None,
                ignore_index=None,
                iter_id=2,
                **kwargs):
        print(type(label))
        assert reduction_override in (None, 'none', 'mean', 'sum')
        reduction = (
            reduction_override if reduction_override else self.reduction)
        if ignore_index is None:
            ignore_index = self.ignore_index

        if self.class_weight is not None:
            class_weight = cls_score.new_tensor(
                self.class_weight, device=cls_score.device)
        else:
            class_weight = None

        pos_inds = label < self.num_classes
        neg_inds = label == self.num_classes
        if pos_inds.sum() > 0:
            pred_fg_distri = ops.softmax(cls_score[pos_inds, :self.num_classes])
            print(type(label))
            label_pos_inds = Tensor(label).asnumpy()
            label_pos_inds = label_pos_inds[pos_inds]
            fg_confusion_matrix_tmp = ops.zeros_like(self.fg_confusion_matrix).scatter_add_(0,
                                                                                              label[pos_inds].view(-1,
                                                                                                                   1).repeat(
                                                                                                  1, self.num_classes),
                                                                                              pred_fg_distri)

            fg_confusion_matrix_tmp_pool = [ops.zeros_like(fg_confusion_matrix_tmp) for i in
                                            range(1)]
            ops.AllGather(fg_confusion_matrix_tmp_pool, fg_confusion_matrix_tmp)
            fg_confusion_matrix_tmp = sum(fg_confusion_matrix_tmp_pool)

            num_inst_cnt_tmp = ops.zeros_like(self.num_inst_cnt).scatter_add(label_pos_inds.astype('int32'),
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

        if self._epoch >= self.start_epoch and (self.num_inst_cnt == 0).sum() == 0:
            # the requirement is satisfied and the PCB regularization is to compute
            alpha = self.alpha / (self.n_iter - 1) * iter_id

            loss_cls_objectness = self.cls_criterion(
                cls_score[neg_inds],
                label[neg_inds],
                weight[neg_inds],
                class_weight=class_weight,
                reduction=reduction,
                avg_factor=avg_factor,
                ignore_index=ignore_index,
                **kwargs)
            loss_cls_classes = self.cls_criterion(
                cls_score[pos_inds],
                label[pos_inds],
                weight[pos_inds],
                class_weight=class_weight,
                reduction=reduction,
                avg_factor=avg_factor,
                ignore_index=ignore_index,
                **kwargs) * (1 - alpha)

            # C x C (GT vs. Predict)
            cm = self.fg_confusion_matrix.clone().detach()
            cm /= cm.sum(0, keepdim=True)
            p_t = cm[:self.num_classes, label[pos_inds]].t()

            # append a dummy background prediction to compute the PCB
            dummy_probs = p_t.new_zeros(p_t.size(0), 1)
            p_t = ops.Concat([p_t, dummy_probs], dim=1)

            p_s = nn.LogSoftmax(cls_score[pos_inds, :])

            # KL or CE is the same to p_s from the perspective of gradient
            loss_kl_classes = F.kl_div(p_s, p_t, reduction='sum') / len(label) * alpha

            loss_cls = (loss_cls_objectness + loss_cls_classes + loss_kl_classes) * self.loss_weight

        else:
            loss_cls = self.loss_weight * self.cls_criterion(
                cls_score,
                label,
                weight,
                class_weight=class_weight,
                reduction=reduction,
                avg_factor=avg_factor,
                ignore_index=ignore_index,
                **kwargs)
        return loss_cls

    def get_activation(self, cls_score):
        if cls_score is None:
            return None
        if self.use_sigmoid:
            scores = F.sigmoid(cls_score)
            dummpy_prob = scores.new_zeros((scores.size(0), 1))
            scores = ops.Concat([scores, dummpy_prob], dim=1)
        else:
            scores = ops.Softmax(cls_score, dim=1)
        return scores

    def get_cls_channels(self, num_classes):
        if self.use_sigmoid:
            return num_classes
        return num_classes + 1
