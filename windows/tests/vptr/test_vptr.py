"""Layer 1: VirtualPointer smoothing semantics (the pad-pointer low-pass)."""


from triton import state
from triton.geometry import CoordFraction
from triton.vptr import VirtualPointer


def _abs_eq(got, want):
    x, y = got
    assert (round(x), round(y)) == want


def _ptr(x, y):
    return VirtualPointer(
        state.InputState.HOVER, CoordFraction.from_absolute(x, y)
    )


def test_smoothen_alpha_one_is_identity():
    prev = _ptr(100, 100)
    cur = _ptr(200, 300)
    cur.smoothen(prev, 1.0)
    _abs_eq(cur.coord_frac.to_absolute(), (200, 300))


def test_smoothen_alpha_zero_holds_previous():
    prev = _ptr(100, 100)
    cur = _ptr(200, 300)
    cur.smoothen(prev, 0.0)
    _abs_eq(cur.coord_frac.to_absolute(), (100, 100))


def test_smoothen_glide_is_fraction_of_the_way():
    prev = _ptr(0, 0)
    cur = _ptr(100, 200)
    cur.smoothen(prev, 0.25)
    _abs_eq(cur.coord_frac.to_absolute(), (25, 50))


def test_smoothen_mutates_receiver_not_prev():
    prev = _ptr(10, 10)
    cur = _ptr(110, 110)
    cur.smoothen(prev, 0.5)
    _abs_eq(prev.coord_frac.to_absolute(), (10, 10))
    _abs_eq(cur.coord_frac.to_absolute(), (60, 60))
