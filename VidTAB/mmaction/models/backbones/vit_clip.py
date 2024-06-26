from collections import OrderedDict
from mmengine.model.weight_init import trunc_normal_
import torch
from torch import nn
import clip
from mmengine.logging import MMLogger
from einops import rearrange
from mmaction.registry import MODELS



class LayerNorm(nn.LayerNorm):
    """Subclass torch's LayerNorm to handle fp16."""

    def forward(self, x: torch.Tensor):
        orig_type = x.dtype
        ret = super().forward(x.type(torch.float32))
        return ret.type(orig_type)

class QuickGELU(nn.Module):
    def forward(self, x: torch.Tensor):
        return x * torch.sigmoid(1.702 * x)

class ResidualAttentionBlock(nn.Module):
    def __init__(self, d_model: int, n_head: int, num_frames=8):
        super().__init__()

        self.attn = nn.MultiheadAttention(embed_dim=d_model, num_heads=n_head)

        self.ln_1 = LayerNorm(d_model)
        self.mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(d_model, d_model * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(d_model * 4, d_model))
        ]))
        self.ln_2 = LayerNorm(d_model)
        self.attn_mask = None
        self.n_head = n_head

        self.num_frames = num_frames


    def attention(self, x: torch.Tensor):
        self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
        return self.attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

    def forward(self, x: torch.Tensor):
        # x shape [HW+1, BT, D]
        x = x + self.attention(self.ln_1(x))
        x = x + self.mlp(self.ln_2(x))
        return x

    
class Transformer(nn.Module):
    def __init__(self, num_frames, width: int, layers: int, heads: int):
        super().__init__()
        self.width = width
        self.layers = layers
        self.resblocks = nn.Sequential(*[ResidualAttentionBlock(width, heads, num_frames) for i in range(layers)])

    def forward(self, x: torch.Tensor):
        return self.resblocks(x)


@MODELS.register_module()
class ViT_CLIP(nn.Module):
    ## ViT definition in CLIP image encoder
    def __init__(self, input_resolution: int, num_frames: int, patch_size: int, width: int, layers: int, heads: int, pretrained=None, frozen=True, adaptation_type='frozen_tuning'):
        super().__init__()
        self.frozen = frozen
        self.input_resolution = input_resolution
        self.pretrained = pretrained
        self.conv1 = nn.Conv2d(in_channels=3, out_channels=width, kernel_size=patch_size, stride=patch_size, bias=False)

        scale = width ** -0.5
        self.layers = layers
        self.class_embedding = nn.Parameter(scale * torch.randn(width))
        self.positional_embedding = nn.Parameter(scale * torch.randn((input_resolution // patch_size) ** 2 + 1, width))
        self.ln_pre = LayerNorm(width)

        self.num_frames = num_frames
        self.temporal_embedding = nn.Parameter(torch.zeros(1, num_frames, width))

        self.transformer = Transformer(num_frames, width, layers, heads)
        self.ln_post = LayerNorm(width)
        self.adaptation_type = adaptation_type
        if self.adaptation_type == 'frozen_tuning':
            from .attentive_pooler import AttentivePooler
            self.attentive_pooler = AttentivePooler(
                num_queries=1,
                embed_dim=width,
                num_heads=heads,
                mlp_ratio=4.0,
                depth=1
            )
        elif self.adaptation_type == 'adapter':
            raise NotImplementedError
        
    def init_weights(self):
        def _init_weights(m):
            if isinstance(m, nn.Linear):
                trunc_normal_(m.weight, std=.02)
                if isinstance(m, nn.Linear) and m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.LayerNorm):
                nn.init.constant_(m.bias, 0)
                nn.init.constant_(m.weight, 1.0)

        logger = MMLogger.get_current_instance()
        if isinstance(self.pretrained, str):
            self.apply(_init_weights)
            ## Load OpenAI CLIP pretrained weights
            if self.layers == 12:
                logger.info(f'load model from: {self.pretrained} clip ViT-B/16')
                clip_model, _ = clip.load("ViT-B/16", device="cpu", download_root=self.pretrained)
            else:
                logger.info(f'load model from: {self.pretrained} clip ViT-L/14')
                clip_model, _ = clip.load("ViT-L/14", device="cpu", download_root=self.pretrained)
            pretrain_dict = clip_model.visual.state_dict()
            del clip_model
            del pretrain_dict['proj']
            msg = self.load_state_dict(pretrain_dict, strict=False)
            logger.info('Missing keys: {}'.format(msg.missing_keys))
            logger.info('Unexpected keys: {}'.format(msg.unexpected_keys))
            logger.info(f"=> loaded successfully '{self.pretrained}'")
            torch.cuda.empty_cache()
        elif self.pretrained is None:
            logger.warn("You should load pretrained!!!")
            self.apply(_init_weights)
        else:
            raise TypeError('pretrained must be a str or None')


        ## freeze some parameters
        for name, param in self.named_parameters():
            param.requires_grad = True
            if 'temporal_embedding' not in name and 'ln_post' not in name and 'attentive_pooler' not in name:
                param.requires_grad = False



        for name, param in self.named_parameters():
            logger.info('{}: {}'.format(name, param.requires_grad))
        num_param = sum(p.numel() for p in self.parameters() if p.requires_grad)
        num_total_param = sum(p.numel() for p in self.parameters())
        logger.info('Number of total parameters: {}, tunable parameters: {}'.format(num_total_param, num_param))


    def forward(self, x: torch.Tensor):
        ## Space-only
        B, C, T, H, W = x.shape
        assert T == self.num_frames, f"{T} != {self.num_frames}"
        x = rearrange(x, 'b c t h w -> (b t) c h w')
        x = self.conv1(x)  
        x = x.reshape(x.shape[0], x.shape[1], -1) 
        x = x.permute(0, 2, 1)
        x = torch.cat([self.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device), x], dim=1)
        x = x + self.positional_embedding.to(x.dtype)

        n = x.shape[1]
        x = rearrange(x, '(b t) n d -> (b n) t d', t=self.num_frames)
        x = x + self.temporal_embedding
        x = rearrange(x, '(b n) t d -> (b t) n d', n=n)
        x = self.ln_pre(x)

        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.transformer(x)

        x = self.ln_post(x)

        if self.adaptation_type == 'frozen_tuning':
            x = x.permute(1, 0, 2)  #NBD -> BND
            # print("Before attentive pooler", x.shape)
            x = self.attentive_pooler(x)
            x = x[:, 0, :]
            # print("After attentive pooler:", x.shape)
        else:
            x = x[0]

        x = x.unsqueeze(-1).unsqueeze(-1).unsqueeze(-1) # for I3D head
        # if self.proj is not None:
        #     x = self.dropout(x[0]) @ self.proj
        # else:
        #     x = x.permute(1, 0, 2)  #NBD -> BND

        return x
