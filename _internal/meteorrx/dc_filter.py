"""Remove the receiver's complex I/Q DC offset before spectrum and decoding."""
from __future__ import annotations

import math

import numpy as np


class DcOffsetRemover:
    """Track the center-bin offset without changing signals away from DC."""

    def __init__(self, sample_rate: float, settle_s: float = 0.25):
        self._settle_samples = max(1.0, float(sample_rate) * settle_s)
        self.reset()

    def reset(self):
        self._offset: complex | None = None

    def process(self, samples: np.ndarray) -> np.ndarray:
        if len(samples) == 0:
            return samples
        block_mean = complex(np.mean(samples, dtype=np.complex128))
        if self._offset is None:
            self._offset = block_mean
        else:
            weight = -math.expm1(-len(samples) / self._settle_samples)
            self._offset += weight * (block_mean - self._offset)
        return np.asarray(samples, dtype=np.complex64) - np.complex64(self._offset)
