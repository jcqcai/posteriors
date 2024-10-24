from typing import Callable, Any, Tuple, NamedTuple
from functools import partial
import torch
from torch.func import grad_and_value, vmap
from optree import tree_map
from optree.integration.torch import tree_ravel
import torchopt

from posteriors.types import TensorTree, Transform, LogProbFn
from posteriors.tree_utils import tree_size, tree_insert_
from posteriors.utils import (
    is_scalar,
    CatchAuxError,
    cholesky_from_log_flat,
    cholesky_to_log_flat,
)


def build(
    log_posterior: Callable[[TensorTree, Any], float],
    optimizer: torchopt.base.GradientTransformation,
    temperature: float = 1.0,
    n_samples: int = 1,
    stl: bool = True,
    init_cov: torch.Tensor | float = 1.0,
) -> Transform:
    """Builds a transform for variational inference with a Normal
    distribution over parameters.

    Find $\\mu$ and $\\Sigma$ that mimimize $\\text{KL}(N(θ| \\mu, \\Sigma) || p_T(θ))$
    where $p_T(θ) \\propto \\exp( \\log p(θ) / T)$ with temperature $T$.

    The log posterior and temperature are recommended to be [constructed in tandem](../../log_posteriors.md)
    to ensure robust scaling for a large amount of data.

    For more information on variational inference see [Blei et al, 2017](https://arxiv.org/abs/1601.00670).

    Args:
        log_posterior: Function that takes parameters and input batch and
            returns the log posterior (which can be unnormalised).
        optimizer: TorchOpt functional optimizer for updating the variational
            parameters. Make sure to use lower case like torchopt.adam()
        temperature: Temperature to rescale (divide) log_posterior.
        n_samples: Number of samples to use for Monte Carlo estimate.
        stl: Whether to use the stick-the-landing estimator
            from (Roeder et al](https://arxiv.org/abs/1703.09194).
        init_cov: Initial covariance matrix of the variational distribution.
        Can be a tensor matching params or scalar.

    Returns:
        Dense VI transform instance.
    """
    init_fn = partial(init, optimizer=optimizer, init_cov=init_cov)
    update_fn = partial(
        update,
        log_posterior=log_posterior,
        optimizer=optimizer,
        temperature=temperature,
        n_samples=n_samples,
        stl=stl,
    )
    return Transform(init_fn, update_fn)


class VIDenseState(NamedTuple):
    """State encoding a diagonal Normal variational distribution over parameters.

    Attributes:
        params: Mean of the variational distribution.
        cov: Covariance matrix of the variational distribution.
        log_chol: Flat representation of the nonzero values of the Cholesky
            of the covariance matrix of the variational distribution. The
            log of the diagonal is taken.
        opt_state: TorchOpt state storing optimizer data for updating the
            variational parameters.
        nelbo: Negative evidence lower bound (lower is better).
        aux: Auxiliary information from the log_posterior call.
    """

    params: TensorTree
    cov: torch.Tensor
    log_chol: torch.Tensor
    opt_state: torchopt.typing.OptState
    nelbo: torch.tensor = torch.tensor([])
    aux: Any = None


def init(
    params: TensorTree,
    optimizer: torchopt.base.GradientTransformation,
    init_cov: torch.Tensor | float = 1.0,
) -> VIDenseState:
    """Initialise diagonal Normal variational distribution over parameters.

    optimizer.init will be called on flattened variational parameters so hyperparameters
    such as learning rate need to pre-specified through TorchOpt's functional API:

    ```
    import torchopt

    optimizer = torchopt.adam(lr=1e-2)
    vi_state = init(init_mean, optimizer)
    ```

    It's assumed maximize=False for the optimizer, so that we minimize the NELBO.

    Args:
        params: Initial mean of the variational distribution.
        optimizer: TorchOpt functional optimizer for updating the variational
            parameters. Make sure to use lower case like torchopt.adam()
        init_cov: Initial covariance matrix of the variational distribution.
        Can be a tree matching params or scalar.

    Returns:
        Initial DenseVIState.
    """

    num_params = tree_size(params)
    if is_scalar(init_cov):
        init_cov = init_cov * torch.eye(num_params, requires_grad=True)

    init_chol = torch.linalg.cholesky(init_cov)
    init_log_chol = cholesky_to_log_flat(init_chol)

    opt_state = optimizer.init([params, init_log_chol])
    return VIDenseState(params, init_cov, init_log_chol, opt_state)


def update(
    state: VIDenseState,
    batch: Any,
    log_posterior: LogProbFn,
    optimizer: torchopt.base.GradientTransformation,
    temperature: float = 1.0,
    n_samples: int = 1,
    stl: bool = True,
    inplace: bool = False,
) -> VIDenseState:
    """Updates the variational parameters to minimize the NELBO.

    Args:
        state: Current state.
        batch: Input data to log_posterior.
        log_posterior: Function that takes parameters and input batch and
            returns the log posterior (which can be unnormalised).
        optimizer: TorchOpt functional optimizer for updating the variational
            parameters. Make sure to use lower case like torchopt.adam()
        temperature: Temperature to rescale (divide) log_posterior.
        n_samples: Number of samples to use for Monte Carlo estimate.
        stl: Whether to use the stick-the-landing estimator
            from (Roeder et al](https://arxiv.org/abs/1703.09194).
        inplace: Whether to modify state in place.

    Returns:
        Updated DenseVIState.
    """

    def nelbo_log_chol(m, chol):
        return nelbo(m, chol, batch, log_posterior, temperature, n_samples, stl)

    with torch.no_grad(), CatchAuxError():
        nelbo_grads, (nelbo_val, aux) = grad_and_value(
            nelbo_log_chol, argnums=(0, 1), has_aux=True
        )(state.params, state.log_chol)

    updates, opt_state = optimizer.update(
        nelbo_grads,
        state.opt_state,
        params=[state.params, state.log_chol],
        inplace=inplace,
    )
    mean, L_params = torchopt.apply_updates(
        (state.params, state.log_chol), updates, inplace=inplace
    )
    chol = cholesky_from_log_flat(L_params, state.cov.shape[0])
    cov = chol @ chol.T

    if inplace:
        tree_insert_(state.nelbo, nelbo_val.detach())
        tree_insert_(state.cov, cov)
        return state._replace(aux=aux)

    return VIDenseState(mean, cov, L_params, opt_state, nelbo_val.detach(), aux)


def nelbo(
    mean: dict,
    log_chol: torch.Tensor,
    batch: Any,
    log_posterior: LogProbFn,
    temperature: float = 1.0,
    n_samples: int = 1,
    stl: bool = True,
) -> Tuple[float, Any]:
    """Returns the negative evidence lower bound (NELBO) for a Normal
    variational distribution over the parameters of a model.

    Monte Carlo estimate with `n_samples` from q.
    $$
    \\text{NELBO} = - 𝔼_{q(θ)}[\\log p(y|x, θ) + \\log p(θ) - \\log q(θ) * T])
    $$
    for temperature $T$.

    `log_posterior` expects to take parameters and input batch and return a scalar
    as well as a TensorTree of any auxiliary information:

    ```
    log_posterior_eval, aux = log_posterior(params, batch)
    ```

    The log posterior and temperature are recommended to be [constructed in tandem](../../log_posteriors.md)
    to ensure robust scaling for a large amount of data and variable batch size.

    Args:
        mean: Mean of the variational distribution.
        sd_diag: Square-root diagonal of the covariance matrix of the
            variational distribution.
        batch: Input data to log_posterior.
        log_posterior: Function that takes parameters and input batch and
            returns the log posterior (which can be unnormalised).
        temperature: Temperature to rescale (divide) log_posterior.
        n_samples: Number of samples to use for Monte Carlo estimate.
        stl: Whether to use the stick-the-landing estimator
            from (Roeder et al](https://arxiv.org/abs/1703.09194).

    Returns:
        The sampled approximate NELBO averaged over the batch.
    """

    mean_flat, unravel_func = tree_ravel(mean)
    num_params = mean_flat.shape[0]
    chol = cholesky_from_log_flat(log_chol, num_params)
    cov = chol @ chol.T
    dist = torch.distributions.MultivariateNormal(
        loc=mean_flat,
        covariance_matrix=cov,
        validate_args=False,
    )

    sampled_params = dist.rsample((n_samples,))
    sampled_params_tree = torch.vmap(lambda s: unravel_func(s))(sampled_params)

    if stl:
        mean_flat.detach()
        chol = log_chol.detach()
        chol = cholesky_from_log_flat(chol, num_params)
        cov = chol @ chol.T
        # Redefine distribution to sample from after stl
        dist = torch.distributions.MultivariateNormal(
            loc=mean_flat,
            covariance_matrix=cov,
            validate_args=False,
        )

    # Don't use vmap for single sample, since vmap doesn't work with lots of models
    if n_samples == 1:
        single_param = tree_map(lambda x: x[0], sampled_params_tree)
        log_p, aux = log_posterior(single_param, batch)
        log_q = dist.log_prob(single_param)

    else:
        log_p, aux = vmap(log_posterior, (0, None), (0, 0))(sampled_params_tree, batch)
        log_q = dist.log_prob(sampled_params)

    return -(log_p - log_q * temperature).mean(), aux


def sample(
    state: VIDenseState, sample_shape: torch.Size = torch.Size([])
) -> TensorTree:
    """Single sample from Normal distribution over parameters.

    Args:
        state: State encoding mean and covariance matrix.
        sample_shape: Shape of the desired samples.

    Returns:
        Sample(s) from Normal distribution.
    """

    mean_flat, unravel_func = tree_ravel(state.params)

    samples = torch.distributions.MultivariateNormal(
        loc=mean_flat,
        covariance_matrix=state.cov,
        validate_args=False,
    ).rsample(sample_shape)

    samples = torch.vmap(unravel_func)(samples)
    return samples