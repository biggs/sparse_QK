import torch as t
from torch import Tensor, nn
from torch.nn.functional import normalize
import transformer_lens
from transformer_lens import HookedTransformer
from dataclasses import dataclass
import math
from tqdm import tqdm
import torch.nn.functional as F
import wandb
import einops

class SparseQK(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        d_hidden = cfg["d_hidden"]
        reg_coeff = cfg["reg_coeff"]
        t.manual_seed(cfg["seed"])
        n_heads = cfg["n_heads"]
        self.n_heads = n_heads
        self.d_head = cfg["d_head"]
        self.d_model = cfg["d_model"]

        self.d_hidden = d_hidden
        self.W_encQ = nn.Parameter(t.nn.init.kaiming_uniform_(t.empty(cfg["d_model"], self.d_hidden)))
        self.W_encK = nn.Parameter(t.nn.init.kaiming_uniform_(t.empty(cfg["d_model"], self.d_hidden)))
        self.W_dec = nn.Parameter(t.nn.init.kaiming_uniform_(t.empty(d_hidden, self.n_heads)))
        self.b_encQ = nn.Parameter(t.zeros(self.d_hidden))
        self.b_encK = nn.Parameter(t.zeros(self.d_hidden))
        self.b_dec = nn.Parameter(t.zeros(self.n_heads))
        self.eps = cfg["eps"]

        #self.b_enc = nn.Parameter(torch.zeros(d_hidden, dtype=dtype))
        #self.b_dec = nn.Parameter(torch.zeros(cfg["act_size"], dtype=dtype))
        #self.W_dec.data[:] = self.W_dec / self.W_dec.norm(dim=-1, keepdim=True)

        self.d_hidden = d_hidden
        self.reg_coeff = reg_coeff
        self.register_buffer("IGNORE", t.tensor(-1e6, dtype=t.float32, device="cuda"))

        self.to(cfg["device"])

    def forward(self, x, masked = False):
        queries = F.relu(einops.einsum(self.W_encQ, x, "d_model d_hidden, d_model -> d_hidden") + self.b_encQ).unsqueeze(2)
        keys= F.relu(einops.einsum(self.W_encK, x, "d_model d_hidden, d_model -> d_hidden") + self.b_encK).unsqueeze(1)
   
        acts = queries * keys
        reg_loss = self.reg_coeff * t.sqrt(acts.float().abs() + self.eps).sum()
        feature_fires = (acts > 0).int()
        score_reconstr = einops.einsum(acts, self.W_dec, "batch posn_q posn_k d_hidden, d_hidden n_heads -> batch posn_q posn_k n_heads")/(self.d_head ** 0.5) + self.b_dec
        score_reconstr = einops.rearrange(score_reconstr, "batch posn_q posn_k n_heads -> batch n_heads posn_q posn_k")
        if masked:
          score_recostr = self.apply_causal_mask(score_reconstr)
        return score_reconstr, reg_loss, feature_fires

    def apply_causal_mask(self, attn_scores: t.Tensor):
        mask = t.triu(t.ones(attn_scores.size(-2), attn_scores.size(-1), device=attn_scores.device), diagonal=1).bool()
        attn_scores.masked_fill_(mask, self.IGNORE)
        return attn_scores

    @t.no_grad()
    def renorm_weights(self):
        q_normed = self.W_enc[:, 0, :].norm(dim = 0)
        k_normed = self.W_enc[:, 1, :].norm(dim = 0)
        self.W_enc[:, 0, :] = self.W_enc[:, 0, :]/q_normed
        self.W_enc[:, 1, :] = self.W_enc[:, 1, :]/k_normed


def train_sparse_QK(
    orig_model,
    cfg,
    n_epochs: int,
    layer,
    data):
      
    sparse_model = SparseQK(cfg = cfg).cuda()
    print(f"Training model with {sparse_model.d_hidden} feature pairs.")
    optimizer = t.optim.AdamW(sparse_model.parameters(), lr = 1e-3)
    wandb.init(project="sparse_QK_gpt2_L0.5", entity="kwyn390")
    feature_tots = t.zeros(cfg["d_hidden"]).cuda()
    tot_data = 0
    for epoch in range(n_epochs):
        batch_index = None
        progress_bar = tqdm(list(enumerate(data)))
        for batch_idx, batch in progress_bar:
            if batch_idx < 10000:
              tot_data += (batch["tokens"].size(0) * batch["tokens"].size(1) * batch["tokens"].size(1))
              #normalise encoder weights
              sparse_model.renorm_weights()
              optimizer.zero_grad()
              _, cache = orig_model.run_with_cache(batch["tokens"])
              resid_pre = cache["resid_pre", layer].clone()
              ln = cache['blocks.'+str(layer)+'.ln1.hook_scale']
              resid_pre = resid_pre/ln
              q, k = cache["q", 10], cache["k", 10]
              original_scores = einops.einsum(q, k, "batch pos_q n_heads d_head, batch pos_k n_heads d_head -> batch n_heads pos_q pos_k").clone()/8
              modified_output, reg_loss, feature_fires = sparse_model(resid_pre)
              mse_loss = t.nn.MSELoss(reduction="none")
              abs_true = t.abs(original_scores)
              abs_pred = t.abs(modified_output)
              abs = t.maximum(abs_true, abs_pred)
              reconstruction_loss = einops.einsum(abs_true, mse_loss(modified_output, original_scores), "batch posn_q n_heads d_head, batch posn_q n_heads d_head ->")
              reconstruction_loss = reconstruction_loss / (original_scores.size(0)*original_scores.size(1)*original_scores.size(2)**2)
              loss = reconstruction_loss + reg_loss
              loss.backward(retain_graph = True)
              optimizer.step()
              feature_tots += feature_fires.sum(0).sum(0).sum(0)
              l0 = feature_fires.sum() / (feature_fires.size(0)*(feature_fires.size(1)**2))
              feature_freqs = feature_tots / tot_data
              wandb.log({
                  "recons_score": reconstruction_loss,
                  "loss": loss,
                  "reg_loss": reg_loss,
                  "l0": l0,
                  "dead_features": (feature_freqs < cfg["dead_freq"]).sum()

              })
              del batch_idx


        print(
                f"Epoch {epoch} reconstruction loss: {reconstruction_loss.item()} l0: {l0} reg_loss {reg_loss}"
            )
        print(f"Epoch {epoch} loss: {loss.item()}")

      

    return sparse_model