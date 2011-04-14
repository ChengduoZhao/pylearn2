"""
Implementations of Restricted Boltzmann Machines and associated sampling
strategies.
"""
# Standard library imports
from itertools import izip

# Third-party imports
import numpy
import theano
from theano import tensor
from theano.tensor import nnet

# Local imports
from .base import Block, StackedBlocks
from .utils import sharedX, safe_update

theano.config.warn.sum_div_dimshuffle_bug = False
floatX = theano.config.floatX

if 0:
    print 'WARNING: using SLOW rng'
    RandomStreams = tensor.shared_randomstreams.RandomStreams
else:
    import theano.sandbox.rng_mrg
    RandomStreams = theano.sandbox.rng_mrg.MRG_RandomStreams


def training_updates(visible_batch, model, sampler, optimizer):
    """
    Combine together updates from various sources for RBM training.

    Parameters
    ----------
    visible_batch : tensor_like
        Theano symbolic representing a minibatch on the visible units,
        with the first dimension indexing training examples and the second
        indexing data dimensions.
    rbm : object
        An instance of `RBM` or a derived class, or one implementing
        the RBM interface.
    sampler : object
        An instance of `Sampler` or a derived class, or one implementing
        the sampler interface.
    optimizer : object
        An instance of `Optimizer` or a derived class, or one implementing
        the optimizer interface (typically an `SGDOptimizer`).
    """
    pos_v = visible_batch
    neg_v = sampler.particles
    grads = model.ml_gradients(pos_v, neg_v)
    ups = optimizer.updates(gradients=grads)

    # Add the sampler's updates (negative phase particles, etc.).
    safe_update(ups, sampler.updates())
    return ups


class Sampler(object):
    """
    A sampler is responsible for implementing a sampling strategy on top of
    an RBM, which may include retaining state e.g. the negative particles for
    Persistent Contrastive Divergence.
    """
    def __init__(self, rbm, particles, rng):
        """
        Construct a Sampler.

        Parameters
        ----------
        rbm : object
            An instance of `RBM` or a derived class, or one implementing
            the `gibbs_step_for_v` interface.
        particles : ndarray
            An initial state for the set of persistent Narkov chain particles
            that will be updated at every step of learning.
        rng : RandomState object
            NumPy random number generator object used to initialize a
            RandomStreams object used in training.
        """
        self.__dict__.update(rbm=rbm)
        if not hasattr(rng, 'randn'):
            rng = numpy.random.RandomState(rng)
        seed = int(rng.randint(2 ** 30))
        self.s_rng = RandomStreams(seed)
        self.particles = sharedX(particles, name='particles')

    def updates(self):
        """
        Get the dictionary of updates for the sampler's persistent state
        at each step.

        Returns
        -------
        updates : dict
            Dictionary with shared variable instances as keys and symbolic
            expressions indicating how they should be updated as values.

        Notes
        -----
        In the `Sampler` base class, this is simply a stub.
        """
        raise NotImplementedError()


class PersistentCDSampler(Sampler):
    """
    Implements a persistent Markov chain for use with Persistent Contrastive
    Divergence, a.k.a. stochastic maximum likelhiood, as described in [1].

    .. [1] T. Tieleman. "Training Restricted Boltzmann Machines using
       approximations to the likelihood gradient". Proceedings of the 25th
       International Conference on Machine Learning, Helsinki, Finland,
       2008. http://www.cs.toronto.edu/~tijmen/pcd/pcd.pdf
    """
    def __init__(self, rbm, particles, rng, steps=1, particles_clip=None):
        """
        Construct a PersistentCDSampler.

        Parameters
        ----------
        rbm : object
            An instance of `RBM` or a derived class, or one implementing
            the `gibbs_step_for_v` interface.
        particles : ndarray
            An initial state for the set of persistent Markov chain particles
            that will be updated at every step of learning.
        rng : RandomState object
            NumPy random number generator object used to initialize a
            RandomStreams object used in training.
        steps : int, optional
            Number of Gibbs steps to run the Markov chain for at each
            iteration.
        particles_clip: None or (min, max) pair
            The values of the returned particles will be clipped between
            min and max.
        """
        super(PersistentCDSampler, self).__init__(rbm, particles, rng)
        self.steps = steps
        self.particles_clip = particles_clip

    def updates(self, particles_clip=None):
        """
        Get the dictionary of updates for the sampler's persistent state
        at each step..

        Returns
        -------
        updates : dict
            Dictionary with shared variable instances as keys and symbolic
            expressions indicating how they should be updated as values.
        """
        steps = self.steps
        particles = self.particles
        # TODO: do this with scan?
        for i in xrange(steps):
            particles, _locals = self.rbm.gibbs_step_for_v(
                particles,
                self.s_rng
            )
            if self.particles_clip is not None:
                p_min, p_max = self.particles_clip
                particles = tensor.clip(particles, p_min, p_max)
        if not hasattr(self.rbm, 'h_sample'):
            self.rbm.h_sample = sharedX(numpy.zeros((0, 0)), 'h_sample')
        return {
            self.particles: particles,
            # TODO: self.rbm.h_sample is never used, why is that here?
            # Moreover, it does not make sense for things like ssRBM.
            self.rbm.h_sample: _locals['h_mean']
        }


class RBM(Block):
    """
    A base interface for RBMs, implementing the binary-binary case.

    TODO: model shouldn't depend on batch_size.
    """
    def __init__(self, nvis, nhid, batch_size=10, irange=0.5, rng=9001):
        """
        Construct an RBM object.

        Parameters
        ----------
        nvis : int
            Number of visible units in the model.
        nhid : int
            Number of hidden units in the model.
        batch_size : int, optional
            Size of minibatches to be used (TODO: this parameter should be
            deprecated)
        irange : float, optional
            The size of the initial interval around 0 for weights.
        rng : RandomState object or seed
            NumPy RandomState object to use when initializing parameters
            of the model, or (integer) seed to use to create one.
        """
        if rng is None:
            # TODO: global rng configuration stuff.
            rng = numpy.random.RandomState(1001)
        self.visbias = sharedX(
            numpy.zeros(nvis),
            name='vb',
            borrow=True
        )
        self.hidbias = sharedX(
            numpy.zeros(nhid),
            name='hb',
            borrow=True
        )
        self.weights = sharedX(
            (0.5 - rng.rand(nvis, nhid)) * irange,
            name='W',
            borrow=True
        )
        self.__dict__.update(batch_size=batch_size, nhid=nhid, nvis=nvis)
        self._params = [self.visbias, self.hidbias, self.weights]

    def ml_gradients(self, pos_v, neg_v):
        """
        Get the contrastive gradients given positive and negative phase
        visible units.

        Parameters
        ----------
        pos_v : tensor_like
            Theano symbolic representing a minibatch on the visible units,
            with the first dimension indexing training examples and the second
            indexing data dimensions (usually actual training data).
        neg_v : tensor_like
            Theano symbolic representing a minibatch on the visible units,
            with the first dimension indexing training examples and the second
            indexing data dimensions (usually reconstructions of the data or
            sampler particles from a persistent Markov chain).

        Returns
        -------
        grads : list
            List of Theano symbolic variables representing gradients with
            respect to model parameters, in the same order as returned by
            `params()`.

        Notes
        -----
        `pos_v` and `neg_v` need not have the same first dimension, i.e.
        minibatch size.
        """

        # taking the mean over each term independently allows for different
        # mini-batch sizes in the positive and negative phase.
        ml_cost = (self.free_energy_given_v(pos_v).mean() -
                   self.free_energy_given_v(neg_v).mean())

        grads = tensor.grad(ml_cost, self.params(),
                            consider_constant=[pos_v, neg_v])

        return grads

    def gibbs_step_for_v(self, v, rng):
        """
        Do a round of block Gibbs sampling given visible configuration

        Parameters
        ----------
        v  : tensor_like
            Theano symbolic representing the hidden unit states for a batch of
            training examples (or negative phase particles), with the first
            dimension indexing training examples and the second indexing data
            dimensions.
        rng : RandomStreams object
            Random number generator to use for sampling the hidden and visible
            units.

        Returns
        -------
        v_sample : tensor_like
            Theano symbolic representing the new visible unit state after one
            round of Gibbs sampling.
        locals : dict
            Contains the following auxillary state as keys (all symbolics
            except shape tuples):
             * `h_mean`: the returned value from `mean_h_given_v`
             * `h_mean_shape`: shape tuple indicating the size of `h_mean` and
               `h_sample`
             * `h_sample`: the stochastically sampled hidden units
             * `v_mean_shape`: shape tuple indicating the shape of `v_mean` and
               `v_sample`
             * `v_mean`: the returned value from `mean_v_given_h`
             * `v_sample`: the stochastically sampled visible units
        """
        h_mean = self.mean_h_given_v(v)
        # For binary hidden units
        # TODO: factor further to extend to other kinds of hidden units
        #       (e.g. spike-and-slab)
        h_mean_shape = self.batch_size, self.nhid
        h_sample = tensor.cast(rng.uniform(size=h_mean_shape) < h_mean, floatX)
        v_mean_shape = self.batch_size, self.nvis
        # v_mean is always based on h_sample, not h_mean, because we don't
        # want h transmitting more than one bit of information per unit.
        v_mean = self.mean_v_given_h(h_sample)
        v_sample = self.sample_visibles([v_mean], v_mean_shape, rng)
        return v_sample, locals()

    def sample_visibles(self, params, shape, rng):
        """
        Stochastically sample the visible units given hidden unit
        configurations for a set of training examples.

        Parameters
        ----------
        params : list
            List of the necessary parameters to sample :math:`p(v|h)`. In the
            case of a binary-binary RBM this is a single-element list
            containing the symbolic representing :math:`p(v|h)`, as returned
            by `mean_v_given_h`.

        Returns
        -------
        vprime : tensor_like
            Theano symbolic representing stochastic samples from :math:`p(v|h)`
        """
        v_mean = params[0]
        return tensor.cast(rng.uniform(size=shape) < v_mean, floatX)

    def input_to_h_from_v(self, v):
        """
        Compute the affine function (linear map plus bias) that serves as
        input to the hidden layer in an RBM.

        Parameters
        ----------
        v  : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the one or several
            minibatches on the visible units, with the first dimension indexing
            training examples and the second indexing data dimensions.

        Returns
        -------
        a : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the input to each
            hidden unit for each training example.
        """
        if isinstance(v, tensor.Variable):
            return self.hidbias + tensor.dot(v, self.weights)
        else:
            return [self.input_to_h_from_v(vis) for vis in v]

    def mean_h_given_v(self, v):
        """
        Compute the mean activation of the visibles given hidden unit
        configurations for a set of training examples.

        Parameters
        ----------
        v  : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the hidden unit
            states for a batch (or several) of training examples, with the
            first dimension indexing training examples and the second indexing
            data dimensions.

        Returns
        -------
        h : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the mean
            (deterministic) hidden unit activations given the visible units.
        """
        if isinstance(v, tensor.Variable):
            return nnet.sigmoid(self.input_to_h_from_v(v))
        else:
            return [self.mean_h_given_v(vis) for vis in v]

    def mean_v_given_h(self, h):
        """
        Compute the mean activation of the visibles given hidden unit
        configurations for a set of training examples.

        Parameters
        ----------
        h  : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the hidden unit
            states for a batch (or several) of training examples, with the
            first dimension indexing training examples and the second indexing
            hidden units.

        Returns
        -------
        vprime : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the mean
            (deterministic) reconstruction of the visible units given the
            hidden units.
        """
        if isinstance(h, tensor.Variable):
            return nnet.sigmoid(self.visbias + tensor.dot(h, self.weights.T))
        else:
            return [self.mean_v_given_h(hid) for hid in h]

    def free_energy_given_v(self, v):
        """
        Calculate the free energy of a visible unit configuration by
        marginalizing over the hidden units.

        Parameters
        ----------
        v : tensor_like
            Theano symbolic representing the hidden unit states for a batch of
            training examples, with the first dimension indexing training
            examples and the second indexing data dimensions.

        Returns
        -------
        f : tensor_like
            0-dimensional tensor (i.e. effectively a scalar) representing the
            free energy of the visible unit configuration.
        """
        sigmoid_arg = self.input_to_h_from_v(v)
        return -(tensor.dot(v, self.visbias) +
                 nnet.softplus(sigmoid_arg).sum(axis=1))

    def __call__(self, v):
        """
        Forward propagate (symbolic) input through this module, obtaining
        a representation to pass on to layers above.

        This just aliases the `mean_h_given_v()` function for syntactic
        sugar/convenience.
        """
        return self.mean_h_given_v(v)

    def reconstruction_error(self, v, rng):
        """
        Compute the mean-squared error across a minibatch after a Gibbs
        step starting from the training data.

        Parameters
        ----------
        v : tensor_like
            Theano symbolic representing the hidden unit states for a batch of
            training examples, with the first dimension indexing training
            examples and the second indexing data dimensions.
        rng : RandomStreams object
            Random number generator to use for sampling the hidden and visible
            units.

        Returns
        -------
        mse : tensor_like
            0-dimensional tensor (essentially a scalar) indicating the mean
            reconstruction error across the minibatch.

        Notes
        -----
        The reconstruction used to assess error is deterministic, i.e.
        no sampling is done, to reduce noise in the estimate.
        """
        sample, _locals = self.gibbs_step_for_v(v, rng)
        return ((_locals['v_mean'] - v) ** 2).sum(axis=1).mean()


class GaussianBinaryRBM(RBM):
    """
    An RBM with Gaussian visible units and binary hidden units.

    TODO: model shouldn't depend on batch_size.
    """
    def __init__(self, nvis, nhid, batch_size, irange=0.5, rng=None,
                 mean_vis=False):
        """
        Allocate a GaussianBinaryRBM object.

        Parameters
        ----------
        nvis : int
            Number of visible units in the model.
        nhid : int
            Number of hidden units in the model.
        batch_size : int, optional
            Size of minibatches to be used (TODO: this parameter should be
            deprecated)
        irange : float, optional
            The size of the initial interval around 0 for weights.
        rng : RandomState object or seed
            NumPy RandomState object to use when initializing parameters
            of the model, or (integer) seed to use to create one.
        mean_vis : bool, optional
            Don't actually sample visibles; make sample method simply return
            mean.
        """
        super(GaussianBinaryRBM, self).__init__(nvis, nhid, batch_size,
                                                irange, rng)
        self.sigma = sharedX(
            numpy.ones(nvis),
            name='sigma',
            borrow=True
        )
        self.mean_vis = mean_vis

    def input_to_h_from_v(self, v):
        """
        Compute the affine function (linear map plus bias) that serves as
        input to the hidden layer in an RBM.

        Parameters
        ----------
        v  : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing one or several
            minibatches on the visible units, with the first dimension indexing
            training examples and the second indexing data dimensions.

        Returns
        -------
        a : tensor_like or list of tensor_likes
            Theano symbolic (or list thereof) representing the input to each
            hidden unit for each training example.

        Notes
        -----
        In the Gaussian-binary case, each data dimension is scaled by a sigma
        parameter (which defaults to 1 in this implementation, but is
        nonetheless present as a shared variable in the model parameters).
        """
        if isinstance(v, tensor.Variable):
            return self.hidbias + tensor.dot(v / self.sigma, self.weights)
        else:
            return [self.input_to_h_from_v(vis) for vis in v]

    def mean_v_given_h(self, h):
        """
        Compute the mean activation of the visibles given hidden unit
        configurations for a set of training examples.

        Parameters
        ----------
        h  : tensor_like
            Theano symbolic representing the hidden unit states for a batch of
            training examples, with the first dimension indexing training
            examples and the second indexing hidden units.

        Returns
        -------
        vprime : tensor_like
            Theano symbolic representing the mean (deterministic)
            reconstruction of the visible units given the hidden units.
        """
        return self.visbias + self.sigma * tensor.dot(h, self.weights.T)

    def free_energy_given_v(self, v):
        """
        Calculate the free energy of a visible unit configuration by
        marginalizing over the hidden units.

        Parameters
        ----------
        v : tensor_like
            Theano symbolic representing the hidden unit states for a batch of
            training examples, with the first dimension indexing training
            examples and the second indexing data dimensions.

        Returns
        -------
        f : tensor_like
            0-dimensional tensor (i.e. effectively a scalar) representing the
            free energy of the visible unit configuration.
        """
        hid_inp = self.input_to_h_from_v(v)
        squared_term = (self.visbias - v) ** 2 / self.sigma
        return squared_term.sum(axis=1) - nnet.softplus(hid_inp).sum(axis=1)

    def sample_visibles(self, params, shape, rng):
        """
        Stochastically sample the visible units given hidden unit
        configurations for a set of training examples.

        Parameters
        ----------
        params : list
            List of the necessary parameters to sample :math:`p(v|h)`. In the
            case of a Gaussian-binary RBM this is a single-element list
            containing the conditional mean.

        Returns
        -------
        vprime : tensor_like
            Theano symbolic representing stochastic samples from :math:`p(v|h)`

        Notes
        -----
        If `mean_vis` is specified as `True` in the constructor, this is
        equivalent to a call to `mean_v_given_h`.
        """
        v_mean = params[0]
        if self.mean_vis:
            return v_mean
        else:
            # zero mean, std sigma noise
            zero_mean = rng.normal(size=shape) * self.sigma
            return zero_mean + v_mean

class mu_pooled_ssRBM(RBM):
    """
    alpha    : vector of length nslab, diagonal precision term on s.
    b        : vector of length nhid, hidden unit bias.
    B        : vector of length nvis, diagonal precision on v.
               Lambda in ICML2011 paper.
    Lambda   : matrix of shape nvis x nhid, whose i-th column encodes a diagonal
               precision on v, conditioned on h_i.
               phi in ICML2011 paper.
    log_alpha: vector of length nslab, precision on s.
    mu       : vector of length nslab, mean parameter on s.
    W        : matrix of shape nvis x nslab, weights of the nslab linear filters s.
    """
    def __init__(self, nvis, nhid, n_s_per_h,
            batch_size,
            alpha0, alpha_irange,
            b0,
            B0,
            Lambda0, Lambda_irange,
            mu0,
            W_irange,
            rng=None):
        if rng is None:
            # TODO: global rng default seed
            rng = numpy.random.RandomState(1001)

        self.nhid = nhid
        self.nslab = nhid * n_s_per_h
        self.n_s_per_h = n_s_per_h
        self.nvis = nvis

        self.batch_size = batch_size

        # configure \alpha: precision parameter on s
        alpha_init = numpy.zeros(self.nslab) + alpha0
        if alpha_irange > 0:
            alpha_init += (2 * rng.rand(self.nslab) - 1) * alpha_irange
        self.log_alpha = sharedX(numpy.log(alpha_init), name='log_alpha')
        self.alpha = tensor.exp(self.log_alpha)
        self.alpha.name = 'alpha'

        self.mu = sharedX(
                numpy.zeros(self.nslab) + mu0,
                name='mu', borrow=True)
        self.b = sharedX(
                numpy.zeros(self.nhid) + b0,
                name='b', borrow=True)

        self.W = sharedX(
                (.5-rng.rand(self.nvis, self.nslab)) * 2 * W_irange,
                name='W', borrow=True)

        # THE BETA IS IGNORED DURING TRAINING - FIXED AT MARGINAL DISTRIBUTION
        self.B = sharedX(numpy.zeros(self.nvis) + B0, name='B', borrow=True)

        if Lambda_irange > 0:
            L = (rng.rand(self.nvis, self.nhid) * Lambda_irange
                    + Lambda0)
        else:
            L = numpy.zeros((self.nvis, self.nhid)) + Lambda0
        self.Lambda = sharedX(L, name='Lambda', borrow=True)

        self._params = [
                self.mu,
                self.B,
                self.Lambda,
                self.W,
                self.b,
                self.log_alpha]

    #def ml_gradients(self, pos_v, neg_v):
    #    inherited version is OK.

    def gibbs_step_for_v(self, v, rng):
        # sample h given v
        h_mean = self.mean_h_given_v(v)
        h_mean_shape = (self.batch_size, self.nhid)
        h_sample = tensor.cast(rng.uniform(size=h_mean_shape) < h_mean, floatX)

        # sample s given (v,h)
        s_mu, s_var = self.mean_var_s_given_v_h1(v)
        s_mu_shape = (self.batch_size, self.nslab)
        s_sample = s_mu + rng.normal(size=s_mu_shape) * tensor.sqrt(s_var)
        #s_sample = (s_sample.reshape() * h_sample.dimshuffle(0,1,'x')).flatten(2)

        # sample v given (s,h)
        v_mean, v_var = self.mean_var_v_given_h_s(h_sample, s_sample)
        v_mean_shape = (self.batch_size, self.nvis)
        v_sample = rng.normal(size=v_mean_shape) * tensor.sqrt(v_var) + v_mean

        return v_sample, locals()

    ## TODO?
    def sample_visibles(self, params, shape, rng):
        raise NotImplementedError('mu_pooled_ssRBM.sample_visibles')

    def input_to_h_from_v(self, v):
        D = self.Lambda
        alpha = self.alpha
        def sum_s(x):
            return x.reshape((
                -1,
                self.nhid,
                self.n_s_per_h)).sum(axis=2)

        return tensor.add(
                self.b,
                -0.5 * tensor.dot(v * v, D),
                sum_s(self.mu * tensor.dot(v, self.W)),
                sum_s(0.5 * tensor.sqr(tensor.dot(v, self.W))/alpha))

    #def mean_h_given_v(self, v):
    #    inherited version is OK:
    #    return nnet.sigmoid(self.input_to_h_from_v(v))

    def mean_var_v_given_h_s(self, h, s):
        v_var = 1 / (self.B + tensor.dot(h, self.Lambda.T))
        s3 = s.reshape((
                -1,
                self.nhid,
                self.n_s_per_h))
        hs = h.dimshuffle(0, 1, 'x') * s3
        v_mu = tensor.dot(hs.flatten(2), self.W.T) * v_var
        return v_mu, v_var

    def mean_var_s_given_v_h1(self, v):
        alpha = self.alpha
        return (self.mu + tensor.dot(v, self.W) / alpha,
                1.0 / alpha)

    ## TODO?
    def mean_v_given_h(self, h):
        raise NotImplementedError('mu_pooled_ssRBM.mean_v_given_h')


    def free_energy_given_v(self, v):
        sigmoid_arg = self.input_to_h_from_v(v)
        return tensor.add(
                0.5 * (self.B * (v**2)).sum(axis=1),
                -tensor.nnet.softplus(sigmoid_arg).sum(axis=1))

    #def __call__(self, v):
    #    inherited version is OK

    #def reconstruction_error:
    #    inherited version should be OK

    #def params(self):
    #    inherited version is OK.


def build_stacked_RBM(nvis, nhids, batch_size, vis_type='binary',
        input_mean_vis=None, irange=1e-3, rng=None):
    """
    Allocate a StackedBlocks containing RBMs.

    The visible units of the input RBM can be either binary or gaussian,
    the other ones are all binary.
    """
    #TODO: not sure this is the right way of dealing with mean_vis.
    layers = []
    assert vis_type in ['binary', 'gaussian']
    if vis_type == 'binary':
        assert input_mean_vis is None
    elif vis_type == 'gaussian':
        assert input_mean_vis in True, False

    # The number of visible units in each layer is the initial input
    # size and the first k-1 hidden unit sizes.
    nviss = [nvis] + nhids[:-1]
    seq = izip(
            xrange(len(nhids)),
            nhids,
            nviss,
            )
    for k, nhid, nvis in seq:
        if k == 0 and vis_type == 'gaussian':
            rbm = GaussianBinaryRBM(nvis=nvis, nhid=nhid,
                    batch_size=batch_size,
                    irange=irange,
                    rng=rng,
                    mean_vis=input_mean_vis)
        else:
            rbm = RBM(nvis - nvis, nhid=nhid,
                    batch_size=batch_size,
                    irange=irange,
                    rng=rng)
        layers.append(rbm)

    # Create the stack
    return StackedBlocks(layers)


##################################################
def get(str):
    """ Evaluate str into an rbm object, if it exists """
    obj = globals()[str]
    if issubclass(obj, RBM):
        return obj
    else:
        raise NameError(str)
