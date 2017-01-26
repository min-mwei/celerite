# -*- coding: utf-8 -*-

from __future__ import division, print_function
import math
import numpy as np
from itertools import chain

from ._celerite import Solver
from .modeling import Model, ConstantModel

__all__ = ["GP"]


class GP(Model):
    """The main interface to the celerite Gaussian Process solver

    Args:
        kernel: An instance of a subclass of :class:`terms.Kernel`.
        mean (Optional): A simple mean value for the process. This can either
            be a ``float`` or a subclass of :class:`Model`. (default: ``0.0``)
        fit_mean (optional): If ``False``, all of the parameters of ``mean``
            will be frozen. Otherwise, the parameter states are unaffected.
            (default: ``False``)
        log_white_noise (Optional): A white noise model for the process. The
            ``exp`` of this will be added to the diagonal of the matrix in
            :func:`GP.compute`. This can either be a ``float`` or a subclass
            of :class:`Model`. (default: ``-inf``)
        fit_white_noise (optional): If ``False``, all of the parameters of
            ``log_white_noise`` will be frozen. Otherwise, the parameter
            states are unaffected. (default: ``False``)

    """

    def __init__(self,
                 kernel,
                 mean=0.0, fit_mean=False,
                 log_white_noise=-float("inf"), fit_white_noise=False):
        self.kernel = kernel

        self.solver = None
        self._computed = False
        self._t = None
        self._y_var = None

        # Interpret the white noise model
        try:
            float(log_white_noise)
        except TypeError:
            self.log_white_noise = log_white_noise
        else:
            self.log_white_noise = ConstantModel(float(log_white_noise))

        # If this model is supposed to be constant, go through and freeze
        # all of the parameters
        if not fit_white_noise:
            for k in self.log_white_noise.get_parameter_names():
                self.log_white_noise.freeze_parameter(k)

        # And the mean model
        try:
            float(mean)
        except TypeError:
            self.mean = mean
        else:
            self.mean = ConstantModel(float(mean))

        if not fit_mean:
            for k in self.mean.get_parameter_names():
                self.mean.freeze_parameter(k)

    @property
    def computed(self):
        return not self.dirty

    def compute(self, t, yerr=1.123e-12, check_sorted=True):
        t = np.atleast_1d(t)
        if check_sorted and np.any(np.diff(t) < 0.0):
            raise ValueError("the input coordinates must be sorted")
        if check_sorted and len(t.shape) > 1:
            raise ValueError("dimension mismatch")
        self._t = t
        self._yerr = np.empty_like(self._t)
        self._yerr[:] = yerr
        if self.solver is None:
            self.solver = Solver()
        (alpha_real, beta_real, alpha_complex_real, alpha_complex_imag,
         beta_complex_real, beta_complex_imag) = self.kernel.coefficients
        self.solver.compute(
            alpha_real, beta_real,
            alpha_complex_real, alpha_complex_imag,
            beta_complex_real, beta_complex_imag,
            t, self._get_diag()
        )
        self.dirty = False

    def _recompute(self):
        if self.dirty:
            if self._t is None:
                raise RuntimeError("you must call 'compute' first")
            self.compute(self._t, self._yerr, check_sorted=False)

    def _process_input(self, y):
        if self._t is None:
            raise RuntimeError("you must call 'compute' first")
        if len(self._t) != len(y):
            raise ValueError("dimension mismatch")
        return np.ascontiguousarray(y, dtype=float)

    def log_prior(self):
        lp = (
            self.mean.log_prior() +
            self.log_white_noise.log_prior() +
            self.kernel.log_prior()
        )
        return lp if np.isfinite(lp) else -np.inf

    def log_likelihood(self, y, _const=math.log(2.0*math.pi)):
        y = self._process_input(y)
        resid = y - self.mean.get_value(self._t)
        self._recompute()
        if len(y.shape) > 1:
            raise ValueError("dimension mismatch")
        return -0.5 * (self.solver.dot_solve(resid) +
                       self.solver.log_determinant() +
                       len(y) * _const)

    def apply_inverse(self, y):
        self._recompute()
        return self.solver.solve(self._process_input(y))

    def dot(self, y):
        self._recompute()
        (alpha_real, beta_real, alpha_complex_real, alpha_complex_imag,
         beta_complex_real, beta_complex_imag) = self.kernel.coefficients
        return self.solver.dot(
            alpha_real, beta_real,
            alpha_complex_real, alpha_complex_imag,
            beta_complex_real, beta_complex_imag,
            self._t, self._process_input(y)
        )

    def predict(self, y, t=None, return_cov=True, return_var=False):
        y = self._process_input(y)
        if len(y.shape) > 1:
            raise ValueError("dimension mismatch")

        if t is None:
            xs = self._t
        else:
            xs = np.ascontiguousarray(t, dtype=float)
            if len(xs.shape) > 1:
                raise ValueError("dimension mismatch")

        # Make sure that the model is computed
        self._recompute()

        # Compute the predictive mean.
        resid = y - self.mean.get_value(self._t)
        alpha = self.solver.solve(resid).flatten()

        if t is None:
            alpha = y - self._get_diag() * alpha
        else:
            Kxs = self.get_matrix(xs, self._t)
            alpha = np.dot(Kxs, alpha)

        mu = self.mean.get_value(xs) + alpha
        if not (return_var or return_cov):
            return mu

        # Predictive variance.
        if t is None:
            Kxs = self.get_matrix(xs, self._t)
        KxsT = np.ascontiguousarray(Kxs.T, dtype=np.float64)
        if return_var:
            var = -np.sum(KxsT*self.apply_inverse(KxsT), axis=0)
            var += self.kernel.get_value(0.0)
            return mu, var

        # Predictive covariance
        cov = self.kernel.get_value(xs[:, None] - xs[None, :])
        cov -= np.dot(Kxs, self.apply_inverse(KxsT))
        return mu, cov

    def _get_diag(self):
        return self._yerr**2 + np.exp(self.log_white_noise
                                      .get_value(self._t))

    def get_matrix(self, x1=None, x2=None, include_diagonal=None):
        if x1 is None and x2 is None:
            if self._t is None or not self.computed:
                raise RuntimeError("you must call 'compute' first")
            K = self.kernel.get_value(self._t[:, None] - self._t[None, :])
            if include_diagonal is None or include_diagonal:
                K[np.diag_indices_from(K)] += self._get_diag()
            return K

        incl = False
        x1 = np.ascontiguousarray(x1, dtype=float)
        if x2 is None:
            x2 = x1
            incl = include_diagonal is not None and include_diagonal
        K = self.kernel.get_value(x1[:, None] - x2[None, :])
        if incl:
            K[np.diag_indices_from(K)] += np.exp(self.log_white_noise
                                                 .get_value(x1))
        return K

    def sample(self, x, tiny=1e-12, size=None):
        K = self.get_matrix(x, include_diagonal=True)
        K[np.diag_indices_from(K)] += tiny
        sample = np.random.multivariate_normal(np.zeros_like(x), K, size=size)
        return self.mean.get_value(x) + sample

    #
    # MODELING PROTOCOL
    #
    @property
    def dirty(self):
        return (
            self.mean.dirty or
            self.log_white_noise.dirty or
            self.kernel.dirty or
            not self._computed
        )

    @dirty.setter
    def dirty(self, value):
        self._computed = not value
        self.mean.dirty = value
        self.log_white_noise.dirty = value
        self.kernel.dirty = value

    @property
    def full_size(self):
        return (
            self.mean.full_size +
            self.log_white_noise.full_size +
            self.kernel.full_size
        )

    @property
    def vector_size(self):
        return (
            self.mean.vector_size +
            self.log_white_noise.vector_size +
            self.kernel.vector_size
        )

    @property
    def unfrozen_mask(self):
        return np.concatenate((
            self.mean.unfrozen_mask,
            self.log_white_noise.unfrozen_mask,
            self.kernel.unfrozen_mask,
        ))

    @property
    def parameter_vector(self):
        return np.concatenate((
            self.mean.parameter_vector,
            self.log_white_noise.parameter_vector,
            self.kernel.parameter_vector
        ))

    @property
    def parameter_bounds(self):
        return list(chain(
            self.mean.parameter_bounds,
            self.log_white_noise.parameter_bounds,
            self.kernel.parameter_bounds
        ))

    @parameter_vector.setter
    def parameter_vector(self, v):
        i = self.mean.full_size
        self.mean.parameter_vector = v[:i]
        j = i + self.log_white_noise.full_size
        self.log_white_noise.parameter_vector = v[i:j]
        self.kernel.parameter_vector = v[j:]

    @property
    def parameter_names(self):
        return tuple(chain(
            map("mean:{0}".format, self.mean.parameter_names),
            map("log_white_noise:{0}".format,
                self.log_white_noise.parameter_names),
            map("kernel:{0}".format, self.kernel.parameter_names),
        ))

    def _apply_to_parameter(self, func, name, *args):
        if name.startswith("mean:"):
            return getattr(self.mean, func)(name[5:], *args)
        if name.startswith("log_white_noise:"):
            return getattr(self.log_white_noise, func)(name[16:], *args)
        if name.startswith("kernel:"):
            return getattr(self.kernel, func)(name[7:], *args)
        raise ValueError("unrecognized parameter '{0}'".format(name))

    def freeze_parameter(self, name):
        self._apply_to_parameter("freeze_parameter", name)

    def thaw_parameter(self, name):
        self._apply_to_parameter("thaw_parameter", name)

    def freeze_all_parameters(self):
        self.mean.freeze_all_parameters()
        self.log_white_noise.freeze_all_parameters()
        self.kernel.freeze_all_parameters()

    def thaw_all_parameters(self):
        self.mean.thaw_all_parameters()
        self.log_white_noise.thaw_all_parameters()
        self.kernel.thaw_all_parameters()

    def get_parameter(self, name):
        return self._apply_to_parameter("get_parameter", name)

    def set_parameter(self, name, value):
        self.dirty = True
        return self._apply_to_parameter("set_parameter", name, value)