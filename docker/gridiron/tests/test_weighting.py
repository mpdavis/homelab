"""The recency kernel: the knob the whole 'varying amounts of time' idea rides on."""

from __future__ import annotations

import numpy as np
import pytest

from gridiron.models.weighting import Sweep, Weighting


def test_exponential_halves_at_the_half_life():
    kernel = Weighting(kind="exponential", half_life_days=100.0, season_carryover=1.0)
    weights = kernel.weights(np.array([0.0, 100.0, 200.0, 300.0]))
    assert weights[0] == pytest.approx(1.0)
    assert weights[1] == pytest.approx(0.5)
    assert weights[2] == pytest.approx(0.25)
    assert weights[3] == pytest.approx(0.125)


def test_future_rows_get_zero_weight():
    """A negative age is a leak. It has to die here, not skew a rating."""
    kernel = Weighting(kind="exponential", half_life_days=100.0, season_carryover=1.0)
    weights = kernel.weights(np.array([-1.0, -400.0, 10.0]))
    assert weights[0] == 0.0
    assert weights[1] == 0.0
    assert weights[2] > 0.0


def test_window_is_a_cliff_and_uniform_is_flat():
    window = Weighting(kind="window", window_days=365.0, season_carryover=1.0)
    weights = window.weights(np.array([0.0, 364.9, 365.1, 900.0]))
    assert list(weights) == [1.0, 1.0, 0.0, 0.0]

    flat = Weighting(kind="uniform", season_carryover=1.0)
    assert list(flat.weights(np.array([0.0, 500.0, 1999.0]))) == [1.0, 1.0, 1.0]


def test_max_age_truncates_every_kind():
    flat = Weighting(kind="uniform", season_carryover=1.0, max_age_days=100.0)
    assert list(flat.weights(np.array([99.0, 101.0]))) == [1.0, 0.0]


def test_season_carryover_discounts_per_offseason():
    kernel = Weighting(kind="uniform", season_carryover=0.5)
    weights = kernel.weights(np.zeros(3), np.array([0, 1, 2]))
    assert list(weights) == [1.0, 0.5, 0.25]


def test_negative_seasons_back_is_clipped_not_amplified():
    """A row from the future must never be worth *more* than a recent one."""
    kernel = Weighting(kind="uniform", season_carryover=0.5)
    weights = kernel.weights(np.zeros(2), np.array([-3, 0]))
    assert list(weights) == [1.0, 1.0]


def test_bad_configuration_is_loud():
    with pytest.raises(ValueError):
        Weighting(kind="exponential", half_life_days=0.0).weights(np.array([1.0]))
    with pytest.raises(ValueError):
        Weighting(kind="nonsense").weights(np.array([1.0]))


def test_sweep_covers_smooth_and_naive_kernels():
    kernels = Sweep().weightings()
    kinds = {kernel.kind for kernel in kernels}
    assert kinds == {"exponential", "window", "uniform"}
    # The point of the sweep is comparison, so every entry must be distinct.
    assert len({kernel.describe() for kernel in kernels}) == len(kernels)


def test_sweep_can_be_narrowed_to_exponentials():
    kernels = Sweep(half_lives=(90, 240), include_windows=False).weightings()
    assert [k.half_life_days for k in kernels] == [90, 240]
