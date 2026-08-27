"""The smoothing filter, which decides whether the cursor feels alive or drunk."""

from __future__ import annotations

import pytest

from mindcontrol.filters import OneEuroFilter, OneEuroFilter2D


def test_settles_on_a_constant():
    f = OneEuroFilter(fc_min=1.0, beta=0.007)
    for _ in range(200):
        value = f(5.0, 1 / 30.0)
    assert value == pytest.approx(5.0, abs=1e-3)


def test_first_sample_passes_through():
    """No warm-up lag: the filter must start where the signal starts."""
    assert OneEuroFilter()(3.7, 1 / 30.0) == pytest.approx(3.7)


def test_jitter_is_attenuated():
    f = OneEuroFilter(fc_min=1.0, beta=0.0)
    noisy = [10.0, -10.0] * 40
    outputs = [f(v, 1 / 30.0) for v in noisy]
    assert max(abs(v) for v in outputs[10:]) < 5.0


def test_fast_motion_is_not_over_smoothed():
    """The adaptive term must let a deliberate fast move keep up.

    This is the whole point of a One Euro filter over a fixed low-pass: slow
    means steady, fast means responsive.
    """
    lagging = OneEuroFilter(fc_min=1.0, beta=0.0)
    adaptive = OneEuroFilter(fc_min=1.0, beta=2.0)
    for step in range(30):
        target = step * 5.0
        slow = lagging(target, 1 / 30.0)
        quick = adaptive(target, 1 / 30.0)
    assert abs(quick - target) < abs(slow - target)


def test_two_dimensional_axes_are_independent():
    f = OneEuroFilter2D(fc_min=1.0, beta=0.007)
    for _ in range(120):
        x, y = f(2.0, -7.0, 1 / 30.0)
    assert x == pytest.approx(2.0, abs=1e-2)
    assert y == pytest.approx(-7.0, abs=1e-2)


def test_reset_forgets_history():
    f = OneEuroFilter()
    for _ in range(50):
        f(100.0, 1 / 30.0)
    f.reset()
    assert f(0.0, 1 / 30.0) == pytest.approx(0.0)


def test_zero_dt_does_not_divide_by_zero():
    """Duplicate camera timestamps happen; they must not produce NaN or infinity."""
    import math

    f = OneEuroFilter()
    f(1.0, 1 / 30.0)
    assert math.isfinite(f(2.0, 0.0))
