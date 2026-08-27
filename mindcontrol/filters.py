"""Signal smoothing.

Landmark streams are noisy at a level you can see as cursor jitter, but a plain
low-pass trades that jitter for lag you can feel. The one-euro filter adapts:
heavy smoothing while the hand is still, light smoothing while it moves, so the
pointer is both steady when parked and responsive when thrown.

Reference: Casiez, Roussel & Vogel, "1 e filter" (CHI 2012).
"""

from __future__ import annotations

import math


def _alpha(cutoff: float, dt: float) -> float:
    tau = 1.0 / (2.0 * math.pi * max(cutoff, 1e-6))
    return 1.0 / (1.0 + tau / max(dt, 1e-6))


class _LowPass:
    __slots__ = ("_value",)

    def __init__(self) -> None:
        self._value: float | None = None

    def __call__(self, sample: float, alpha: float) -> float:
        self._value = (
            sample if self._value is None else alpha * sample + (1.0 - alpha) * self._value
        )
        return self._value

    @property
    def value(self) -> float | None:
        return self._value

    def reset(self) -> None:
        self._value = None


class OneEuroFilter:
    """Adaptive low-pass filter for a scalar stream."""

    def __init__(self, fc_min: float = 1.0, beta: float = 0.01, dc_cutoff: float = 1.0) -> None:
        self.fc_min = fc_min
        self.beta = beta
        self.dc_cutoff = dc_cutoff
        self._x = _LowPass()
        self._dx = _LowPass()

    def __call__(self, sample: float, dt: float) -> float:
        previous = self._x.value
        rate = 0.0 if previous is None else (sample - previous) / max(dt, 1e-6)
        edge = self._dx(rate, _alpha(self.dc_cutoff, dt))
        cutoff = self.fc_min + self.beta * abs(edge)
        return self._x(sample, _alpha(cutoff, dt))

    def reset(self) -> None:
        self._x.reset()
        self._dx.reset()


class OneEuroFilter2D:
    """Two-axis one-euro filter that shares one adaptive cutoff.

    Both axes are driven by the *combined* speed rather than their own, so a
    diagonal move is smoothed evenly instead of bending toward whichever axis
    happened to be quieter.
    """

    def __init__(self, fc_min: float = 1.0, beta: float = 0.01, dc_cutoff: float = 1.0) -> None:
        self.fc_min = fc_min
        self.beta = beta
        self.dc_cutoff = dc_cutoff
        self._x = _LowPass()
        self._y = _LowPass()
        self._speed = _LowPass()

    def __call__(self, x: float, y: float, dt: float) -> tuple[float, float]:
        px, py = self._x.value, self._y.value
        stale = px is None or py is None
        rate = 0.0 if stale else math.hypot(x - px, y - py) / max(dt, 1e-6)
        edge = self._speed(rate, _alpha(self.dc_cutoff, dt))
        alpha = _alpha(self.fc_min + self.beta * abs(edge), dt)
        return self._x(x, alpha), self._y(y, alpha)

    @property
    def speed(self) -> float:
        """Smoothed magnitude of recent motion, in input units per second."""
        return abs(self._speed.value or 0.0)

    def reset(self) -> None:
        self._x.reset()
        self._y.reset()
        self._speed.reset()
