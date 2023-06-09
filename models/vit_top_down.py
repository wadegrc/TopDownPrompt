import math
import logging
from functools import partial
from functools import reduce
from operator import mul
from collections import OrderedDict
from copy import deepcopy

import torch
import torch.nn as nn
import torch.nn.functional as F

from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD, IMAGENET_INCEPTION_MEAN, IMAGENET_INCEPTION_STD
from timm.models.helpers import build_model_with_cfg, named_apply, adapt_input_conv
from timm.models.layers.helpers import to_2tuple
from timm.models.layers import PatchEmbed, Mlp, DropPath, trunc_normal_, lecun_normal_#,PatchEmbed
from timm.models.registry import register_model
from timm.models.vision_transformer import _cfg
from torch.nn.modules.utils import _pair
import sys
import numpy as np
from .utils import tensor_prompt
from .hsic import hsic_normalized as hsic
_logger = logging.getLogger(__name__)


class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, attn_drop=0., proj_drop=0.):
        super().__init__()
        assert dim % num_heads == 0, 'dim should be divisible by num_heads'
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x, td=None, return_visualization = False):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv.unbind(0)   # make torchscript happy (cannot use tensor as tuple)

        if td is not None:
            qkv_td = self.qkv(td).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
            v = v + qkv_td[2]

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        if return_visualization:
            attn_copy = attn[0].clone().detach().cpu().numpy()
            attn_copy = attn_copy[:, 0, 1:]
            # height, width = int(math.sqrt(attn_copy.shape[-1])), int(math.sqrt(attn_copy.shape[-1]))
            # attn_copy = attn_copy.reshape((attn_copy.shape[0], height, width))
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        if return_visualization:
            return x, attn_copy
        return x, None



class LayerScale(nn.Module):
    def __init__(self, dim, init_values=1e-5, inplace=False):
        super().__init__()
        self.inplace = inplace
        self.gamma = nn.Parameter(init_values * torch.ones(dim))

    def forward(self, x):
        return x.mul_(self.gamma) if self.inplace else x * self.gamma


class Block(nn.Module):

    def __init__(
            self, dim, num_heads, mlp_ratio=4., qkv_bias=False, drop=0., attn_drop=0., init_values=None,
            drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(dim, num_heads=num_heads, qkv_bias=qkv_bias, attn_drop=attn_drop, proj_drop=drop)
        self.ls1 = LayerScale(dim, init_values=init_values) if init_values else nn.Identity()
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path1 = DropPath(drop_path) if drop_path > 0. else nn.Identity()

        self.norm2 = norm_layer(dim)
        self.mlp = Mlp(in_features=dim, hidden_features=int(dim * mlp_ratio), act_layer=act_layer, drop=drop)
        self.ls2 = LayerScale(dim, init_values=init_values) if init_values else nn.Identity()
        self.drop_path2 = DropPath(drop_path) if drop_path > 0. else nn.Identity()

    def forward(self, x, td=None, return_visualization = False):
        x_attn, visualization_heads = self.attn(self.norm1(x), td, return_visualization = return_visualization)
        x = x + self.drop_path1(self.ls1(x_attn))
        x = x + self.drop_path2(self.ls2(self.mlp(self.norm2(x))))
        return x, visualization_heads

class Decode_Block(nn.Module):
    def __init__(self, inplanes):
        super().__init__()
        self.linear = nn.Linear(inplanes, inplanes, bias=False)
        self.linear2 = nn.Linear(inplanes, inplanes, bias=False)

    def forward(self, x):
        x = self.linear(x)
        out = self.linear2(x)
        # out = x
        return x, out
        #return x, x


class VisionTransformer(nn.Module):
    """ Vision Transformer
    A PyTorch impl of : `An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale`
        - https://arxiv.org/abs/2010.11929
    """

    def __init__(
            self, img_size=224, patch_size=16, in_chans=3, num_classes=1000, global_pool='token',
            embed_dim=768, depth=12, num_heads=12, mlp_ratio=4., qkv_bias=True, init_values=None,
            class_token=True, no_embed_class=False, fc_norm=None, drop_rate=0., attn_drop_rate=0., drop_path_rate=0.1,
            weight_init='', embed_layer=PatchEmbed, norm_layer=nn.LayerNorm, act_layer=None, block_fn=Block, truncate_embedding="none",key_dim=768):
        """
        Args:
            img_size (int, tuple): input image size
            patch_size (int, tuple): patch size
            in_chans (int): number of input channels
            num_classes (int): number of classes for classification head
            global_pool (str): type of global pooling for final sequence (default: 'token')
            embed_dim (int): embedding dimension
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            init_values: (float): layer-scale init values
            class_token (bool): use class token
            fc_norm (Optional[bool]): pre-fc norm after pool, set if global_pool == 'avg' if None (default: None)
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            weight_init (str): weight init scheme
            embed_layer (nn.Module): patch embedding layer
            norm_layer: (nn.Module): normalization layer
            act_layer: (nn.Module): MLP activation layer
        """
        super().__init__()
        assert global_pool in ('', 'avg', 'token')
        assert class_token or global_pool != 'token'

        #self.prompt_config = prompt_config
        #self.vit_config = config
        self.patch_size = patch_size

        use_fc_norm = global_pool == 'avg' if fc_norm is None else fc_norm
        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        act_layer = act_layer or nn.GELU

        self.num_classes = num_classes
        self.global_pool = global_pool
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
        self.num_prefix_tokens = 1 if class_token else 0
        self.no_embed_class = no_embed_class
        self.grad_checkpointing = False

        self.patch_embed = embed_layer(
            img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim)
        num_patches = self.patch_embed.num_patches

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim)) if class_token else None
        embed_len = num_patches if no_embed_class else num_patches + self.num_prefix_tokens
        self.pos_embed = nn.Parameter(torch.randn(1, embed_len, embed_dim) * .02)
        self.pos_drop = nn.Dropout(p=drop_rate)

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.Sequential(*[
            block_fn(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, init_values=init_values,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, act_layer=act_layer)
            for i in range(depth)])
        self.n_tasks = 10
        self.norm = norm_layer(embed_dim) if not use_fc_norm else nn.Identity()
        #self.decoders = nn.ModuleList([nn.ModuleList([Decode_Block(embed_dim) for _ in range(3)]) for _ in range(10)])
        #self.decoder_list = []
        #self.decoders = nn.ModuleList([Decode_Block(embed_dim) for _ in range(depth)])
        self.prompt = torch.nn.Parameter(torch.randn(self.n_tasks, self.embed_dim))
        self.top_down_transform = torch.nn.Parameter(torch.stack([torch.eye(self.embed_dim) for i in range(self.n_tasks)], dim = 0))
        self.decoders = nn.ModuleList([nn.Linear(self.embed_dim, self.embed_dim, bias = False) for i in range(self.n_tasks)])
        #self.prompt = torch.nn.Parameter(torch.randn(self.embed_dim))
        #self.top_down_transform = torch.nn.Parameter(torch.eye(self.embed_dim))
        self.task_count = 0
        # 加权
        self.key_d = key_dim
        self.k = tensor_prompt(self.n_tasks, self.key_d, ortho = True)
        self.a = tensor_prompt(self.n_tasks, self.key_d, ortho = True)
        # Classifier Head
        #self.fc_norm = norm_layer(embed_dim) if use_fc_norm else nn.Identity()
        #self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

        if weight_init != 'skip':
            self.init_weights(weight_init)

    def init_weights(self, mode=''):
        assert mode in ('jax', 'jax_nlhb', 'moco', '')
        head_bias = -math.log(self.num_classes) if 'nlhb' in mode else 0.
        trunc_normal_(self.pos_embed, std=.02)
        if self.cls_token is not None:
            nn.init.normal_(self.cls_token, std=1e-6)
        named_apply(init_weights_vit_timm, self)
    
    def process_task_count(self):
        self.task_count += 1
        #self.decoder_list[-1] = self.decoder_list[-1].detach().clone()
        #self.decoder_list.append(deepcopy(self.decoders))

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token', 'dist_token'}

    @torch.jit.ignore
    def group_matcher(self, coarse=False):
        return dict(
            stem=r'^cls_token|pos_embed|patch_embed',  # stem and embed
            blocks=[(r'^blocks\.(\d+)', None), (r'^norm', (99999,))]
        )

    @torch.jit.ignore
    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes: int, global_pool=None):
        self.num_classes = num_classes
        if global_pool is not None:
            assert global_pool in ('', 'avg', 'token')
            self.global_pool = global_pool
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def _pos_embed(self, x):
        if self.no_embed_class:
            # deit-3, updated JAX (big vision)
            # position embedding does not overlap with class token, add then concat
            x = x + self.pos_embed
            if self.cls_token is not None:
                x = torch.cat((self.cls_token.expand(x.shape[0], -1, -1), x), dim=1)
        else:
            # original timm, JAX, and deit vit impl
            # pos_embed has entry for class token, concat then add
            if self.cls_token is not None:
                x = torch.cat((self.cls_token.expand(x.shape[0], -1, -1), x), dim=1)
            x = x + self.pos_embed
        return self.pos_drop(x)

    def forward_features(self, x, td=None):
        x = self.patch_embed(x)
        x = self._pos_embed(x)
        B, nt, fd = x.shape
        """
        for i, blk in enumerate(self.blocks):
            if i < len(self.blocks) - 1:
                x, visualization_heads = blk(x, td[i] if td is not None else None)
            else:
                x, visualization_heads = blk(x, td[i] if td is not None else None)
        """
        for i, blk in enumerate(self.blocks):
            if i < len(self.blocks) - (len(td) if td is not None else 0):
                x, visualization_heads = blk(x, None)
            else:
                x, visualization_heads = blk(x, td[i-len(self.blocks)+len(td)] if td is not None else None)

        x = self.norm(x)
        return x

    def feedback(self, x, i=0):
        td = []
        for depth in range(len(self.decoders[i]) - 1, -1, -1):
            x, out = self.decoders[i][depth](x)
            td = [out] + td
        """
        if i < len(self.decoder_list):
            for depth in range(len(self.decoder_list[i]) - 1, -1, -1):
                #self.decoder_list[i][depth].to(x.device)
                x, out = self.decoder_list[i][depth](x)
                td = [out] + td
        else:
            for depth in range(len(self.decoders) - 1, -1, -1):
                #self.decoder_list[i][depth].to(x.device)
                x, out = self.decoders[depth](x)
                td = [out] + td
        """
        return td

    def forward_head(self, x, pre_logits: bool = False):
        if self.global_pool:
            x = x[:, self.num_prefix_tokens:].mean(dim=1) if self.global_pool == 'avg' else x[:, 0]
        x = self.fc_norm(x)
        return x if pre_logits else self.head(x)
    
    def forward(self, x, register_blk=-1, prompt=None, q=None, train=False, task_id=None, return_all_features=False):

        input = x
        output_each_iter = []

        # first feedforward
        x = self.forward_features(input)
        #output_each_iter.append(self.forward_head(x))

        # feature selection and feedback
        """ 
        cos_sim = F.normalize(x, dim=-1) @ F.normalize(self.prompt[None, ..., None], dim=1)  # B, N, 1
        mask = cos_sim.clamp(0, 1)
        x = x * mask
        x = x @ self.top_down_transform
        td = self.feedback(x)
        """
        x_sum = []
        for i in range(self.task_count + 1):
            cos_sim = F.normalize(x, dim=-1) @ F.normalize(self.prompt[i][None, ..., None].detach().clone() if i < self.task_count else self.prompt[i][None, ..., None], dim=1)  # B, N, 1
            mask = cos_sim.clamp(0, 1)
            x_ = x * mask
            x_ = x_ @ (self.top_down_transform[i].detach().clone() if i < self.task_count else self.top_down_transform[i])
            #x_ = self.decoders[i](x_)
            x_ = x_.unsqueeze(1)
            x_sum.append(x_)
        x_sum = torch.cat(x_sum, dim=1)

        #print("x_sum:", x_sum.shape)
        # 加权
        #pt = int(self.k.shape[0] / (self.n_tasks))
        s = int(self.task_count)
        f = int((self.task_count + 1))

        if self.task_count > 0:
            K = torch.cat((self.k[:s].detach().clone(), self.k[s:f]), dim = 0)
            A = torch.cat((self.a[:s].detach().clone(), self.a[s:f]), dim = 0)
        else:
            K = self.k[s:f]
            A = self.a[s:f]
        
        # with attention and cosine sim
        # (b x 1 x d) * soft([1 x k x d]) = (b x k x d) -> attention = k x d
        #print("x[0]", x[:,0,:].shape)
        #print("A:", A.shape)
        a_querry = torch.einsum('bd,kd->bkd', x[:,0,:], A)
        #print("a_querry:",a_querry.shape)
        # # (b x k x d) - [1 x k x d] = (b x k) -> key = k x d
        n_K = nn.functional.normalize(K, dim=1)
        q = nn.functional.normalize(a_querry, dim=2)
        aq_k = torch.einsum('bkd,kd->bk', q, n_K)
        #print("aq_k:", aq_k.shape)
        ## (b x 1 x k x 1) * [1 x plen x k x d] = (b x plen x d) -> prompt = plen x k x d
        
        t_sum = torch.einsum('bk,bkld->bkld', aq_k, x_sum)
        #td = self.feedback(x_sum)
        td = torch.sum(t_sum, dim=1)
        td = [td for i in range(3)]
        """
            if i == 0:
                td_sum = td
            else:
                td_sum = [td_sum[j]+td[j] for j in range(len(td))]
        td_sum = [td_sum[i] / (self.task_count + 1) for i in range(len(td_sum))]
        """

        """
        x_sum = sum(x_sum) / (self.task_count + 1)
        if self.task_count > 0:
            cos_sim = F.normalize(x, dim=-1) @ F.normalize(torch.cat((self.prompt[:self.task_count, ..., None], self.prompt[self.task_count:self.task_count+1,..., None])), dim=1)  # B, N, 1
            mask = cos_sim.clamp(0,1)
            x = x * mask
            x = x @ torch.cat((self.top_down_transform[:self.task_count], self.top_down_transform[self.task_count:self.task_count+1], dim = 0))
        else:
            cos_sim = F.normalize(x, dim=-1) @ F.normalize(self.prompt[:self.task_count, ..., None], dim=1)  # B, N, 1
            mask = cos_sim.clamp(0,1)
            x = x * mask
            x = x @ self.top_down_transform[:self.task_count]
        
        td = self.feedback(torch.sum(x, dim = 0))
        """
        #减少当前的之前与过去的差距
        h_sum = [x_sum[:,i,:,:].reshape(x_sum.shape[0],-1) for i in range(x_sum.shape[1])]
        for i in range(x_sum.shape[1]):
            if i == 0:
                hsic_loss = hsic(h_sum[x_sum.shape[1]-1], h_sum[i], 5)
                continue
            hsic_loss += hsic(h_sum[x_sum.shape[1]-1], h_sum[i], 5)
        #h_sum = [a_querry[:,i,:].reshape(a_querry.shape[0],-1) for i in range(a_querry.shape[1])]
        #hsic_loss = [hsic(h_sum[a_querry.shape[1]-1], h_sum[i], 5) for i in range(a_querry.shape[1]-1)]
        """
        if self.task_count>0:
            print(h_sum[0].shape)
            print(hsic_loss)
        """
        #print(hsic_loss)
        #hsic_loss = sum(hsic_loss)
        #print(hsic_loss)
        #loss += ortho_penalty(self.top_down_transform)
        # second feedforward
        x = self.forward_features(input, td)
        """
        cos = torch.nn.CosineSimilarity(dim=2,eps=1e-07)
        score = cos(x,x_old)
        score = 1.0-torch.mean(score)
        """
        #loss = ortho_penalty(K)
        #loss += ortho_penalty(A)
        loss =  0.9*hsic_loss
        #loss += 0.1*score
        #x = self.forward_head(x)
        output_each_iter.append(x)
        

        return x, loss

    def var_loss(self, in_var, out_var, x):
        recon_loss = []
        for depth in range(len(self.decoders) - 1, -1, -1):
            recon, out = self.decoders[depth](out_var[depth].detach())
            target = in_var[depth].detach()
            recon_loss.append(F.mse_loss(recon, target))

        return 0.1*sum(recon_loss)

def ortho_penalty(t):
    return ((t @t.T - torch.eye(t.shape[0]).cuda())**2).mean() * 1e-6

def init_weights_vit_timm(module: nn.Module, name: str = ''):
    """ ViT weight initialization, original timm impl (for reproducibility) """
    if isinstance(module, nn.Linear):
        trunc_normal_(module.weight, std=.02)
        if module.bias is not None:
            nn.init.zeros_(module.bias)
    elif hasattr(module, 'init_weights'):
        module.init_weights()



@register_model
def vit_topdown_tiny_patch16_224(pretrained=False, cfg = None, prompt_cfg = None, **kwargs):
    assert cfg is not None, "config cannot be None!"
    if pretrained:
        assert cfg.MODEL.MODEL_ROOT is not None, "Checkpoint path cannot be None for pretrained models! "
    model = VisionTransformer(
        config=cfg, prompt_config=prompt_cfg,
        patch_size=16, embed_dim=192, depth=12, num_heads=3, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        checkpoint = torch.hub.load_state_dict_from_url(
            url="https://dl.fbaipublicfiles.com/deit/deit_tiny_patch16_224-a1311bcf.pth",
            map_location="cpu", check_hash=True
        )
        model.load_state_dict(checkpoint["model"])
    return model


@register_model
def vit_topdown_small_patch16_224(pretrained=False, cfg = None, prompt_cfg = None, **kwargs):
    assert cfg is not None, "config cannot be None!"
    if pretrained:
        assert cfg.MODEL.MODEL_ROOT is not None, "Checkpoint path cannot be None for pretrained models! "
    model = VisionTransformer(
        config=cfg, prompt_config=prompt_cfg,
        patch_size=16, num_classes = -1, embed_dim=384, depth=12, num_heads=6, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        state_dict = torch.load(cfg.MODEL.MODEL_ROOT, map_location="cpu")
        model.load_state_dict(state_dict["model"], strict=False)
    return model, model.embed_dim


@register_model
def vit_topdown_base_patch16_224(pretrained=False, cfg = None, prompt_cfg = None, **kwargs):
    assert cfg is not None, "config cannot be None!"
    if pretrained:
        assert cfg.MODEL.MODEL_ROOT is not None, "Checkpoint path cannot be None for pretrained models! "
    model = VisionTransformer(
        config=cfg, prompt_config=prompt_cfg,
        patch_size=16, num_classes=-1, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        state_dict = torch.load(cfg.MODEL.MODEL_ROOT, map_location="cpu")
        model.load_state_dict(state_dict["model"], strict=False)
    return model, model.embed_dim


@register_model
def vit_topdown_large_patch16_224(pretrained=False, cfg = None, prompt_cfg = None, **kwargs):
    assert cfg is not None, "config cannot be None!"
    if pretrained:
        assert cfg.MODEL.MODEL_ROOT is not None, "Checkpoint path cannot be None for pretrained models! "
    model = VisionTransformer(
        config=cfg, prompt_config=prompt_cfg,
        patch_size=16, num_classes=-1, embed_dim=1024, depth=24, num_heads=16, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), **kwargs)
    model.default_cfg = _cfg()
    if pretrained:
        state_dict = torch.load(cfg.MODEL.MODEL_ROOT, map_location="cpu")
        model.load_state_dict(state_dict["model"], strict=False)
    return model, model.embed_dim
