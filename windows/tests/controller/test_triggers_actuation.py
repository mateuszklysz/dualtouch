"""Layer 1: analog trigger actuation (_TriggerMixin._osk_trigger_pressed)
— firmware full-pull bit vs the lowered sc_osk_trigger_actuation point."""

import pytest
from triton import state
from triton.triggers import _TriggerMixin


class _Host(_TriggerMixin):
    pass


@pytest.fixture(autouse=True)
def reset_threshold():
    yield
    state.set_sc_osk_trigger_threshold(None)


LT_BIT = 1 << 7


def test_full_pull_bit_always_presses():
    h = _Host()
    assert h._osk_trigger_pressed(LT_BIT | 0x0F00, LT_BIT, 0.0)


def test_no_threshold_ignores_analog():
    h = _Host()
    state.set_sc_osk_trigger_threshold(None)
    assert not h._osk_trigger_pressed(0, LT_BIT, 30000)


def test_low_actuation_engages_at_threshold():
    h = _Host()
    state.set_sc_osk_trigger_threshold(8000)
    assert not h._osk_trigger_pressed(0, LT_BIT, 7999)
    assert h._osk_trigger_pressed(0, LT_BIT, 8000)
    assert h._osk_trigger_pressed(0, LT_BIT, 32767)
