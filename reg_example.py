#!/usr/bin/env python
# Ryan Turner (turnerry@iro.umontreal.ca)

import cPickle as pkl
import lasagne
import theano
import pymc3 as pm

import numpy as np
from scipy.misc import logsumexp
import theano.tensor as T
import hypernet_trainer as ht
import ign.ign as ign
from ign.t_util import make_shared_dict, make_unshared_dict

NOISE_STD = 0.02


def dm_example(N):
    x = 0.5 * np.random.rand(N, 1)

    noise = NOISE_STD * np.random.randn(N, 1)
    x_n = x + noise
    y = x + 0.3 * np.sin(2.0 * np.pi * x_n) + 0.3 * np.sin(4.0 * np.pi * x_n) \
        + noise
    return x, y


def unpack(v, weight_shapes):
    L = []
    tt = 0
    for ws in weight_shapes:
        num_param = np.prod(ws, dtype=int)
        L.append(v[tt:tt + num_param].reshape(ws))
        tt += num_param
    return L


def primary_net_f(X, theta, weight_shapes):
    L = unpack(theta, weight_shapes)
    n_layers, rem = divmod(len(L) - 1, 2)
    assert(rem == 0)

    yp = X
    for nn in xrange(n_layers):
        W, b = L[2 * nn], L[2 * nn + 1]
        assert(W.ndim == 2 and b.ndim == 1)
        act = T.dot(yp, W) + b[None, :]
        # Linear at final layer
        yp = act if nn == n_layers - 1 else T.maximum(0.0, act)

    log_prec = L[-1]
    assert(log_prec.ndim == 0)
    y_prec = T.exp(log_prec)
    return yp, y_prec


def primary_net_pm(X_train, Y_train, X_test, weight_shapes):
    N, D = X_train.get_value().shape
    assert(Y_train.get_value().shape == (N,))
    n_layers, rem = divmod(len(weight_shapes) - 1, 2)
    assert(rem == 0)

    yp_train = X_train
    yp_test = X_test
    var_names_order = [None] * len(weight_shapes)
    with pm.Model() as neural_network:
        for nn in xrange(n_layers):
            var_name = 'W_' + str(nn)
            W0_s = weight_shapes[2 * nn]
            assert(len(W0_s) == 2)
            var_names_order[2 * nn] = var_name
            W = pm.Normal(var_name, 0, sd=1, shape=W0_s)

            var_name = 'b_' + str(nn)
            b0_s = weight_shapes[2 * nn + 1]
            assert(len(b0_s) == 1)
            var_names_order[2 * nn + 1] = var_name
            b = pm.Normal(var_name, 0, sd=1, shape=b0_s)

            # TODO padleft, use pm. not T.
            act_train = T.dot(yp_train, W) + b[None, :]
            act_test = T.dot(yp_test, W) + b[None, :]

            # Linear at final layer
            yp_train = act_train if nn == n_layers - 1 else \
                T.maximum(0.0, act_train)
            yp_test = act_test if nn == n_layers - 1 else \
                T.maximum(0.0, act_test)
        assert(weight_shapes[-1] == ())
        var_name = 'log_prec'
        var_names_order[-1] = var_name
        log_prec = pm.Normal(var_name, 0, sd=1)
        pm.Deterministic('mu_test', yp_test)

        y_prec = T.exp(log_prec)
        pm.Normal('out', mu=yp_train, tau=y_prec, observed=Y_train)
    return neural_network, var_names_order


def hmc_net(X_train, Y_train, X_test, hypernet_f, weight_shapes,
            restarts=100, n_iter=500, init_scale_iter=1000):
    num_params = sum(np.prod(ws, dtype=int) for ws in weight_shapes)

    ann_input = theano.shared(X_train)
    ann_output = theano.shared(Y_train[:, 0])
    ann_input_test = theano.shared(X_test)
    ann_model, var_names = primary_net_pm(ann_input, ann_output,
                                          ann_input_test, weight_shapes)

    theta_scale = [hypernet_f(np.random.randn(num_params), False)
                   for _ in xrange(init_scale_iter)]
    scale_est = np.var(theta_scale, axis=0)
    assert(scale_est.shape == (num_params,))

    tr = [None] * restarts
    for rr in xrange(restarts):
        z_noise = np.random.randn(num_params)
        theta = hypernet_f(z_noise, False)
        start = dict(zip(var_names, unpack(theta, weight_shapes)))

        # Use deault step => use NUTS
        # TODO decide fairest way to deal with tuning period
        # TODO increase n_tune
        with ann_model:
            step = pm.NUTS(scaling=scale_est, is_cov=True)
            tr[rr] = pm.sampling.sample(draws=n_iter, step=step, start=start,
                                        progressbar=True, tune=10)

        # TODO now evaluate posterior predictive
    return tr


def hmc_pred(tr):
    n_samples = len(tr)
    assert(n_samples >= 1)
    n_iter, n_grid, _ = tr[0]['mu_test'].shape

    mu_grid = np.zeros((n_samples, n_iter, n_grid))
    y_grid = np.zeros((n_samples, n_iter, n_grid))
    for ss in xrange(n_samples):
        mu = tr[ss]['mu_test']
        log_prec = tr[ss]['log_prec']
        assert(mu.shape == (n_iter, n_grid, 1))
        assert(log_prec.shape == (n_iter,))

        std_dev = np.sqrt(1.0 / np.exp(log_prec))
        # Just store an MC version than do the mixture of Gauss logic
        mu_grid[ss, :, :] = mu[:, :, 0]
        # Note: using same noise across whole grid
        y_grid[ss, :, :] = mu[:, :, 0] + std_dev[:, None] * np.random.randn()

    # TODO make vectorized
    mu = np.zeros((n_iter, n_grid))
    LB = np.zeros((n_iter, n_grid))
    UB = np.zeros((n_iter, n_grid))
    for ii in xrange(n_iter):
        mu[ii, :] = np.mean(mu_grid[:, ii, :], axis=0)
        LB[ii, :] = np.percentile(y_grid[:, ii, :], 2.5, axis=0)
        UB[ii, :] = np.percentile(y_grid[:, ii, :], 97.5, axis=0)
    return mu, LB, UB


def loglik_primary_f(X, y, theta, weight_shapes):
    yp, y_prec = primary_net_f(X, theta, weight_shapes)
    err = yp - y

    loglik_c = -0.5 * np.log(2.0 * np.pi)
    loglik_cmp = 0.5 * T.log(y_prec)  # Hopefully Theano undoes log(exp())
    loglik_fit = -0.5 * y_prec * T.sum(err ** 2, axis=1)
    loglik = loglik_c + loglik_cmp + loglik_fit
    return loglik


def logprior_f(theta):
    # Standard Gauss
    logprior = -0.5 * T.sum(theta ** 2)  # Ignoring normalizing constant
    return logprior


def simple_test(X, y, X_valid, y_valid,
                n_epochs, n_batch, init_lr, weight_shapes,
                n_layers=5, vis_freq=100, n_samples=100,
                z_std=1.0):
    '''z_std = 1.0 gives the correct answer but other values might be good for
    debugging and analysis purposes.'''
    N, D = X.shape
    N_valid = X_valid.shape[0]
    assert(y.shape == (N, 1))  # Univariate for now
    assert(X_valid.shape == (N_valid, D) and y_valid.shape == (N_valid, 1))

    num_params = sum(np.prod(ws, dtype=int) for ws in weight_shapes)

    WL_init = 1e-2  # 10.0 ** (-2.0 / n_layers)
    layers = ign.init_ign_LU(n_layers, num_params, WL_val=WL_init)
    phi_shared = make_shared_dict(layers, '%d%s')

    ll_primary_f = lambda X, y, w: loglik_primary_f(X, y, w, weight_shapes)
    hypernet_f = lambda z, prelim=False: ign.network_T_and_J_LU(z[None, :], phi_shared, force_diag=prelim)[0][0, :]
    # TODO verify this length of size 1
    log_det_dtheta_dz_f = lambda z, prelim: T.sum(ign.network_T_and_J_LU(z[None, :], phi_shared, force_diag=prelim)[1])
    primary_f = lambda X, w: primary_net_f(X, w, weight_shapes)
    params_to_opt = phi_shared.values()
    R = ht.build_trainer(params_to_opt, N, ll_primary_f, logprior_f,
                         hypernet_f, primary_f=primary_f,
                         log_det_dtheta_dz_f=log_det_dtheta_dz_f)
    trainer, get_err, test_loglik, primary_out, grad_f, theta_f = R

    batch_order = np.arange(int(N / n_batch))

    burn_in = 100

    cost_hist = np.zeros(n_epochs)
    loglik_valid = np.zeros(n_epochs)
    for epoch in xrange(n_epochs):
        np.random.shuffle(batch_order)

        cost = 0.0
        for ii in batch_order:
            x_batch = X[ii * n_batch:(ii + 1) * n_batch]
            y_batch = y[ii * n_batch:(ii + 1) * n_batch]
            z_noise = z_std * np.random.randn(num_params)
            if epoch <= burn_in:
                current_lr = init_lr
                prelim = False
            else:
                decay_fac = 1.0 - (float(epoch - burn_in) / (n_epochs - burn_in))
                current_lr = init_lr * decay_fac
                prelim = False
            batch_cost = trainer(x_batch, y_batch, z_noise, current_lr, prelim)
            cost += batch_cost
        cost /= len(batch_order)
        print cost
        cost_hist[epoch] = cost

        loglik_valid_s = np.zeros((N_valid, n_samples))
        for ss in xrange(n_samples):
            z_noise = z_std * np.random.randn(num_params)
            loglik_valid_s[:, ss] = test_loglik(X_valid, y_valid, z_noise, False)
        loglik_valid_s_adj = loglik_valid_s - np.log(n_samples)
        loglik_valid[epoch] = np.mean(logsumexp(loglik_valid_s_adj, axis=1))
        print 'valid %f' % loglik_valid[epoch]

    phi = make_unshared_dict(phi_shared)
    return phi, cost_hist, loglik_valid, primary_out, grad_f, theta_f


def traditional_test(X, y, X_valid, y_valid, n_epochs, n_batch, init_lr, weight_shapes):
    '''z_std = 1.0 gives the correct answer but other values might be good for
    debugging and analysis purposes.'''
    N, D = X.shape
    N_valid = X_valid.shape[0]
    assert(y.shape == (N, 1))  # Univariate for now
    assert(X_valid.shape == (N_valid, D) and y_valid.shape == (N_valid, 1))

    num_params = sum(np.prod(ws, dtype=int) for ws in weight_shapes)
    phi_shared = make_shared_dict({'w': np.random.randn(num_params)})

    X_ = T.matrix('x')
    y_ = T.matrix('y')  # Assuming multivariate output
    lr = T.scalar('lr')

    loglik = loglik_primary_f(X_, y_, phi_shared['w'], weight_shapes)
    loss = -T.sum(loglik)
    params_to_opt = phi_shared.values()
    grads = T.grad(loss, params_to_opt)
    updates = lasagne.updates.adam(grads, params_to_opt, learning_rate=lr)

    test_loglik = theano.function([X_, y_], loglik)
    trainer = theano.function([X_, y_, lr], loss, updates=updates)
    primary_out = theano.function([X_], primary_net_f(X_, phi_shared['w'], weight_shapes))

    batch_order = np.arange(int(N / n_batch))

    cost_hist = np.zeros(n_epochs)
    loglik_valid = np.zeros(n_epochs)
    for epoch in xrange(n_epochs):
        np.random.shuffle(batch_order)

        cost = 0.0
        for ii in batch_order:
            x_batch = X[ii * n_batch:(ii + 1) * n_batch]
            y_batch = y[ii * n_batch:(ii + 1) * n_batch]
            current_lr = init_lr
            batch_cost = trainer(x_batch, y_batch, current_lr)
            cost += batch_cost
        cost /= len(batch_order)
        print cost
        cost_hist[epoch] = cost
        loglik_valid[epoch] = np.mean(test_loglik(X, y))
        print 'valid %f' % loglik_valid[epoch]

    phi = make_unshared_dict(phi_shared)
    return phi, cost_hist, loglik_valid, primary_out


if __name__ == '__main__':
    import matplotlib.pyplot as plt

    np.random.seed(5645)

    init_lr = 0.0005
    n_epochs = 1000
    n_batch = 32
    N = 1000
    z_std = 1.0  # 1.0 is correct for the model, 0.0 is MAP

    primary_layers = 1
    input_dim = 1
    hidden_dim = 50
    output_dim = 1

    weight_shapes = [(input_dim, hidden_dim), (hidden_dim,),
                     (hidden_dim, output_dim), (output_dim,), ()]

    X, y = dm_example(N)
    X_valid, y_valid = dm_example(N)

    n_samples = 500
    n_grid = 1000
    x_grid = np.linspace(-0.5, 1.5, n_grid)

    phi, cost_hist, loglik_valid, primary_out, grad_f, hypernet_f = \
        simple_test(X, y, X_valid, y_valid,
                    n_epochs, n_batch, init_lr, weight_shapes,
                    n_layers=3, z_std=z_std)

    phi_trad, cost_hist_trad, loglik_valid_trad, primary_out_trad = \
        traditional_test(X, y, X_valid, y_valid, n_epochs, n_batch, init_lr, weight_shapes)

    tr = hmc_net(X, y, x_grid[:, None], hypernet_f, weight_shapes, restarts=50, n_iter=20)
    mu_hmc, LB_hmc, UB_hmc = hmc_pred(tr)

    num_params = sum(np.prod(ws, dtype=int) for ws in weight_shapes)

    mu_trad, prec_trad = primary_out_trad(x_grid[:, None])
    std_dev_trad = np.sqrt(1.0 / prec_trad)

    mu_grid = np.zeros((n_samples, n_grid))
    y_grid = np.zeros((n_samples, n_grid))
    for ss in xrange(n_samples):
        z_noise = z_std * np.random.randn(num_params)
        mu, prec = primary_out(x_grid[:, None], z_noise, False)
        std_dev = np.sqrt(1.0 / prec)
        # Just store an MC version than do the mixture of Gauss logic
        mu_grid[ss, :] = mu[:, 0]
        # Note: using same noise across whole grid
        y_grid[ss, :] = mu[:, 0] + std_dev * np.random.randn()

    with open('reg_example_dump.pkl', 'wb') as f:
        pkl.dump((x_grid, mu_hmc, LB_hmc, UB_hmc, mu_grid, y_grid, mu_trad, std_dev_trad), f)

    _, (ax1, ax2) = plt.subplots(1, 2, sharex=True, sharey=True)
    ax1.plot(X[:100, :], y[:100], 'rx', zorder=0)
    ax1.plot(x_grid, mu_grid[:5, :].T, zorder=1, alpha=0.7)
    ax1.plot(x_grid, np.mean(mu_grid, axis=0), 'k', zorder=2)
    ax1.plot(x_grid, np.percentile(y_grid, 2.5, axis=0), 'k--', zorder=2)
    ax1.plot(x_grid, np.percentile(y_grid, 97.5, axis=0), 'k--', zorder=2)
    ax1.grid()
    ax1.set_title('hypernet')

    ax2.plot(X[:100, :], y[:100], 'rx', zorder=0)
    ax2.plot(x_grid, mu_trad, 'k', zorder=2)
    ax2.plot(x_grid, mu_trad - 2 * std_dev_trad, 'k--', zorder=2)
    ax2.plot(x_grid, mu_trad + 2 * std_dev_trad, 'k--', zorder=2)
    ax2.grid()
    ax2.set_title('traditional')
    plt.xlim([-0.2, 1.3])
    plt.ylim([-0.5, 1.2])

    plt.figure()
    plt.plot(loglik_valid, label='hypernet')
    plt.plot(loglik_valid_trad, label='traditional')
    plt.legend()
    plt.xlabel('epoch')
    plt.ylabel('validation log likelihood')
    plt.grid()

    plt.figure()
    plt.plot(cost_hist, label='hypernet')
    plt.xlabel('epoch')
    plt.ylabel('training cost (-ELBO)')
    plt.grid()
