"""Layer 1: pure math helpers (utils) and the CoordFraction value type."""

import pytest
from triton import utils
from triton.geometry import CoordFraction, set_dims


def test_clamp_orders_bounds():
    assert utils.clamp(5, 0, 10) == 5
    assert utils.clamp(-1, 0, 10) == 0
    assert utils.clamp(11, 0, 10) == 10
    assert utils.clamp(3, 3, 3) == 3


def test_round_to_int_is_bankers():
    # Python's round() half-to-even — asserted so a future switch to
    # round-half-up is a conscious change, not an accident.
    assert utils.round_to_int(0.5) == 0
    assert utils.round_to_int(1.5) == 2
    assert utils.round_to_int(-0.5) == 0
    assert utils.round_to_int(2.4) == 2


def test_compute_lowpass_endpoints_and_midpoint():
    assert utils.compute_lowpass(100, 0, 1.0) == 100  # identity at alpha=1
    assert utils.compute_lowpass(100, 0, 0.0) == 0  # frozen at alpha=0
    assert utils.compute_lowpass(100, 0, 0.5) == 50


def test_ease_out_back_endpoints_clamped():
    assert utils.ease_out_back(-1.0) == pytest.approx(0.0, abs=1e-12)
    assert utils.ease_out_back(0.0) == pytest.approx(0.0)
    assert utils.ease_out_back(1.0) == pytest.approx(1.0)
    assert utils.ease_out_back(2.0) == 1.0  # t clamped into [0, 1]


def test_ease_out_back_overshoots_then_lands():
    peak = max(utils.ease_out_back(t / 100) for t in range(101))
    assert peak > 1.0  # the springy landing overshoots...


def test_spring_p_settles_and_overshoots():
    assert utils.spring_p(-1.0, 0.7, 20.0) == 0.0
    assert utils.spring_p(0.0, 0.7, 20.0) == 0.0
    # Settled well before ~1 s.
    assert abs(utils.spring_p(1.0, 0.7, 20.0) - 1.0) < 1e-6
    # Sub-critical damping overshoots once on the way up.
    samples = [utils.spring_p(t / 200, 0.7, 20.0) for t in range(1, 200)]
    assert max(samples) > 1.0
    # Monotone rise into the first crossing (no pre-overshoot jitter).
    first_cross = next(i for i, v in enumerate(samples) if v >= 1.0)
    rising = samples[: first_cross + 1]
    assert rising == sorted(rising)


def test_spring_p_critical_damping_no_crash():
    """ζ=1.0 used to divide by √0; it must take the damped-limit form."""
    p = utils.spring_p(0.05, 1.0, 20.0)
    assert 0.0 < p < 1.0
    assert abs(utils.spring_p(5.0, 1.0, 20.0) - 1.0) < 1e-9
    # Over-damped also fine.
    assert 0.0 < utils.spring_p(0.05, 1.4, 20.0) < 1.0


def test_coord_fraction_roundtrip_and_resize():
    try:
        set_dims(1000, 500)
        cf = CoordFraction.from_absolute(250, 125)
        assert (cf.x_fraction, cf.y_fraction) == (0.25, 0.25)
        assert cf.to_absolute() == (250.0, 125.0)
        # Fractions survive a resize; pixels re-scale.
        set_dims(2000, 500)
        assert cf.to_absolute() == (500.0, 125.0)
        cf.update_absolute(1000, 250)
        assert (cf.x_fraction, cf.y_fraction) == (0.5, 0.5)
    finally:
        set_dims(1286, 369)
