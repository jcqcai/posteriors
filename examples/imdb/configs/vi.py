import posteriors
import torchopt
import torch
from optree import tree_map

name = "vi"
save_dir = "examples/imdb/results/" + name
params_dir = None  # directory to load state containing initialisation params


prior_sd = (1 / 40) ** 0.5
batch_size = 32
burnin = None
save_frequency = None

method = posteriors.vi.diag
config_args = {
    "optimizer": torchopt.adamw(lr=1e-3),
    "temperature": None,  # None temperature gets set by train.py
    "n_samples": 1,
    "stl": True,
    "init_log_sds": -3,
}  # arguments for method.build (aside from log_posterior)
log_metrics = {
    "nelbo": "nelbo",
}  # dict containing names of metrics as keys and their paths in state as values
display_metric = "nelbo"  # metric to display in tqdm progress bar

log_frequency = 100  # frequency at which to log metrics
log_window = 30  # window size for moving average

n_test_samples = 50
n_linearised_test_samples = 10000


def to_sd_diag(state):
    return tree_map(lambda x: x.exp(), state.log_sd_diag)


def forward(model, state, batch):
    x, _ = batch
    sd_diag = to_sd_diag(state)

    sampled_params = posteriors.diag_normal_sample(
        state.params, sd_diag, (n_test_samples,)
    )

    def model_func(p, x):
        return torch.func.functional_call(model, p, x)

    logits = torch.vmap(model_func, in_dims=(0, None))(sampled_params, x).transpose(
        0, 1
    )
    return logits


def forward_linearized(model, state, batch):
    x, _ = batch
    sd_diag = to_sd_diag(state)

    def model_func_with_aux(p, x):
        return torch.func.functional_call(model, p, x), torch.tensor([])

    lin_mean, lin_chol, _ = posteriors.linearized_forward_diag(
        model_func_with_aux,
        state.params,
        x,
        sd_diag,
    )

    samps = torch.randn(
        lin_mean.shape[0],
        n_linearised_test_samples,
        lin_mean.shape[1],
        device=lin_mean.device,
    )
    lin_logits = lin_mean.unsqueeze(1) + samps @ lin_chol.transpose(-1, -2)
    return lin_logits


forward_dict = {"VI": forward, "VI Linearized": forward_linearized}
