"""Headless tests for hover-to-open diacritics.

A real finger resting on a variant-capable key WITHOUT any click activity
opens that key's variant row after ACCENT_HOVER_OPEN seconds. Hover is
gated on Lift-Off Typing — resting a finger is a lift-native gesture, so
with lift-off off the countdown never runs. Hover-opened rows commit on the
finger LIFT (no click will ever release), while moving to another key,
click activity, or lifting before the threshold cancels.
"""

from collections import deque

import pytest

from triton import state
from triton.pad import _PadMixin
from triton.screen import CoordFraction


LPADTOUCH, LT, LPAD = 0x00000200, 0x00000100, 0x00000400


@pytest.fixture(autouse=True)
def _clean_state():
    state.reset_session()
    state.set_diacritics_enabled(True)
    # Most tests exercise the hover flow itself; the liftoff-dependent
    # ones flip this explicitly.
    state.set_sc_liftoff_enter(True)
    yield
    state.set_sc_liftoff_enter(False)
    state.set_diacritics_enabled(True)


def _build_kb():
    from triton import config, layouts, vkb

    cfg = config.YamlFile(layouts.layout_filename("QWERTY"))
    cfg.read()
    cfg.add_to_config("keys", vkb.VirtualKeyboardConfig())
    kb = vkb.VirtualKeyboardConfig().construct()
    kb.update_dimensions()
    return kb


class _KB:
    def __init__(self):
        self.downs = []

    def pressEvent(self, keys):
        self.downs.append(keys)

    def releaseEvent(self, keys):
        pass


class _P:
    def __init__(self, buttons):
        self.buttons = buttons


class _CS:
    def __init__(self):
        self.click_queue = deque()


class _D(_PadMixin):
    _kb: _KB
    sc_input_previous: _P
    controller_state: _CS

    BACKSPACE_HOLD_DELAY = 0.5
    BACKSPACE_REPEAT = 0.05
    PAD_CLICK_SETTLE = 0.05

    def __init__(self):
        self._kb = _KB()
        self._select_pad = None
        self._diacritic_pad = None
        self._deferred_base = {}
        self._click_repeat_at = {}
        self._click_settle_at = {}
        self._select_real_touch = False
        self._select_dir = 0
        self._select_base_dir = 0
        self._select_reverse_buffer = []
        self._liftoff_touch = {}
        self._liftoff_coord = {}
        self._liftoff_clicked = {}
        self._liftoff_t0 = {}
        self._hover_start = {}
        self._hover_rc = {}
        self._diacritic_hover = {}
        self.sc_input_previous = _P(0)
        self.controller_state = _CS()
        self._prev = 0

    def frame(self, buttons, cf, raw_x, now, real_touch=True):
        self.sc_input_previous = _P(self._prev)
        r = self.handle_pad_input(
            cf,
            buttons,
            LPADTOUCH,
            LT,
            click_button_mask=LPAD,
            allow_click=True,
            now=now,
            trigger_pressed=False,
            trigger_prev=False,
            raw_x=raw_x,
            real_touch=real_touch,
        )
        self._prev = buttons
        return r


def _center_cf(kb, row, col):
    layout = kb.get_key_layout(row, col)
    assert layout is not None
    return CoordFraction.from_absolute(
        layout.x + layout.w // 2, layout.y + layout.h // 2
    )


def _rest(d, cf, t, seconds, step=0.1, real_touch=True):
    """Feed resting-finger frames for `seconds`, returning the new clock."""
    n = int(round(seconds / step))
    for _ in range(n):
        d.frame(LPADTOUCH, cf, 0, t, real_touch=real_touch)
        t += step
    return t


def test_hover_opens_variant_row_after_the_window():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    d = _D()
    cf = _center_cf(kb, 3, 1)  # 'a'
    t = 1000.0
    t = _rest(d, cf, t, 0.4)
    assert not state.is_diacritic_open()
    t = _rest(d, cf, t, 0.2)
    # The frame crossing ACCENT_HOVER_OPEN (0.5 s) opens the row and latches
    # it as hover-opened.
    assert state.is_diacritic_open()
    assert d._diacritic_pad == LT
    assert d._diacritic_hover.get(LT) is True


def test_hover_silent_while_counting_down():
    """Resting must never type anything — the insert only comes from the
    opened row's commit (or the ordinary click paths)."""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    t = _rest(d, cf, t, 0.4)
    assert list(d.controller_state.click_queue) == []
    assert not state.is_diacritic_open()


def test_hover_moving_to_another_key_restarts():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    d = _D()
    a = _center_cf(kb, 3, 1)
    s = _center_cf(kb, 3, 2)
    t = 1000.0
    t = _rest(d, a, t, 0.4)
    # Slide one key over: the countdown starts from zero there.
    t = _rest(d, s, t, 0.4)
    assert not state.is_diacritic_open()
    # ...and back to 'a': another full window from scratch.
    t = _rest(d, a, t, 0.4)
    assert not state.is_diacritic_open()
    t = _rest(d, a, t, 0.2)
    assert state.is_diacritic_open()


def test_hover_cancelled_by_click_activity():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    t = _rest(d, cf, t, 0.4)
    # A pad click interrupts the rest (press edge + release while touching).
    d.frame(LPADTOUCH | LPAD, cf, 0, t)
    t += 0.05
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.05
    # More than the original window remains under the threshold from NOW:
    # a further 0.4 s of rest must NOT open (the countdown restarted).
    t = _rest(d, cf, t, 0.4)
    assert not state.is_diacritic_open()
    t = _rest(d, cf, t, 0.2)
    assert state.is_diacritic_open()


def test_hover_cancelled_by_finger_lift_mid_countdown():
    """Lifting the finger before the threshold kills the half-counted rest:
    the countdown restarts from zero on the next touch. (Hover needs
    Lift-Off Typing on, so the mid-rest lift also fires its ordinary
    lift-off insert — asserted separately in test_layouts_liftoff.)"""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    t = _rest(d, cf, t, 0.3)
    assert not state.is_diacritic_open()
    # The finger lifts mid-countdown...
    t += 0.01
    d.frame(0, cf, 0, t, real_touch=False)
    # ...and comes back down: a fresh window starts from zero.
    t += 0.05
    d.frame(LPADTOUCH, cf, 0, t)
    t = _rest(d, cf, t + 0.1, 0.3)
    assert not state.is_diacritic_open()
    t = _rest(d, cf, t, 0.2)
    assert state.is_diacritic_open()


def test_hover_never_counts_synthetic_touch():
    """Only the REAL finger counts — the synthesized touch of a held click
    button (or a stick-driven pointer) must not open rows by itself."""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    t = _rest(d, cf, t, 3.0, real_touch=False)
    assert not state.is_diacritic_open()


def test_hover_disabled_when_diacritics_off():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_diacritics_enabled(False)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    t = _rest(d, cf, t, 3.0)
    assert not state.is_diacritic_open()


def test_hover_disabled_when_liftoff_off():
    """Resting a finger is a lift-native gesture: with Lift-Off Typing off
    the countdown never runs."""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(False)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    t = _rest(d, cf, t, 3.0)
    assert not state.is_diacritic_open()


def test_hover_row_commits_first_variant_on_finger_lift():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    t = _rest(d, cf, t, 2.25)
    assert state.is_diacritic_open()
    # Resting frames past the opening keep highlighting whatever sits under
    # the finger inside the row, so read the CURRENT selection rather than
    # assuming the first variant.
    expected = state.get_diacritic_selected_char()
    if expected is None:
        expected = state.get_diacritic_variants_list()[0]
    # Finger lifts (no click was ever held): commit the highlighted variant.
    t += 0.01
    d.frame(0, cf, 0, t, real_touch=False)
    queue = list(d.controller_state.click_queue)
    assert queue == [("variant", expected)]
    # The row itself is closed by commit_diacritic when the MAIN thread
    # consumes the queued commit; the pad side must already be unlatched:
    assert d._diacritic_pad is None
    assert d._diacritic_hover.get(LT) is None


def test_accent_commit_plays_the_click_sound(monkeypatch):
    """Typing an accented variant clicks — one tick per inserted character.
    A failed injection stays silent."""
    from triton import vkb

    kb = _build_kb()
    state.set_virtual_kb(kb)
    ticks = []
    # Earlier runner-fixture tests leave key sound globally disabled
    # (state_cleanup); this test owns both knobs and restores them.
    saved_enabled = state.is_key_sound_enabled()
    state.set_key_sound_enabled(True)
    state.set_key_sound(lambda: ticks.append(1))
    try:
        monkeypatch.setattr(vkb.kb, "tap_char", lambda ch: True)
        assert vkb.open_diacritic_rc(kb, 3, 1, "pad")
        state.set_diacritic_index(0)
        vkb.commit_diacritic()
        assert ticks == [1]

        monkeypatch.setattr(vkb.kb, "tap_char", lambda ch: False)
        assert vkb.open_diacritic_rc(kb, 3, 1, "pad")
        state.set_diacritic_index(0)
        vkb.commit_diacritic()
        assert ticks == [1]
    finally:
        state.set_key_sound(None)
        state.set_key_sound_enabled(saved_enabled)


def test_hover_row_lift_does_not_double_fire_liftoff_insert():
    """With Lift-Off Typing ALSO enabled, an opened row's lift commits the
    variant only — the lift-off insert must stay suppressed for the pad
    that owns the row."""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    t = _rest(d, cf, t, 2.25)
    assert state.is_diacritic_open()
    expected = state.get_diacritic_selected_char()
    if expected is None:
        expected = state.get_diacritic_variants_list()[0]
    t += 0.01
    d.frame(0, cf, 0, t, real_touch=False)
    queue = list(d.controller_state.click_queue)
    assert queue == [("variant", expected)]
