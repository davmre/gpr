from treegp.bcm.multi_shared_bcm import MultiSharedBCM, Blocker, sample_synthetic
from treegp.bcm.local_regression import BCM

from treegp.gp import GPCov, GP, mcov, prior_sample, dgaussian
from treegp.util import mkdir_p
import numpy as np
import scipy.stats
import scipy.optimize
import time
import os
import sys
import cPickle as pickle

import argparse

EXP_DIR = os.path.join(os.environ["HOME"], "bcmopt_experiments")

def sample_data(n, ntrain, lscale, obs_std, yd, seed, centers, noise_var):
    sample_basedir = os.path.join(os.environ["HOME"], "bcmopt_experiments", "synthetic_datasets")
    mkdir_p(sample_basedir)
    sample_fname = "%d_%d_%.2f_%.3f_%d_%d%s.pkl" % (n, ntrain, lscale, obs_std, yd, seed, "" if noise_var==0.01 else "_%.4f" % noise_var)
    sample_fname_full = os.path.join(sample_basedir, sample_fname)

    try:
        with open(sample_fname_full, 'rb') as f:
            sdata = pickle.load(f)
    except IOError:
        sdata = SampledData(n=n, ntrain=ntrain, lscale=lscale, obs_std=obs_std, seed=seed, centers=None, yd=yd, noise_var=noise_var)

        with open(sample_fname_full, 'wb') as f:
            pickle.dump(sdata, f)


    sdata.set_centers(centers)
    return sdata

class OutOfTimeError(Exception):
    pass

class SampledData(object):

    def __init__(self,
                 noise_var=0.01, n=30, ntrain=20, lscale=0.5,
                 obs_std=0.05, yd=10, centers=None, seed=1):
        self.noise_var=noise_var
        self.n = n
        self.ntrain = ntrain
        self.lscale=lscale

        Xfull, Yfull, cov = sample_synthetic(n=n, noise_var=noise_var, yd=yd, lscale=lscale, seed=seed)
        self.cov = cov
        X, Y = Xfull[:ntrain,:], Yfull[:ntrain,:]
        self.Xtest, self.Ytest = Xfull[ntrain:,:], Yfull[ntrain:,:]

        self.SX, self.SY = X, Y
        self.block_boundaries = [(0, X.shape[0])]
        self.centers = [np.array((0.0, 0.0))]

        self.obs_std = obs_std
        np.random.seed(seed)
        self.X_obs = self.SX + np.random.randn(*X.shape)*obs_std

    def set_centers(self, centers):
        b = Blocker(centers)
        self.SX, self.SY, self.perm, self.block_boundaries = b.sort_by_block(self.SX, self.SY)
        self.centers = centers
        self.X_obs = self.X_obs[self.perm]

    def build_mbcm(self, X=None, cov=None, local_dist=1e-4):
        if X is None:
            X = self.SX #self.X_obs

        if cov is None:
            cov = self.cov
            noise_var = self.noise_var
        elif cov.shape[0]==1:
            noise_var = cov[0, 0]
            cov = GPCov(wfn_params=[cov[0,1]], dfn_params=cov[0,2:], dfn_str="euclidean", wfn_str="se")
        else:
            raise Exception("invalid cov params %s" % (cov))

        mbcm = MultiSharedBCM(X, Y=self.SY, block_boundaries=self.block_boundaries,
                              cov=cov, noise_var=noise_var,
                              kernelized=False, neighbor_threshold=local_dist)
        return mbcm

    def mean_abs_err(self, x):
        return np.mean(np.abs(x - self.SX.flatten()))

    def lscale_error(self, FC):
        true_lscale = self.cov.dfn_params[0]
        inferred_lscale = FC[0, 2]
        return np.abs(inferred_lscale-true_lscale)/true_lscale

    def prediction_error_gp(self, x):
        XX = x.reshape(self.X_obs.shape)
        ntest = self.n-self.ntrain
        ll = 0

        gp = GP(X=XX, y=self.SY[:, 0:1], cov_main=self.cov, noise_var=self.noise_var,
                sort_events=False, sparse_invert=False)
        pred_cov = gp.covariance(self.Xtest, include_obs=True)
        logdet = np.linalg.slogdet(pred_cov)[1]
        pred_prec = np.linalg.inv(pred_cov)

        for y, yt in zip(self.SY.T, self.Ytest.T):
            gp.y = y
            gp.alpha_r = gp.factor(y)
            pred_means = gp.predict(self.Xtest)
            rt = yt - pred_means

            lly =  -.5 * np.dot(rt, np.dot(pred_prec, rt))
            lly += -.5 * logdet
            lly += -.5 * ntest * np.log(2*np.pi)

            ll += lly

        return ll

    def prediction_error_bcm(self, X=None, cov=None, local_dist=1.0, marginal=False):
        ntest = self.n-self.ntrain
        yd = self.SY.shape[1]
        mbcm = self.build_mbcm(X=X, cov=cov, local_dist=local_dist)

        p = mbcm.train_predictor()
        if marginal:
            ll = 0
            for xt, yt in zip(self.Xtest, self.Ytest):
                PM, PC = p(xt.reshape((1, -1)), test_noise_var=self.noise_var)
                PP = np.linalg.inv(PC)
                PR = np.reshape(yt, (1, -1))-PM
                ll -= .5 * np.sum(PP *  np.dot(PR, PR.T))
                ll -= .5 * yd * np.linalg.slogdet(PC)[1]
                ll -= .5 * yd * np.log(2*np.pi)
        else:
            PM, PC = p(self.Xtest, test_noise_var=self.noise_var)
            PP = np.linalg.inv(PC)
            PR = self.Ytest-PM

            ll =  -.5 * np.sum(PP *  np.dot(PR, PR.T))
            ll += -.5 * yd * np.linalg.slogdet(PC)[1]
            ll += -.5 * ntest * yd * np.log(2*np.pi)

        return ll / (ntest * yd)
        

    def x_prior(self, xx):
        flatobs = self.X_obs.flatten()
        t0 = time.time()

        n = len(xx)
        r = (xx-flatobs)/self.obs_std
        ll = -.5*np.sum( r**2)- .5 *n * np.log(2*np.pi*self.obs_std**2)

        lderiv = np.array([-(xx[i]-flatobs[i])/(self.obs_std**2) for i in range(len(xx))]).flatten()
        t1 = time.time()
        return ll, lderiv

    def random_init(self, jitter_std=None):
        if jitter_std is None:
            jitter_std = self.obs_std
        return self.X_obs + np.random.randn(*self.X_obs.shape)*jitter_std


def do_optimization(d, mbcm, X0, C0, sdata, method, maxsec=3600, parallel=False):

    def cov_prior(c):
        mean = -1
        std = 3
        r = (c-mean)/std
        ll = -.5*np.sum( r**2)- .5 *len(c) * np.log(2*np.pi*std**2)
        lderiv = -(c-mean)/(std**2)
        return ll, lderiv

    def full_cov(C):
        if C.shape[1] == 1:
            # lscale
            FC = np.empty((C0.shape[0], 2+sdata.X_obs.shape[1]))
            FC[:, 0] = sdata.noise_var
            FC[:, 1] = 1.0
            FC[:, 2:3] = C
            FC[:, 3:4] = C
        elif C.shape[1] == 4:
            FC = C
        else:
            raise Exception("unrecognized cov param shape")
        return FC

    def collapse_cov_grad(grad_FC):
        if C0.shape[1] == 1:
            # lscale
            gradC = grad_FC[:, 2:3] + grad_FC[:, 3:4]
        elif C0.shape[1] == 4:
            gradC = grad_FC
        else:
            raise Exception("unrecognized cov param shape")
        return gradC

    gradX = (X0 is not None)
    gradC = (C0 is not None)

    if gradX:
        x0 = X0.flatten()
    else:
        x0 = np.array(())

    if gradC:
        c0 = np.log(C0.flatten())
    else:
        c0 = np.array(())
    full0 = np.concatenate([x0, c0])

    sstep = [0,]
    f_log = open(os.path.join(d, "log.txt"), 'w')
    t0 = time.time()

    def lgpllgrad(x):

        xx = x[:len(x0)]
        xc = x[len(x0):]

        if gradX:
            XX = xx.reshape(X0.shape)
            mbcm.update_X(XX)
            np.save(os.path.join(d, "step_%05d_X.npy" % sstep[0]), XX)
        if gradC:
            C = np.exp(xc.reshape(C0.shape))
            FC = full_cov(C)
            print FC
            mbcm.update_covs(FC)
            np.save(os.path.join(d, "step_%05d_cov.npy" % sstep[0]), FC)

        ll, gX, gC = mbcm.llgrad(local=True, grad_X=gradX, grad_cov=gradC,
                                       parallel=parallel)

        if gradX:
            prior_ll, prior_grad = sdata.x_prior(xx)
            ll += prior_ll
            gX = gX.flatten() + prior_grad
        if gradC:
            prior_ll, prior_grad = cov_prior(xc)
            ll += prior_ll
            gC = (np.array(collapse_cov_grad(gC)) * C).flatten() + prior_grad

        grad = np.concatenate([gX.flatten(), gC.flatten()])

        print "%d %.2f %.2f" % (sstep[0], time.time()-t0, ll)
        f_log.write("%d %.2f %.2f\n" % (sstep[0], time.time()-t0, ll))
        f_log.flush()

        sstep[0] += 1

        if time.time()-t0 > maxsec:
            raise OutOfTimeError

        return -ll, -grad

    bounds = [(0.0, 1.0),]*len(x0) + [(-10, 5)]*len(c0)
    try:
        r = scipy.optimize.minimize(lgpllgrad, full0, jac=True, method=method, bounds=bounds)
        rx = r.x
    except OutOfTimeError:
        print "terminated optimization for time"

    t1 = time.time()
    f_log.write("optimization finished after %.fs\n" % (time.time()-t0))
    f_log.close()

    with open(os.path.join(d, "finished"), 'w') as f:
        f.write("")


def load_log(d):
    log = os.path.join(d, "log.txt")
    steps = []
    times = []
    lls = []
    with open(log, 'r') as lf:
        for line in lf:
            try:
                step, time, ll = line.split(' ')
                steps.append(int(step))
                times.append(float(time))
                lls.append(float(ll))
            except:
                continue

    return np.asarray(steps), np.asarray(times), np.asarray(lls)

def analyze_run(d, sdata, local_dist=1.0):

    steps, times, lls = load_log(d)

    rfname = os.path.join(d, "results.txt")
    results = open(rfname, 'w')
    print "writing results to", rfname
    for i, step in enumerate(steps):
        try:
            fname_X = os.path.join(d, "step_%05d_X.npy" % step)
            X = np.load(fname_X)
        except IOError:
            X = sdata.SX

        try:
            fname_cov = os.path.join(d, "step_%05d_cov.npy" % step)
            FC = np.load(fname_cov)
        except IOError:
            FC = None

        l1 = sdata.mean_abs_err(X.flatten())
        c1 = sdata.lscale_error(FC) if FC is not None else 0.00
        l2 = sdata.x_prior(X.flatten())[0]
        p1 = sdata.prediction_error_bcm(X=X, cov=FC, local_dist=1.0)
        p2 = sdata.prediction_error_bcm(X=X, cov=FC, marginal=True)
        s = "%d %.2f %.2f %.4f %.4f %.4f %.4f %.4f" % (step, times[i], lls[i], c1, l1, l2, p1, p2)
        print s
        results.write(s + "\n")

    X = sdata.SX
    l1 = sdata.mean_abs_err(X.flatten()) # = 0.0
    c1 = 0.0
    l2 = sdata.x_prior(X.flatten())[0]
    p1 = sdata.prediction_error_bcm(X=X, cov=None, local_dist=1.0)
    p2 = sdata.prediction_error_bcm(X=X, cov=None, marginal=True)
    mbcm = sdata.build_mbcm(X=X, local_dist=local_dist)
    ll1 = mbcm.llgrad()[0]
    s = "trueX inf %.2f %.4f %.4f %.4f %.4f %.4f" % (ll1, c1, l1, l2, p1, p2)
    print s
    results.write(s + "\n")
    results.close()

def do_run(d, lscale, n, ntrain, nblocks, yd, seed=0,
           fullgp=False, method=None,
           obs_std=None, local_dist=1.0, maxsec=3600,
           task='x', analyze_only=False, init_seed=-1, noise_var=0.01):

    pmax = np.ceil(np.sqrt(nblocks))*2+1
    pts = np.linspace(0, 1, pmax)[1::2]
    centers = [np.array((xx, yy)) for xx in pts for yy in pts]
    print "bcm with %d blocks" % (len(centers))

    if obs_std is None:
        obs_std = lscale/10

    data = sample_data(n=n, ntrain=ntrain, lscale=lscale, obs_std=obs_std, yd=yd, seed=seed, centers=centers, noise_var=noise_var)
    mbcm = data.build_mbcm(local_dist=local_dist)

    if task=='x':
        X0 = data.X_obs
        C0 = None
    elif task == 'cov':
        X0 = None
        if init_seed >= 0:
            np.random.seed(init_seed)
            C0 = np.exp(np.random.randn(1, 4)-1)
        else:
            C0 = np.array((0.1, 1.0, 0.3,  0.3)).reshape(1,-1)
    elif task =='xcov':
        X0 = data.X_obs
        if init_seed >= 0:
            np.random.seed(init_seed)
            C0 = np.exp(np.random.randn(1, 1)-1)
            X0 = X0 + np.random.randn(*X0.shape)*0.005
        else:
            C0 = np.array((0.3)).reshape(1,1)
    else:
        raise Exception("unrecognized task "+ task)

    if analyze_only:
        analyze_run(d, data, local_dist=local_dist)
    else:
        do_optimization(d, mbcm, X0, C0, data, method=method, maxsec=maxsec)


    """
    load the log
    """

def build_run_name(args):
    try:
        ntrain, n, nblocks, lscale, obs_std, local_dist, yd, method, task, init_seed, noise_var = (args.ntrain, args.n, args.nblocks, args.lscale, args.obs_std, args.local_dist, args.yd, args.method, args.task, args.init_seed, args.noise_var)
    except:
        defaults = { 'yd': 50, 'seed': 0, 'local_dist': 0.05, "method": 'l-bfgs-b', 'task': 'x', 'init_seed': -1, 'noise_var': 0.01}
        defaults.update(args)
        args = defaults
        ntrain, n, nblocks, lscale, obs_std, local_dist, yd, method, task, init_seed, noise_var = (args['ntrain'], args['n'], args['nblocks'], args['lscale'], args['obs_std'], args['local_dist'], args['yd'], args['method'], args['task'], args['init_seed'], args['noise_var'])
    run_name = "%d_%d_%d_%.2f_%.2f_%.3f_%d_%s_%s_%d%s" % (ntrain, n, nblocks, lscale, obs_std, local_dist, yd, method, task, init_seed, "" if noise_var==0.01 else "%.4f" % noise_var)
    return run_name

def exp_dir(args):
    run_name = build_run_name(args)
    exp_dir = os.path.join(EXP_DIR, run_name)
    mkdir_p(exp_dir)
    return exp_dir

def main():

    mkdir_p(EXP_DIR)

    MAXSEC=3600

    parser = argparse.ArgumentParser(description='bcmopt')
    parser.add_argument('--ntrain', dest='ntrain', type=int)
    parser.add_argument('--n', dest='n', type=int)
    parser.add_argument('--nblocks', dest='nblocks', default=1, type=int)
    parser.add_argument('--lscale', dest='lscale', type=float)
    parser.add_argument('--obs_std', dest='obs_std', type=float)
    parser.add_argument('--local_dist', dest='local_dist', default=1.0, type=float)
    parser.add_argument('--method', dest='method', default="l-bfgs-b", type=str)
    parser.add_argument('--seed', dest='seed', default=0, type=int)
    parser.add_argument('--yd', dest='yd', default=50, type=int)
    parser.add_argument('--maxsec', dest='maxsec', default=3600, type=int)
    parser.add_argument('--task', dest='task', default="x", type=str)
    parser.add_argument('--analyze', dest='analyze', default=False, action="store_true")
    parser.add_argument('--init_seed', dest='init_seed', default=-1, type=int)
    parser.add_argument('--noise_var', dest='noise_var', default=0.01, type=float)

    args = parser.parse_args()

    d = exp_dir(args)
    do_run(d=d, lscale=args.lscale, obs_std=args.obs_std, local_dist=args.local_dist, n=args.n, ntrain=args.ntrain, nblocks=args.nblocks, yd=args.yd, method=args.method, seed=args.seed, maxsec=args.maxsec, analyze_only=args.analyze, task=args.task, init_seed=args.init_seed, noise_var=args.noise_var)

if __name__ == "__main__":
    main()
