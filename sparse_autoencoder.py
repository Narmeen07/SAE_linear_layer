"""Most of this is just copied over from Joseph Blooms's code and slightly simplified:
"""

import gzip
import os
import pickle
from typing import NamedTuple

import einops
import torch
from torch import nn
from transformer_lens.hook_points import HookedRootModule, HookPoint

from config import LanguageModelSAERunnerConfig


class ForwardOutput(NamedTuple):
    sae_out: torch.Tensor
    feature_acts: torch.Tensor
    loss: torch.Tensor
    mse_loss: torch.Tensor
    l1_loss: torch.Tensor
    ghost_grad_loss: torch.Tensor


class SparseAutoencoder(HookedRootModule):
    """ """

    def __init__(
        self,
        cfg: LanguageModelSAERunnerConfig,
        pre_trained_weights=None
    ):
        super().__init__()
        self.cfg = cfg
        self.d_in = cfg.d_in
        if not isinstance(self.d_in, int):
            raise ValueError(
                f"d_in must be an int but was {self.d_in=}; {type(self.d_in)=}"
            )
        assert cfg.d_sae is not None  # keep pyright happy
        self.d_sae = cfg.d_sae
        self.l1_coefficient = cfg.l1_coefficient
        self.lp_norm = cfg.lp_norm
        self.dtype = cfg.dtype
        self.device = cfg.device

        # NOTE: if using resampling neurons method, you must ensure that we initialise the weights in the order W_enc, b_enc, W_dec, b_dec
        self.W_enc = nn.Parameter(
            torch.nn.init.kaiming_uniform_(
                torch.empty(self.d_in, self.d_sae, dtype=self.dtype, device=self.device)
            )
        )
        self.b_enc = nn.Parameter(
            torch.zeros(self.d_sae, dtype=self.dtype, device=self.device)
        )

        self.W_mid = nn.Parameter(
            torch.nn.init.kaiming_uniform_(
                torch.empty(self.d_sae, self.d_sae, dtype=self.dtype, device=self.device)
            )
        )
        self.b_mid = nn.Parameter(
            torch.zeros(self.d_sae, dtype=self.dtype, device=self.device)
        )
        self.W_dec = nn.Parameter(
            torch.nn.init.kaiming_uniform_(
                torch.empty(self.d_sae, self.d_in, dtype=self.dtype, device=self.device)
            )
        )

        with torch.no_grad():
            # Anthropic normalize this to have unit columns
            self.set_decoder_norm_to_unit_norm()

        self.b_dec = nn.Parameter(
            torch.zeros(self.d_in, dtype=self.dtype, device=self.device)
        )

                # Load pretrained weights if provided
        if pre_trained_weights:
            if 'W_enc' in pre_trained_weights:
                if self.W_enc.shape == pre_trained_weights['W_enc'].shape:
                    self.W_enc.data = pre_trained_weights['W_enc']
                    print("Loaded pretrained weights for W_enc")
                else:
                    print("Warning: Shape mismatch for W_enc, not loading weights.")
            if 'b_enc' in pre_trained_weights:
                if self.b_enc.shape == pre_trained_weights['b_enc'].shape:
                    self.b_enc.data = pre_trained_weights['b_enc']
                    print("Loaded pretrained weights for b_enc")
                else:
                    print("Warning: Shape mismatch for b_enc, not loading weights.")
            if 'W_dec' in pre_trained_weights:
                if self.W_dec.shape == pre_trained_weights['W_dec'].shape:
                    self.W_dec.data = pre_trained_weights['W_dec']
                    print("Loaded pretrained weights for W_dec")
                else:
                    print("Warning: Shape mismatch for W_dec, not loading weights.")
            if 'b_dec' in pre_trained_weights:
                if self.b_dec.shape == pre_trained_weights['b_dec'].shape:
                    self.b_dec.data = pre_trained_weights['b_dec']
                    print("Loaded pretrained weights for b_dec")
                else:
                    print("Warning: Shape mismatch for b_dec, not loading weights.")

        self.hook_sae_in = HookPoint()
        self.hook_hidden_pre = HookPoint()
        self.hook_hidden_post = HookPoint()
        self.hook_sae_out = HookPoint()

        self.setup()  # Required for `HookedRootModule`s

    def forward(self, x: torch.Tensor, y:torch.Tensor, dead_neuron_mask: torch.Tensor | None = None):
        # move x to correct dtype
        x = x.to(self.dtype)
        sae_in = self.hook_sae_in(
            x - self.b_dec
        )  # Remove decoder bias as per Anthropic
        print("Shape of X should be .... din")
        print(x.shape)
        print("Shape of Y should be .... din")
        print(y.shape)
        hidden_pre = self.hook_hidden_pre(
            einops.einsum(
                sae_in,
                self.W_enc,
                "... d_in, d_in d_sae -> ... d_sae",
            )
            + self.b_enc
        )
        #can be changed to quadratic
        feature_acts = self.hook_hidden_post(torch.nn.functional.relu(hidden_pre))
        # Middle layer transformation without non-linearity
        mid_layer_output = einops.einsum(
            feature_acts,
            self.W_mid,
            "... d_sae, d_sae d_sae -> ... d_sae",
        ) + self.b_mid
        print("Mid layer output")
        print(mid_layer_output)
        sae_out = self.hook_sae_out(
            einops.einsum(
                mid_layer_output,
                self.W_dec,
                "... d_sae, d_sae d_in -> ... d_in",
            )
            + self.b_dec
        )

        sae_out_x = self.hook_sae_out(
            einops.einsum(
                feature_acts,
                self.W_dec,
                "... d_sae, d_sae d_in -> ... d_in",
            )
            + self.b_dec
        )
        # add config for whether l2 is normalized:
        per_item_mse_loss = _per_item_mse_loss_with_target_norm(sae_out_x, x)
        per_item_mse_transition_loss = _per_item_mse_loss_with_target_norm(sae_out, y)
        ghost_grad_loss = torch.tensor(0.0, dtype=self.dtype, device=self.device)
        # gate on config and training so evals is not slowed down.
        if (
            self.cfg.use_ghost_grads
            and self.training
            and dead_neuron_mask is not None
            and dead_neuron_mask.sum() > 0
        ):
            ghost_grad_loss = self.calculate_ghost_grad_loss(
                y=y,
                sae_out=sae_out,
                per_item_mse_loss=per_item_mse_loss,
                hidden_pre=hidden_pre,
                dead_neuron_mask=dead_neuron_mask,
            )

        mse_loss = per_item_mse_loss.mean()
        mse_transition_loss = per_item_mse_transition_loss.mean()
        
        sparsity = feature_acts.norm(p=self.lp_norm, dim=1).mean(dim=(0,))
        l1_loss = self.l1_coefficient * sparsity
        loss = mse_loss + l1_loss + ghost_grad_loss + mse_transition_loss

        return ForwardOutput(
            sae_out=sae_out,
            feature_acts=feature_acts,
            loss=loss,
            mse_loss=mse_loss,
            l1_loss=l1_loss,
            ghost_grad_loss=ghost_grad_loss,
        )

    @torch.no_grad()
    def initialize_b_dec_with_precalculated(self, origin: torch.Tensor):
        out = torch.tensor(origin, dtype=self.dtype, device=self.device)
        self.b_dec.data = out

    @torch.no_grad()
    def initialize_b_dec_with_mean(self, all_activations: torch.Tensor):
        previous_b_dec = self.b_dec.clone().cpu()
        out = all_activations.mean(dim=0)

        previous_distances = torch.norm(all_activations - previous_b_dec, dim=-1)
        distances = torch.norm(all_activations - out, dim=-1)

        print("Reinitializing b_dec with mean of activations")
        print(
            f"Previous distances: {previous_distances.median(0).values.mean().item()}"
        )
        print(f"New distances: {distances.median(0).values.mean().item()}")

        self.b_dec.data = out.to(self.dtype).to(self.device)

    @torch.no_grad()
    def set_decoder_norm_to_unit_norm(self):
        self.W_dec.data /= torch.norm(self.W_dec.data, dim=1, keepdim=True)

    @torch.no_grad()
    def remove_gradient_parallel_to_decoder_directions(self):
        """
        Update grads so that they remove the parallel component
            (d_sae, d_in) shape
        """
        assert self.W_dec.grad is not None  # keep pyright happy

        parallel_component = einops.einsum(
            self.W_dec.grad,
            self.W_dec.data,
            "d_sae d_in, d_sae d_in -> d_sae",
        )
        self.W_dec.grad -= einops.einsum(
            parallel_component,
            self.W_dec.data,
            "d_sae, d_sae d_in -> d_sae d_in",
        )

    def save_model(self, path: str):
        """
        Basic save function for the model. Saves the model's state_dict and the config used to train it.
        """

        # check if path exists
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)

        state_dict = {"cfg": self.cfg, "state_dict": self.state_dict()}

        if path.endswith(".pt"):
            torch.save(state_dict, path)
        elif path.endswith(".pkl"):
            with open(path, "wb") as f:
                pickle.dump(state_dict, f)
        elif path.endswith("pkl.gz"):
            with gzip.open(path, "wb") as f:
                pickle.dump(state_dict, f)
        else:
            raise ValueError(
                f"Unexpected file extension: {path}, supported extensions are .pt and .pkl.gz"
            )

        print(f"Saved model to {path}")

    @classmethod
    def load_from_pretrained(cls, path: str):
        """
        Load function for the model. Loads the model's state_dict and the config used to train it.
        This method can be called directly on the class, without needing an instance.
        """

        # Ensure the file exists
        if not os.path.isfile(path):
            raise FileNotFoundError(f"No file found at specified path: {path}")

        # Load the state dictionary
        if path.endswith(".pt"):
            try:
                if torch.backends.mps.is_available():
                    state_dict = torch.load(path, map_location="mps")
                    state_dict["cfg"].device = "mps"
                else:
                    state_dict = torch.load(path)
            except Exception as e:
                raise IOError(f"Error loading the state dictionary from .pt file: {e}")

        elif path.endswith(".pkl.gz"):
            try:
                with gzip.open(path, "rb") as f:
                    state_dict = pickle.load(f)
            except Exception as e:
                raise IOError(
                    f"Error loading the state dictionary from .pkl.gz file: {e}"
                )
        elif path.endswith(".pkl"):
            try:
                with open(path, "rb") as f:
                    state_dict = pickle.load(f)
            except Exception as e:
                raise IOError(f"Error loading the state dictionary from .pkl file: {e}")
        else:
            raise ValueError(
                f"Unexpected file extension: {path}, supported extensions are .pt, .pkl, and .pkl.gz"
            )

        # Ensure the loaded state contains both 'cfg' and 'state_dict'
        if "cfg" not in state_dict or "state_dict" not in state_dict:
            raise ValueError(
                "The loaded state dictionary must contain 'cfg' and 'state_dict' keys"
            )

        # Create an instance of the class using the loaded configuration
        instance = cls(cfg=state_dict["cfg"])
        instance.load_state_dict(state_dict["state_dict"])

        return instance

    def get_name(self):
        sae_name = f"sparse_autoencoder_{self.cfg.model_name}_{self.cfg.hook_point}_{self.cfg.d_sae}"
        return sae_name

    def calculate_ghost_grad_loss(
        self,
        y: torch.Tensor,
        sae_out: torch.Tensor,
        per_item_mse_loss: torch.Tensor,
        hidden_pre: torch.Tensor,
        dead_neuron_mask: torch.Tensor,
    ) -> torch.Tensor:
        # 1.
        residual = y - sae_out
        l2_norm_residual = torch.norm(residual, dim=-1)

        # 2.
        feature_acts_dead_neurons_only = torch.exp(hidden_pre[:, dead_neuron_mask])
        ghost_out = feature_acts_dead_neurons_only @ self.W_dec[dead_neuron_mask, :]
        l2_norm_ghost_out = torch.norm(ghost_out, dim=-1)
        norm_scaling_factor = l2_norm_residual / (1e-6 + l2_norm_ghost_out * 2)
        ghost_out = ghost_out * norm_scaling_factor[:, None].detach()

        # 3.
        per_item_mse_loss_ghost_resid = _per_item_mse_loss_with_target_norm(
            ghost_out, residual.detach()
        )
        mse_rescaling_factor = (
            per_item_mse_loss / (per_item_mse_loss_ghost_resid + 1e-6)
        ).detach()
        per_item_mse_loss_ghost_resid = (
            mse_rescaling_factor * per_item_mse_loss_ghost_resid
        )

        return per_item_mse_loss_ghost_resid.mean()


def _per_item_mse_loss_with_target_norm(
    preds: torch.Tensor, target: torch.Tensor
) -> torch.Tensor:
    """
    Calculate MSE loss per item in the batch, without taking a mean.
    Then, normalizes by the L2 norm of the centered target.
    This normalization seems to improve performance.
    """
    target_centered = target - target.mean(dim=0, keepdim=True)
    normalization = target_centered.norm(dim=-1, keepdim=True)
    return torch.nn.functional.mse_loss(preds, target, reduction="none") / normalization
