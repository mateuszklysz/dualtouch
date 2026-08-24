"""Headless tests for lift-mode accent opening.

With Lift-Off Typing ON, pressing the pad click on a variant-capable key
opens its accent row INSTANTLY — no hold, no rest gesture. The click's
release commits the highlighted variant (first variant when nothing was
picked), and the touch's later lift stays silent because the click consumed
the insert. With Lift-Off Typing OFF nothing changes: a quick press defers
the base letter, and only holding past ACCENT_HOLD_OPEN opens the row.
"""

from collections import deque

import pytest
from triton import state
from triton.pad import _PadMixin
from triton.screen import CoordFraction

LPADTOUCH, LT, LPAD = 0x00000200, 0x00000100, 0x00000400
RPADTOUCH, RT, RPAD = 0x00000800, 0x00001000, 0x00002000


@pytest.fixture(autouse=True)
def _clean_state():
    state.reset_session()
    state.set_diacritics_enabled(True)
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
        self.sc_input_previous = _P(0)
        self.controller_state = _CS()
        self._prev = 0

    def frame(
        self,
        buttons,
        cf,
        raw_x,
        now,
        real_touch=True,
        touch_mask=LPADTOUCH,
        sel_mask=LT,
        click_mask=LPAD,
    ):
        self.sc_input_previous = _P(self._prev)
        r = self.handle_pad_input(
            cf,
            buttons,
            touch_mask,
            sel_mask,
            click_button_mask=click_mask,
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


def test_lift_mode_press_opens_accent_row_instantly():
    """Lift-Off Typing on: the pad press itself shows the accents — no hold."""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    d = _D()
    cf = _center_cf(kb, 3, 1)  # 'a'
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    assert not state.is_diacritic_open()
    t += 0.02
    d.frame(LPADTOUCH | LPAD, cf, 0, t)  # pad-click press edge
    assert state.is_diacritic_open()
    assert state.get_diacritic_source() == "pad"
    assert d._diacritic_pad == LT


def test_without_lift_mode_press_stays_deferred():
    """Lift-Off Typing off: a quick press must NOT open anything — the base
    letter comes on release exactly as before."""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(False)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.02
    d.frame(LPADTOUCH | LPAD, cf, 0, t)
    assert not state.is_diacritic_open()
    t += 0.02
    d.frame(LPADTOUCH, cf, 0, t)  # click released
    assert list(d.controller_state.click_queue) == [("deferred", cf)]


def test_without_lift_mode_hold_still_opens_after_the_window():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(False)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.02
    d.frame(LPADTOUCH | LPAD, cf, 0, t)  # press edge arms ACCENT_HOLD_OPEN
    t += 0.4
    d.frame(LPADTOUCH | LPAD, cf, 0, t)
    assert not state.is_diacritic_open()
    t += 0.2  # crosses 0.5 s of hold
    d.frame(LPADTOUCH | LPAD, cf, 0, t)
    assert state.is_diacritic_open()
    assert d._diacritic_pad == LT


def test_lift_mode_release_commits_highlighted_variant():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.02
    d.frame(LPADTOUCH | LPAD, cf, 0, t)
    assert state.is_diacritic_open()
    state.set_diacritic_index(1)
    variants = state.get_diacritic_variants_list()
    t += 0.02
    d.frame(LPADTOUCH, cf, 0, t)  # click released → commit the pick
    assert list(d.controller_state.click_queue) == [("variant", variants[1])]
    # The pad side unlatched even though the MAIN thread closes the row when
    # it consumes the commit.
    assert d._diacritic_pad is None


def test_lift_mode_release_without_pick_commits_first_variant():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.02
    d.frame(LPADTOUCH | LPAD, cf, 0, t)
    state.set_diacritic_index(-1)  # simulate "no explicit pick"
    variants = state.get_diacritic_variants_list()
    t += 0.02
    d.frame(LPADTOUCH, cf, 0, t)
    assert list(d.controller_state.click_queue) == [("variant", variants[0])]


def test_lift_mode_accent_click_never_double_inserts_on_lift():
    """The whole accent gesture consumes the touch: after the click released
    and committed, lifting the finger must stay silent."""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.02
    d.frame(LPADTOUCH | LPAD, cf, 0, t)  # row opens
    t += 0.02
    d.frame(LPADTOUCH, cf, 0, t)  # click releases, commits v0
    committed = [
        item
        for item in d.controller_state.click_queue
        if isinstance(item, tuple)
    ]
    assert len(committed) == 1
    t += 0.08
    d.frame(0, cf, 0, t, real_touch=False)  # finger lifts
    assert [
        item
        for item in d.controller_state.click_queue
        if not isinstance(item, tuple)
    ] == []


def test_lift_mode_press_types_immediately_when_diacritics_off():
    """No variants configured: the press behaves like any normal key press."""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    state.set_diacritics_enabled(False)
    d = _D()
    cf = _center_cf(kb, 3, 1)
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.02
    d.frame(LPADTOUCH | LPAD, cf, 0, t)
    assert not state.is_diacritic_open()
    assert list(d.controller_state.click_queue) == [cf]


def test_row_already_open_second_pad_press_defers():
    """With a row open on the left pad, a right-pad press on another variant
    key can't open a second row — it falls back to the plain defer model."""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    d = _D()
    left = _center_cf(kb, 3, 1)
    right = _center_cf(kb, 3, 2)
    t = 1000.0
    d.frame(LPADTOUCH, left, 0, t)
    t += 0.02
    d.frame(LPADTOUCH | LPAD, left, 0, t)
    assert state.is_diacritic_open()
    t += 0.02
    d.frame(
        RPADTOUCH | RPAD,
        right,
        0x7FFF,
        t,
        touch_mask=RPADTOUCH,
        sel_mask=RT,
        click_mask=RPAD,
    )
    assert d._deferred_base.get(RT) is not None
    t += 0.02
    d.frame(
        RPADTOUCH,
        right,
        0x7FFF,
        t,
        touch_mask=RPADTOUCH,
        sel_mask=RT,
        click_mask=RPAD,
    )
    assert list(
        item
        for item in d.controller_state.click_queue
        if isinstance(item, tuple) and item[0] == "deferred"
    ) == [("deferred", right)]
