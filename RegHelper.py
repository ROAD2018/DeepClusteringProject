from pprint import pprint

from torch.distributions import Beta

__all__ = ["VATModuleInterface", "MixUp"]

import contextlib
from typing import Callable, Union, Dict, Tuple

import torch
import torch.nn as nn
from deepclustering.loss.IID_losses import IIDLoss
from deepclustering.loss.loss import KL_div
from deepclustering.model import Model
from deepclustering.utils import simplex, _warnings, nice_dict, assert_list
from torch import Tensor


@contextlib.contextmanager
def _disable_tracking_bn_stats(model):
    def switch_attr(m):
        if hasattr(m, "track_running_stats"):
            m.track_running_stats ^= True

    # let the track_running_stats to be inverse
    model.apply(switch_attr)
    # return the model
    yield
    # let the track_running_stats to be inverse
    model.apply(switch_attr)


def _l2_normalize(d: torch.Tensor) -> torch.Tensor:
    d_reshaped = d.view(d.shape[0], -1, *(1 for _ in range(d.dim() - 2)))
    d /= torch.norm(d_reshaped, dim=1, keepdim=True)  # + 1e-8
    ones_ = torch.ones(d.shape[0], device=d.device)
    assert torch.allclose(d.view(d.shape[0], -1).norm(dim=1), ones_, rtol=1e-3)
    return d


class VATLoss(nn.Module):
    def __init__(self, xi=10.0, eps=1.0, prop_eps=0.25, ip=1, distance_func=KL_div(reduce=True)):
        """VAT loss
        :param xi: hyperparameter of VAT (default: 10.0)
        :param eps: hyperparameter of VAT (default: 1.0)
        :param ip: iteration times of computing adv noise (default: 1)
        """
        super(VATLoss, self).__init__()
        self.xi = xi
        self.eps = eps
        self.ip = ip
        self.prop_eps = prop_eps
        self.distance_func = distance_func

    def forward(self, model, x: torch.Tensor,**kwargs):
        """
        We support the output of the model would be a simplex.
        :param model:
        :param x:
        :return:
        """
        with torch.no_grad():
            pred = model(x)[0]
        assert simplex(pred)

        # prepare random unit tensor
        d = torch.randn_like(x, device=x.device)
        d = _l2_normalize(d)

        with _disable_tracking_bn_stats(model):
            # calc adversarial direction
            for _ in range(self.ip):
                d.requires_grad_()
                pred_hat = model(x + self.xi * d)
                adv_distance = self.distance_func(pred_hat, pred)
                adv_distance.backward()
                d = _l2_normalize(d.grad)

            # calc LDS
            if isinstance(self.eps, torch.Tensor):
                # a dictionary is given
                bn, *shape = x.shape
                basic_view_shape: Tuple[int, ...] = (bn, *([1] * len(shape)))
                r_adv = d * self.eps.view(basic_view_shape).expand_as(d) * self.prop_eps
            elif isinstance(self.eps, (float, int)):
                r_adv = d * self.eps * self.prop_eps
            else:
                raise NotImplementedError(f"eps should be tensor or float, given {self.eps}.")

            pred_hat = model(x + r_adv)[0]
            lds = self.distance_func(pred_hat, pred)

        return lds, (x + r_adv).detach(), r_adv.detach()


class VATLoss_Multihead(nn.Module):
    """
    this is the VAT for the multihead networks. each head outputs a simplex.
    """

    def __init__(self, xi=10.0, eps=1.0, prop_eps=0.25, ip=1, distance_func=KL_div(reduce=True)):
        """VAT loss
        :param xi: hyperparameter of VAT (default: 10.0)
        :param eps: hyperparameter of VAT (default: 1.0)
        :param ip: iteration times of computing adv noise (default: 1)
        """
        super(VATLoss_Multihead, self).__init__()
        self.xi = xi
        self.eps = eps
        self.ip = ip
        self.prop_eps = prop_eps
        self.distance_func = distance_func

    def forward(self, model: Model, x: torch.Tensor,**kwargs):
        with torch.no_grad():
            pred = model(x,**kwargs)
        assert assert_list(simplex, pred), f"pred should be a list of simplex."

        # prepare random unit tensor
        d = torch.randn_like(x, device=x.device)
        d = _l2_normalize(d)

        with _disable_tracking_bn_stats(model):
            # calc adversarial direction
            for _ in range(self.ip):
                d.requires_grad_()
                pred_hat = model(x + self.xi * d,**kwargs)
                assert assert_list(simplex, pred_hat)
                # here the pred_hat is the list of simplex
                adv_distance = list(map(lambda p_, p: self.distance_func(p_, p), pred_hat, pred))
                _adv_distance: torch.Tensor = sum(adv_distance) / float(len(adv_distance))
                _adv_distance.backward()  # type: ignore
                d = _l2_normalize(d.grad)

            # calc LDS
            if isinstance(self.eps, torch.Tensor):
                # a dictionary is given
                bn, *shape = x.shape
                basic_view_shape: Tuple[int, ...] = (bn, *([1] * len(shape)))
                r_adv = d * self.eps.view(basic_view_shape).expand_as(d) * self.prop_eps
            elif isinstance(self.eps, (float, int)):
                r_adv = d * self.eps * self.prop_eps
            else:
                raise NotImplementedError(f"eps should be tensor or float, given {self.eps}.")

            pred_hat = model(x + r_adv,**kwargs)
            assert assert_list(simplex, pred_hat)
            lds =  list(map(lambda p_, p: self.distance_func(p_, p), pred_hat, pred))  # type: ignore
            lds: torch.Tensor = sum(lds) / float(len(lds))

        return lds, (x + r_adv).detach(), r_adv.detach()


def VATModuleInterface(params: Dict[str, Union[str, int, float]], verbose: bool = True):
    loss_name = params["name"]
    assert loss_name in ("kl", "mi")
    iid_loss = lambda x, y: IIDLoss()(x, y)[0]

    loss_func = KL_div(reduce=True) if loss_name == "kl" else iid_loss

    return VATLoss_Multihead(
        distance_func=loss_func,  **{k: v for k, v in params.items() if k != "name"}
    )


class MixUp(object):
    def __init__(self, device: torch.device, num_classes: int) -> None:
        self.device = device
        self.beta_distr = Beta(torch.tensor([1.0]), torch.tensor([1.0]))
        self.num_class = num_classes

    def __call__(self, img1: Tensor, pred1: Tensor, img2: Tensor, pred2: Tensor):
        assert simplex(pred1) and simplex(pred2)
        bn, *shape = img1.shape
        alpha = self.beta_distr.sample((bn,)).squeeze(1).to(self.device)
        _alpha = alpha.view(bn, 1, 1, 1).repeat(1, *shape)
        assert _alpha.shape == img1.shape
        mixup_img = img1 * _alpha + img2 * (1 - _alpha)
        mixup_label = pred1 * alpha.view(bn, 1) + pred2 * (1 - alpha).view(bn, 1)
        mixup_index = torch.stack([alpha, 1 - alpha], dim=1).to(self.device)

        assert mixup_img.shape == img1.shape
        assert mixup_label.shape == pred2.shape
        assert mixup_index.shape[0] == bn
        assert simplex(mixup_index)

        return mixup_img, mixup_label.detach(), mixup_index
