import numpy as np
import scipy.stats

from collections import defaultdict
from multiprocessing import Pool

import sys, os
try:
    sghome = os.environ['SIGVISA_HOME']
    if sghome not in sys.path:
        sys.path.append(sghome)
except:
    pass

from treegp.gp import GP, GPCov, mcov,  prior_sample

from treegp.cover_tree import VectorTree
import pyublas


def check_inv(prec, K):
    k1 = np.abs(np.dot(prec[0, :], K[:, 0]) - 1)
    k2 = np.abs(np.dot(prec[0, :], K[:, 1]))
    k3 = np.abs(np.dot(prec[1, :], K[:, 0]))
    k4 = np.abs(np.dot(prec[1, :], K[:, 1]) - 1)
    numerical_error = np.max((k1, k2, k3, k4))
    return numerical_error

class Blocker(object):

    def __init__(self, block_centers):
        self.block_centers = block_centers
        self.n_blocks = len(block_centers)

    def get_block(self, X_new):
        dists = [np.linalg.norm(X_new - center) for center in self.block_centers]
        return np.argmin(dists)

    def sort_by_block(self, X, Y=None, YY=None):
        if Y is not None:
            assert(Y.shape[0] == X.shape[0])
        elif YY is not None:
            assert(YY.shape[0] == X.shape[0])
            assert(YY.shape[1] == X.shape[0])

        n = X.shape[0]
        blocks = np.array([self.get_block(xp) for xp in X])
        idxs = np.arange(n)
        perm = np.asarray(sorted(idxs, key = lambda i : blocks[i]))
        sorted_X = X[perm]
        sorted_blocks = blocks[perm]
        block_idxs = [idxs[sorted_blocks==block] for block in np.arange(self.n_blocks)]
        block_boundaries = [ (np.min(bi), np.max(bi)+1)  for bi in block_idxs  ]

        if Y is not None:
            sorted_Y = Y[perm]
            return sorted_X, sorted_Y, perm, block_boundaries
        elif YY is not None:
            tmp = YY[perm]
            sorted_YY = tmp[:, perm]
            return sorted_X, sorted_YY, perm, block_boundaries
        else:
            return sorted_X, perm, block_boundaries

def sample_crazy_shape(seed, n, std=0.005):

    np.random.seed(seed)

    def sample_X(n=1000):
        X1 = sample_points_line(n/2, (0.1, 0.1), (0.9, 0.9))
        X2 = sample_points_line(n/2, (0.1, 0.9), (0.9, 0.1))
        return np.vstack([X1, X2])

    def sample_diamond(n=1000):
        X1 = sample_points_line(n/4, (0.5, 0.9), (0.9, 0.5))
        X2 = sample_points_line(n/4, (0.5, 0.9), (0.1, 0.5))
        X3 = sample_points_line(n/4, (0.1, 0.5), (0.5, 0.1))
        X4 = sample_points_line(n/4, (0.5, 0.1), (0.9, 0.5))
        return np.vstack([X1, X2, X3, X4])

    def sample_star(points=10, n=1000):
        Xs = []
        angles = (2*np.pi)/points
        for i in range(points):
            x1 = np.array((0.5, 0.5))
            theta = i * angles
            v = np.array((np.cos(theta), np.sin(theta)))
            v = 0.4 * v / np.linalg.norm(v)
            X1 = sample_points_line(n/4, x1, x1+v)
            Xs.append(X1)
        return np.vstack(Xs)

    def sample_points_line(n, x1, x2, std=0.005):
        x2 = np.array(x2)
        x1 = np.array(x1)
        v = x2-x1
        rs = np.random.rand(n)
        pts = np.array([x1 + r*v for r in rs])
        X = pts + np.random.randn(*pts.shape) * std
        return X

    def sample_fault(n=1200, std=0.005):
        sn = n/10
        Xs = []
        x1 = np.array((0.1, 0.1))
        x2 = np.array((0.2, 0.2))
        Xs.append(sample_points_line(sn, x1, x2))
        x3 = np.array((0.2, 0.5))
        Xs.append(sample_points_line(sn, x2, x3))

        x4 = np.array((0.3, 0.3))
        Xs.append(sample_points_line(sn, x2, x4))

        x5 = np.array((0.5, 0.1))
        Xs.append(sample_points_line(sn, x4, x5))

        x6 = np.array((0.4, 0.45))
        Xs.append(sample_points_line(sn, x4, x6))
        x7 = np.array((0.2, 0.8))
        Xs.append(sample_points_line(sn, x6, x7))

        x8 = np.array((0.5, 0.6))
        Xs.append(sample_points_line(sn, x6, x8))
        x9 = np.array((0.9, 0.4))
        Xs.append(sample_points_line(sn, x8, x9))
        x10 = np.array((0.8, 0.9))
        Xs.append(sample_points_line(sn, x8, x10))
        x11 = np.array((0.8, 0.1))
        Xs.append(sample_points_line(sn, x9, x11))
        return np.vstack(Xs)

    if seed < 1100:
        return sample_fault(n=n)
    elif seed < 1200:
        return sample_X(n=n)
    elif seed < 1300:
        return sample_diamond(n=n)


def sample_synthetic(seed=1, n=400, xd=2, yd=10, lscale=0.1, noise_var=0.01):
    # sample data from the prior
    if seed < 1000:
        np.random.seed(seed)
        X = np.random.rand(n, xd)
    else:
        X = sample_crazy_shape(seed, n)
        assert(X.shape[0]==n)

    cov = GPCov(wfn_params=[1.0], dfn_params=[lscale, lscale], dfn_str="euclidean", wfn_str="se")
    KK = mcov(X, cov, noise_var)

    L = np.linalg.cholesky(KK)
    Z = np.random.randn(X.shape[0], yd)
    y = np.dot(L, Z)

    #y = scipy.stats.multivariate_normal(mean=np.zeros((X.shape[0],)), cov=KK).rvs(yd).T.reshape((-1, yd))

    return X, y, cov

"""
def sample_synthetic_bcm_new(seed=1, n=400, xd=2, yd=10, lscale=0.1, noise_var=0.01, blocker=None, nonstationary_hparam_covs = None):
    # sample data from the prior
    np.random.seed(seed)
    X = np.random.rand(n, xd)

    SX, perm, block_boundaries = blocker.sort_by_block(X)

    if nonstationary_hparam_covs is not None:
        centers = np.array(blocker.block_centers)
        n_blocks = centers.shape[0]

        nvmean, nvcov = nonstationary_hparam_covs["noise_var"]
        block_nv = np.exp(prior_sample(centers, nvcov, 0.0) + nvmean)

        svmean, svcov = nonstationary_hparam_covs["signal_var"]
        block_sv = np.exp(prior_sample(centers, svcov, 0.0) + svmean)

        lsmean, lscov = nonstationary_hparam_covs["lscale"]
        block_lscale = np.exp(prior_sample(centers, lscov, 0.0)+ lsmean)

        print "noise levels", block_nv
        print "signal levels", block_sv
        print "lscale", block_lscale

        covs = [GPCov(wfn_params=[block_sv[i]], dfn_params=[block_lscale[i],] * xd, dfn_str="euclidean", wfn_str="se") for i in range(n_blocks)]
    else:
        cov = GPCov(wfn_params=[1.0], dfn_params=[lscale,] * xd, dfn_str="euclidean", wfn_str="se")

    Y = []
    Ks = []
    Js = []
    Kstar_precs = [] # (i,j) is K_{ij} * K_jj^{-1}
    pred_precs = [] # (i,j) is inv(K_ii - K_{ij} K_jj^{-1} K_{ji})
    combined_covs = [] # [i] is the covariance of p(Y_i |_B Y_:i), i.e. the covariance of the BCM conditional
    combined_chols = [] # cholesky decompositions of combined_covs
    for i, (i_start, i_end) in enumerate(block_boundaries):
        if nonstationary_hparam_covs is not None:
            cov = covs[i]
            noise_var = block_nv[i]

        Xi = SX[i_start:i_end, :]
        Ki = mcov(Xi, cov, noise_var)
        Ji = np.linalg.inv(Ki)

        Ks.append(Ki)
        Js.append(Ji)

        precs = []
        Ksprecs_j = []
        pprecs_j = []
        for j, (j_start, j_end) in enumerate(block_boundaries[:i]):
            Xj = SX[j_start:j_end, :]
            Kj = Ks[j]
            Jj = Js[j]

            if nonstationary_hparam_covs is not None:
                cov_j = covs[j]
            else:
                cov_j = cov

            Kstar = mcov(Xi, cov_j, noise_var, X2=Xj)
            Kstar_prec = np.dot(Kstar, Jj)

            if nonstationary_hparam_covs is None:
                pred_cov = Ki - np.dot(Kstar_prec, Kstar.T)
                pred_prec = np.linalg.inv(pred_cov)
                message_prec = pred_prec - Ji
            else:
                Kij = mcov(Xi, cov_j, block_nv[j])
                Jij = np.linalg.inv(Kij)
                pred_cov = Kij - np.dot(Kstar_prec, Kstar.T)
                pred_prec = np.linalg.inv(pred_cov)
                message_prec = pred_prec - Jij

            precs.append(message_prec)

            if np.max(np.abs(Kstar_prec)) < 1e-3:
                pprecs_j.append(None)
                Ksprecs_j.append(None)
            else:
                pprecs_j.append(pred_prec)
                Ksprecs_j.append(Kstar_prec)

        pred_precs.append(pprecs_j)
        Kstar_precs.append(Ksprecs_j)

        combined_prec = np.sum(precs, axis=0) + Ji
        combined_cov = np.linalg.inv(combined_prec)
        combined_chol = np.linalg.cholesky(combined_cov)
        combined_covs.append(combined_cov)
        combined_chols.append(combined_chol)
        print "precomputed covs for block", i
    Y = []
    for d in range(yd):
        yis = []
        for i, (i_start, i_end) in enumerate(block_boundaries):
            means = [np.zeros((i_end-i_start,))]
            for j, (j_start, j_end) in enumerate(block_boundaries[:i]):
                if Kstar_precs[i][j] is not None:
                    pred_mean = np.dot(Kstar_precs[i][j], yis[j])
                    weighted_mean = np.dot(pred_precs[i][j], pred_mean)
                    means.append(weighted_mean)
            mean = np.dot(combined_covs[i],  np.sum(means, axis=0))

            yi = np.dot(combined_chols[i], np.random.randn(i_end-i_start)) + mean
            yis.append(yi)
        print "sampled y", d
        Yd = np.concatenate(yis).reshape((-1, 1))
        Y.append(Yd)
    Y = np.hstack(Y)

    perm = np.random.permutation(n)
    SX = SX[perm]
    Y = Y[perm]

    if nonstationary_hparam_covs is None:
        return SX, Y, cov
    else:
        return SX, Y, cov, block_nv, block_sv, block_lscale
"""

def pair_distances(Xi, Xj):
    return np.sqrt(np.outer(np.sum(Xi**2, axis=1), np.ones((Xj.shape[0]),)) - 2*np.dot(Xi, Xj.T) + np.outer((np.ones(Xi.shape[0]),), np.sum(Xj**2, axis=1)))

class MultiSharedBCM(object):

    def __init__(self, X, Y, block_boundaries, cov, noise_var, kernelized=False, dy=None,
                 neighbor_threshold=1e-3, nonstationary=False, nonstationary_prec=False,
                 block_centers=None, diag_connections=False):
        self.X = X

        if kernelized:
            self.kernelized = True
            self.YY = Y
            assert(dy is not None)
            self.dy = dy
        else:
            self.kernelized = False
            self.Y = Y
        self.block_boundaries = block_boundaries
        self.n_blocks = len(block_boundaries)

        self.nonstationary = nonstationary
        if not nonstationary:
            self.cov = cov
            self.noise_var = noise_var
            dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
            self.predict_tree = VectorTree(dummy_X, 1, cov.dfn_str, cov.dfn_params, cov.wfn_str, cov.wfn_params)
        else:
            dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
            predict_tree = VectorTree(dummy_X, 1, cov.dfn_str, cov.dfn_params, cov.wfn_str, cov.wfn_params)

            self.block_covs = [(noise_var, cov) for i in range(self.n_blocks)]
            self.block_trees = [predict_tree for i in range(self.n_blocks)]
            self.nonstationary_prec=nonstationary_prec

        if block_centers is not None:
            self.block_neighbors(block_centers, diag=diag_connections)
        else:
            self.compute_neighbors(threshold=neighbor_threshold)
        self.neighbor_threshold = neighbor_threshold

    def block_neighbors(self, centers, diag=False, threshold=1e-3):
        neighbor_count = defaultdict(int)
        neighbors = []

        if threshold < 1e-8:
            self.neighbor_count = neighbor_count
            self.neighbors = neighbors
            return

        center_distances = pair_distances(centers, centers)
        min_dist = np.min(center_distances) + 1e-6
        diag_dist = np.min(center_distances[center_distances > min_dist]) + 1e-6
        connect_dist = diag_dist if diag else min_dist

        for i in range(self.n_blocks):
            for j in range(i):
                if center_distances[i,j] < connect_dist:
                    neighbors.append((i,j))
                    neighbor_count[i] += 1
                    neighbor_count[j] += 1

        self.neighbor_count = neighbor_count
        self.neighbors = neighbors

    def compute_neighbors(self, threshold=1e-3, threshold_Q=False):
        neighbor_count = defaultdict(int)
        neighbors = []

        if threshold < 1e-8:
            self.neighbor_count = neighbor_count
            self.neighbors = neighbors
            return


        #D = pair_distances(self.X, self.X)


        wfn_var = self.cov.wfn_params[0]

        for i in range(self.n_blocks):
            i_start, i_end = self.block_boundaries[i]
            X1 = self.X[i_start:i_end]
            for j in range(i):
                j_start, j_end = self.block_boundaries[j]
                X2 = self.X[j_start:j_end]

                if threshold_Q:
                    ni = i_end-i_start
                    nj - j_end-j_start
                    n = ni+nj
                    K = np.empty((n,n))
                    K[:i_end, :i_end] = self.kernel(X1)/wfn_var
                    K[i_end:, i:end] = self.kernel(X2)/wfn_var
                    K[:i_end, i_end:] = self.kernel(X1, X2=X2)/wfn_var
                    K[i_end:, :i_end] = K[:i_end, i_end:].T
                    Q = np.linalg.inv(K)
                    Qij = Q[:i_start, i_start:]
                    maxk = np.max(np.abs(Qij))
                else:
                    Kij = self.kernel(X1, X2=X2)/wfn_var
                    maxk = np.max(np.abs(Kij))

                if maxk > threshold:
                    neighbors.append((i,j))
                    neighbor_count[i] += 1
                    neighbor_count[j] += 1

                # use a distance-based threshold instead of
                # kernel-based to eliminate complications of
                # nonstationary kernels.
                #
                #Dij = D[i_start:i_end, j_start:j_end]
                #mind = np.min(Dij)
            if i % 10 == 0:
                print "%d: neighbors count %d" % (i, neighbor_count[i])
        self.neighbor_count = neighbor_count
        self.neighbors = neighbors
        print "total pairs %d" % len(self.neighbors)


    def update_covs(self, covs):
        if not self.nonstationary:
            nv, sv = covs[0, :2]
            lscales = covs[0, 2:]
            self.cov = GPCov(wfn_params=[sv,], dfn_params=lscales, dfn_str=self.cov.dfn_str, wfn_str=self.cov.wfn_str)
            self.noise_var = nv

            dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
            self.predict_tree = VectorTree(dummy_X, 1, self.cov.dfn_str, self.cov.dfn_params, self.cov.wfn_str, self.cov.wfn_params)
        else:
            block_covs = []
            block_trees = []
            dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
            for block, covparams in enumerate(covs):
                nv, sv = covparams[:2]
                lscales = covparams[2:]
                cov = GPCov(wfn_params=[sv,], dfn_params=lscales, dfn_str=self.dfn_str, wfn_str="se")
                block_covs.append((nv, cov))
                pt = VectorTree(dummy_X, 1, cov.dfn_str, cov.dfn_params, cov.wfn_str, cov.wfn_params)
                block_trees.append(pt)
            self.block_trees = block_trees
            self.block_covs = block_covs

    def update_X(self, new_X, recompute_neighbors=False):
        self.X = new_X
        if recompute_neighbors:
            self.compute_neighbors(threshold=self.neighbor_threshold)

    def llgrad_blocked(self, parallel=False, **kwargs):
        """
        Compute likelihood under a model with blocked local GPs (no pairwise corrections)
        """
        if parallel:
            pool = Pool(processes=4)
            unary_args = [(kwargs, self, i) for i in range(self.n_blocks)]
            unaries = pool.map(llgrad_unary_shim, unary_args)
            pool.close()
            pool.join()
        else:
            unaries = [self.llgrad_unary(i, **kwargs) for i in range(self.n_blocks)]

        unary_lls, unary_gradX, unary_gradCov = zip(*unaries)

        ll = np.sum(unary_lls)

        if "grad_X" in kwargs and kwargs['grad_X']:
            grads = np.zeros(self.X.shape)
        else:
            grads = np.zeros((0, 0))

        for i in range(self.n_blocks):
            i_start, i_end = self.block_boundaries[i]
            grads[i_start:i_end, :] += unary_grads[i]

        return ll, grads

    def llgrad(self, parallel=False, local=True, **kwargs):
        # overall likelihood is the pairwise potentials for all (unordered) pairs,
        # where each block is involved in (n-1) pairs. So we subtract each unary potential n-1 times.
        # Then finally each unary potential gets added in once.

        if local:
            neighbors = self.neighbors
            neighbor_count = self.neighbor_count
        else:
            neighbors = [(i,j) for i in range(self.n_blocks) for j in range(i)]
            neighbor_count = dict([(i, self.n_blocks-1) for i in range(self.n_blocks)])

        if parallel:
            pool = Pool(processes=4)
            unary_args = [(kwargs, self, i) for i in range(self.n_blocks)]
            unaries = pool.map(llgrad_unary_shim, unary_args)

            pair_args = [(kwargs, self, i, j) for (i,j) in neighbors]
            pairs = pool.map(llgrad_joint_shim, pair_args)
            pool.close()
            pool.join()
        else:
            t0 = time.time()
            unaries = [self.llgrad_unary(i, **kwargs) for i in range(self.n_blocks)]
            t1 = time.time()
            #print self.n_blocks, "unaries in", t1-t0, "seconds"
            pairs = [self.llgrad_joint(i, j, **kwargs) for (i,j) in neighbors]
            t2 = time.time()
            #print len(neighbors), "pairs in", t2-t1, "seconds"

        t0 = time.time()

        unary_lls, unary_gradX, unary_gradCov = zip(*unaries)
        if len(pairs) > 0:
            pair_lls, pair_gradX, pair_gradCov = zip(*pairs)
        else:
            pair_lls = []
            pair_gradX = []
            pair_gradCov = []

        ll = np.sum(pair_lls)
        ll -= np.sum([(neighbor_count[i]-1)*ull for (i, ull) in enumerate(unary_lls) ])



        if "grad_X" in kwargs and kwargs['grad_X']:
            gradX = np.zeros(self.X.shape)
            pair_idx = 0
            for i in range(self.n_blocks):
                i_start, i_end = self.block_boundaries[i]
                gradX[i_start:i_end, :] -= (neighbor_count[i]-1)*unary_gradX[i]

            for pair_idx, (i,j) in enumerate(neighbors):
                i_start, i_end = self.block_boundaries[i]
                j_start, j_end = self.block_boundaries[j]
                ni = i_end-i_start
                gradX[i_start:i_end] += pair_gradX[pair_idx][:ni]
                gradX[j_start:j_end] += pair_gradX[pair_idx][ni:]
        else:
            gradX = np.zeros((0, 0))

        if "grad_cov" in kwargs and kwargs['grad_cov']:
            ncov = 2 + self.X.shape[1]
            if self.nonstationary:
                gradCov = [(1-neighbor_count[i])*unary_gradCov[i] for i in range(self.n_blocks)]
                for pair_idx, (i,j) in enumerate(neighbors):
                    pgcov = pair_gradCov[pair_idx]
                    gradCov[i] += pgcov[:ncov]
                    gradCov[j] += pgcov[ncov:]
            else:
                gradCov = np.sum(pair_gradCov, axis=0)
                gradCov -= np.sum([(neighbor_count[i]-1)*unary_gradCov[i] for i in range(self.n_blocks)], axis=0)
                gradCov = gradCov.reshape((1, -1))

        else:
            gradCov = np.zeros((0, 0))

        t1 = time.time()


        return ll, gradX, gradCov


    def llgrad_unary(self, i, **kwargs):
        i_start, i_end = self.block_boundaries[i]
        X = self.X[i_start:i_end]


        if self.nonstationary:
            kwargs['block_i'] = i

        if self.kernelized:
            YY = self.YY[i_start:i_end, i_start:i_end]
            return self.gaussian_llgrad_kernel(X, YY, dy=self.dy, **kwargs)
        else:
            Y = self.Y[i_start:i_end, :]
            return self.gaussian_llgrad(X, Y, **kwargs)

    def llgrad_joint(self, i, j, **kwargs):
        i_start, i_end = self.block_boundaries[i]
        j_start, j_end = self.block_boundaries[j]
        Xi = self.X[i_start:i_end]
        Xj = self.X[j_start:j_end]

        ni = Xi.shape[0]
        nj = Xj.shape[0]
        X = np.vstack([Xi, Xj])

        if self.nonstationary:
            kwargs['block_i'] = i
            kwargs['block_j'] = j


        if self.kernelized:
            YY = np.empty((ni+nj, ni+nj))
            YY[:ni, :ni] = self.YY[i_start:i_end, i_start:i_end]
            YY[ni:, ni:] = self.YY[j_start:j_end, j_start:j_end]
            YY[:ni, ni:] = self.YY[i_start:i_end, j_start:j_end]
            YY[ni:, :ni]  = YY[:ni, ni:].T
            return self.gaussian_llgrad_kernel(X, YY, dy=self.dy, **kwargs)
        else:
            Yi = self.Y[i_start:i_end, :]
            Yj = self.Y[j_start:j_end, :]
            Y = np.vstack([Yi, Yj])
            return self.gaussian_llgrad(X, Y, **kwargs)


    def kernel(self, X, X2=None, block=None):
        if block is None:
            ptree = self.predict_tree
            nv = self.noise_var
        else:
            ptree = self.block_trees[block]
            nv = self.block_covs[block][0]

        if X2 is None:
            n = X.shape[0]
            K = ptree.kernel_matrix(X, X, False)
            K += np.eye(n) * nv
        else:
            K = ptree.kernel_matrix(X, X2, False)
        return K

    def dKdx(self, X, p, i, return_vec=False, dKv=None, block=None):
        # derivative of kernel(X1, X2) wrt i'th coordinate of p'th point in X1.
        if block is None:
            ptree = self.predict_tree
        else:
            ptree = self.block_trees[block]

        if return_vec:
            if dKv is None:
                dKv = np.zeros((X.shape[0],), dtype=np.float)
            ptree.kernel_deriv_wrt_xi_row(X, p, i, dKv)
            dKv[p] = 0
            return dKv
        else:
            dK = ptree.kernel_deriv_wrt_xi(X, X, p, i)
            dK[p,p] = 0
            dK = dK + dK.T
            return dK

    def dKdi(self, X1, i, block=None):
        if block is None:
            ptree = self.predict_tree
            cov = self.cov
        else:
            ptree = self.block_trees[block]
            cov = self.block_covs[block][1]

        if (i == 0):
            dKdi = np.eye(X1.shape[0])
        elif (i == 1):
            if (len(cov.wfn_params) != 1):
                raise ValueError('gradient computation currently assumes just a single scaling parameter for weight function, but currently wfn_params=%s' % cov.wfn_params)
            dKdi = self.kernel(X1, X1, block=block) / cov.wfn_params[0]
        else:
            dc = ptree.kernel_matrix(X1, X1, True)
            dKdi = ptree.kernel_deriv_wrt_i(X1, X1, i-2, 1, dc)
        return dKdi

    def gaussian_llgrad(self, X, Y, grad_X = False, grad_cov=False, block_i=None, block_j=None, block_split_n=None):

        t0 = time.time()
        n, dx = X.shape
        dy = Y.shape[1]


        gradX = np.array(())
        gradC = np.array(())

        # if we're nonstationary by adding precision matrices (symmetrizing BCM predictions),
        # just average the two results
        if block_j is not None and self.nonstationary and self.nonstationary_prec:
            ll1, gx1, gc1 = self.gaussian_llgrad(X, Y, grad_X=grad_X, grad_cov=grad_cov, block_i=block_i)
            ll2, gx2, gc2 = self.gaussian_llgrad(X, Y, grad_X=grad_X, grad_cov=grad_cov, block_i=block_j)
            return (ll1+ll2)/2, (gx1+gx2)/2, np.concatenate([gc1, gc2])/2

        if self.nonstationary and block_j is not None:
            # if we're here, it's because we're averaging covs
            X1 = X[:block_split_n]
            X2 = X[block_split_n:]
            K1 = self.kernel(X1, block=block_i)
            K2 = self.kernel(X2, block=block_j)

            K12 = self.average_kernel(X1, X2, block_i=block_i, block_j=block_j)
            K = np.empty((n, n))
            K[:block_split_n, :block_split_n] = K1
            K[block_split_n:, block_split_n:] = K2
            K[:block_split_n, block_split_n:] = K12
            K[block_split_n:, :block_split_n] = K12.T


        else:
            K = self.kernel(X, block=block_i)

        prec = np.linalg.inv(K)
        Alpha = np.dot(prec, Y)
        numerical_error = check_inv(prec, K)
        if numerical_error > 1e-4:
            print "numerical failure in gaussian_llgrad, ks %.4f %.4f %.4f %.4f" % (k1, k2, k3, k4)
            ll = -1e10

            if grad_X:
                gradX = np.zeros((n, dx))
            if grad_cov:
                ncov = 2 + len(self.cov.dfn_params)
                gradC = -np.ones((ncov,))

            return ll, gradX, gradC

        ll = -.5 * np.sum(Y*Alpha)
        ll += -.5 * dy * np.linalg.slogdet(K)[1]
        ll += -.5 * dy * n * np.log(2*np.pi)

        if grad_X:
            gradX = np.zeros((n, dx))
            dcv = np.zeros((X.shape[0]), dtype=np.float)
            dcv2 = np.zeros((X.shape[0]), dtype=np.float)
            dK_alpha = np.zeros((X.shape[0]), dtype=np.float)
            for p in range(n):
                for i in range(dx):
                    dll = 0
                    if self.nonstationary and block_j is not None:
                        self.dKdx(X, p, i, return_vec=True, dKv=dcv, block=block_i)
                        self.dKdx(X, p, i, return_vec=True, dKv=dcv2, block=block_j)
                        dcv += dcv2
                        dcv /= 2
                    else:
                        self.dKdx(X, p, i, return_vec=True, dKv=dcv, block=block_i)
                    #t1 = -np.outer(prec[p,:], dcv)
                    #t1[:, p] = -np.dot(prec, dcv)
                    #dll_dcov = .5*ny*np.trace(t1)
                    dll = -dy * np.dot(prec[p,:], dcv)

                    #dK_Alpha = np.outer(dcv, Alpha[p, :])
                    #dK_Alpha = dcv * Alpha[p, :]
                    #k0 = np.sum(Alpha * dK_Alpha)
                    #rowP = np.dot(dcv, Alpha)
                    #dK_Alpha[p, :] = np.dot(dcv, Alpha)
                    #dll_dcov = .5 * np.sum(Alpha * dK_Alpha)
                    new_rowp= np.dot(dcv.T, Alpha)
                    k1 = np.dot(new_rowp, Alpha[p, :])
                    old_rowp = dcv[p] * Alpha[p, :]
                    k2 = k1 + np.dot(Alpha[p, :], new_rowp-old_rowp)
                    dll_dcov = .5* k2

                    dll += dll_dcov

                    """
                    for j in range(dy):
                        alpha = Alpha[:,j]
                        dK_alpha = dcv * alpha[p]
                        dK_alpha[p] = np.dot(dcv, alpha)
                        dll_dcov = .5*np.dot(alpha, dK_alpha)
                        dll += dll_dcov
                    """
                    gradX[p,i] = dll
            t1 = time.time()


        if grad_cov:
            ncov = 2 + len(self.cov.dfn_params)
            gradC = np.zeros((ncov,))
            for i in range(ncov):
                if self.nonstationary and block_j is not None:
                    dKdi = self.dKdi(X, i % ncov_base, block=block_i if i < ncov_base else block_j)/2
                else:
                    dKdi = self.dKdi(X, i, block=block_i)
                dlldi = .5 * np.sum(np.multiply(Alpha,np.dot(dKdi, Alpha)))
                dlldi -= .5 * dy * np.sum(np.sum(np.multiply(prec, dKdi)))
                gradC[i] = dlldi

        #print "llgrad %d pts %.4s" % (n, t1-t0)
        return ll, gradX, gradC

    def train_predictor(self, test_cov=None, Y=None):

        if Y is None:
            Y = self.Y

        if test_cov is None:
            test_cov = self.cov
            test_ptree = self.predict_tree
        else:
            dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
            test_ptree = VectorTree(dummy_X, 1, test_cov.dfn_str, test_cov.dfn_params,
                                    test_cov.wfn_str, test_cov.wfn_params)

        block_Kinvs = []
        block_Alphas = []
        for i in range(self.n_blocks):
            i_start, i_end = self.block_boundaries[i]
            X = self.X[i_start:i_end]
            blockY = Y[i_start:i_end]
            K = self.kernel(X, block = i if self.nonstationary else None)
            Kinv = np.linalg.inv(K)
            Alpha = np.dot(Kinv, blockY)
            block_Kinvs.append(Kinv)
            block_Alphas.append(Alpha)

        def predict(Xstar, test_noise_var=0.0, local=False):

            prior_cov = test_ptree.kernel_matrix(Xstar, Xstar, False)
            prior_cov += np.eye(prior_cov.shape[0])*test_noise_var
            prior_prec = np.linalg.inv(prior_cov)

            prior_mean = np.zeros((Xstar.shape[0], Y.shape[1]))

            if local:
                nearest = np.argmin([np.min(pair_distances(Xstar, self.X[i_start:i_end])) for (i_start, i_end) in self.block_boundaries])
                neighbors = [nearest,]
            else:
                neighbors = range(self.n_blocks)

            for i in neighbors:

                i_start, i_end = self.block_boundaries[i]
                X = self.X[i_start:i_end]

                ptree = self.block_trees[i] if self.nonstationary else self.predict_tree
                nv = self.block_covs[i][0] if self.nonstationary else self.noise_var

                Kinv = block_Kinvs[i]
                Kstar = ptree.kernel_matrix(Xstar, X, False)
                Kss = ptree.kernel_matrix(Xstar, Xstar, False)
                if test_noise_var > 0:
                    Kss += np.eye(Kss.shape[0]) * nv

                mean = np.dot(Kstar, block_Alphas[i])
                cov = Kss - np.dot(Kstar, np.dot(Kinv, Kstar.T))
                prec = np.linalg.inv(cov)
                pp = np.linalg.inv(Kss)
                message_prec = prec - pp
                weighted_mean = np.dot(prec, mean)
                prior_mean += weighted_mean
                prior_prec += message_prec

            final_cov = np.linalg.inv(prior_prec)
            final_mean = np.dot(final_cov, prior_mean)

            return final_mean, final_cov

        return predict

    def gaussian_llgrad_kernel(self, X, YY, dy=None, grad_X=False, grad_cov=False):
        if self.nonstationary:
            raise Exception("nonstationary not supported for kernel observations. (or at all, really).")
        n, dx = X.shape
        if dy is None:
            dy = self.dy
        gradX = np.array(())
        gradC = np.array(())

        K = self.kernel(X)
        Kinv = np.linalg.inv(K)
        prec = Kinv
        numerical_error = check_inv(prec, K)
        if numerical_error > 1e-4:
            print "numerical failure in gaussian_llgrad, ks %.4f %.4f %.4f %.4f" % (k1, k2, k3, k4)
            ll = -1e10

            if grad_X:
                gradX = np.zeros((n, dx))
            if grad_cov:
                ncov = 2 + self.X.shape[1]
                gradC = -np.ones((ncov,))

            return ll, gradX, gradC

        KYYK = np.dot(np.dot(Kinv, YY), Kinv)

        ll =  -.5 * np.sum(Kinv * YY)
        ll += -.5 * dy * np.linalg.slogdet(K)[1]
        ll += -.5 * dy * n * np.log(2*np.pi)

        if grad_X:
            gradX = np.zeros((n, dx))
            for p in range(n):
                for i in range(dx):
                    #dcv_full = self.dKdx(X, p, i)
                    #dll = -.5*np.sum(KYYK * dcv_full)
                    dcv = self.dKdx(X, p, i, return_vec=True)
                    dll = np.dot(KYYK[p,:], dcv)

                    dll += -dy * np.dot(prec[p,:], dcv)

                    gradX[p,i] = dll

                #t1 = -np.outer(prec[p,:], dcov_v)
                #t1[:, p] = -np.dot(prec, dcov_v)

        if grad_cov:
            ncov = 2 + len(self.cov.dfn_params)
            gradC = np.zeros((ncov,))
            for i in range(ncov):
                dKdi = self.dKdi(X, i)
                dll = .5*np.sum(KYYK * dKdi)
                dll += -.5 * dy * np.sum(prec * dKdi)
                gradC[i] = dll

                if np.isnan(dll):
                    import pdb; pdb.set_trace()

        return ll, gradX, gradC

    def __getstate__(self):
        d = self.__dict__.copy()
        del d['predict_tree']
        return d

    def __setstate__(self, d):
        self.__dict__ = d
        dummy_X = np.array([[0.0,] * self.X.shape[1],], dtype=float)
        self.predict_tree = VectorTree(dummy_X, 1, self.cov.dfn_str, self.cov.dfn_params, self.cov.wfn_str, self.cov.wfn_params)

from multiprocessing import Pool
import time

def llgrad_unary_shim(arg):
    return MultiSharedBCM.llgrad_unary(*arg[1:], **arg[0])

def llgrad_joint_shim(arg):
    return MultiSharedBCM.llgrad_joint(*arg[1:], **arg[0])
