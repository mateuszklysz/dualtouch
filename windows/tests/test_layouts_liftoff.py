"""Headless tests for the multi-layout registry + lift-off typing.

Covers:
- triton/layouts.py: bundled layout discovery, filename mapping, name
  normalization;
- triton.load_kb_config honoring state's selected layout (the OSK rebuilds
  the VirtualKeyboard from the chosen YAML at every open);
- pad._PadMixin lift-off typing: insert on real-finger lift, suppressed when
  disabled, when the touch already clicked, when too short, or when nothing
  resolves under the final position.
"""

from collections import deque

import pytest

from triton import state
from triton.layouts import (
    DEFAULT_LAYOUT,
    available_layouts,
    layout_filename,
    normalize_layout_name,
)
from triton.pad import _PadMixin
from triton.screen import CoordFraction


# --- Layout registry ---------------------------------------------------------


def test_all_bundled_layouts_discovered():
    names = available_layouts()
    for expected in ("QWERTY", "AZERTY", "QWERTZ", "Dvorak", "Colemak", "ABC"):
        assert expected in names
    assert names[0] == DEFAULT_LAYOUT


def test_layout_filename_maps_and_falls_back():
    assert layout_filename("Dvorak") == "keyboard-layout-dvorak.yaml"
    assert layout_filename("AZERTY") == "keyboard-layout-azerty.yaml"
    # Unknown / hostile values fall back to the default board's file — never
    # a path smuggled through settings.json.
    assert layout_filename("nope") == "keyboard-layout.yaml"
    assert layout_filename(None) == "keyboard-layout.yaml"
    assert layout_filename("../etc/passwd") == "keyboard-layout.yaml"


def test_normalize_layout_name_case_insensitive():
    assert normalize_layout_name("dvorak") == "Dvorak"
    assert normalize_layout_name("qwerty") == "QWERTY"
    assert normalize_layout_name(" No Such ") is None
    assert normalize_layout_name(42) is None


def test_user_added_board_roundtrip(tmp_path, monkeypatch):
    """A dropped-in keyboard-layout-*.yaml must be offered AND loadable
    under its derived name — never silently fall back to QWERTY."""
    from triton import layouts

    (tmp_path / "keyboard-layout-brazil.yaml").write_text(
        "keys: []\n", encoding="utf-8"
    )
    monkeypatch.setattr(layouts, "_cfg_dir", lambda: str(tmp_path))

    assert "Brazil" in layouts.available_layouts()
    assert layouts.layout_filename("Brazil") == "keyboard-layout-brazil.yaml"
    assert layouts.layout_filename("brazil") == "keyboard-layout-brazil.yaml"
    assert layouts.normalize_layout_name("BRAZIL") == "Brazil"
    # Unknown names still fall back safely.
    assert layouts.layout_filename("nope") == "keyboard-layout.yaml"


def test_load_kb_config_builds_selected_layout():
    import steamcontroller.uinput as sui

    from triton.triton import load_kb_config

    def probe(name):
        state.set_kb_layout(name)
        try:
            kb = load_kb_config().construct()
        finally:
            state.set_kb_layout(None)
        labels = {}
        for r, row in enumerate(kb.keys):
            for c, key in enumerate(row):
                if key.keycode in (sui.Keys.KEY_S, sui.Keys.KEY_GRAVE):
                    labels[key.keycode] = key.str
                if r == 2 and c == 1:  # first letter slot after Tab
                    labels["tab1"] = key.str
        return (
            labels[sui.Keys.KEY_S],
            labels[sui.Keys.KEY_GRAVE],
            labels["tab1"],
        )

    # One (home-row S slot, grave slot, first-tab-slot) probe distinguishes
    # every bundled board. Dvorak/Colemak pair labels with their own
    # scancodes, so their S slot reads "s" like QWERTY — the arrangement
    # lives elsewhere (see test_layout_consistency.py).
    assert probe("QWERTY") == ("s", "`", "q")
    assert probe("AZERTY") == ("s", "²", "a")
    assert probe("QWERTZ") == ("s", "^", "q")
    assert probe("Dvorak") == ("s", "`", "'")
    assert probe("Colemak") == ("s", "`", "q")
    # ABC keeps every letter on its own keycode, so its labels alone match
    # QWERTY's probes — the tab row is where it visibly differs.
    assert probe("ABC") == ("s", "`", "a")
    # A stale/unknown selection falls back to QWERTY.
    assert probe("Nope") == ("s", "`", "q")


# --- Lift-off typing harness --------------------------------------------------


LPADTOUCH, LT, LPAD = 0x00000200, 0x00000100, 0x00000400


@pytest.fixture(autouse=True)
def _clean_state():
    state.reset_session()
    state.set_sc_liftoff_enter(False)
    yield
    state.set_sc_liftoff_enter(False)


def _build_kb():
    from triton import config, vkb

    cfg = config.YamlFile("keyboard-layout.yaml")
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


def _a_center_cf(kb):
    layout = kb.get_key_layout(3, 1)  # 'a' — same slot the diacritic tests use
    assert layout is not None
    return CoordFraction.from_absolute(
        layout.x + layout.w // 2, layout.y + layout.h // 2
    )


def test_liftoff_inserts_on_finger_lift():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    d = _D()
    cf = _a_center_cf(kb)
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    assert list(d.controller_state.click_queue) == []
    t += 0.08
    d.frame(LPADTOUCH, cf, 0, t)
    assert list(d.controller_state.click_queue) == []
    # Lift: the key under the last finger position is inserted once.
    t += 0.01
    d.frame(0, cf, 0, t, real_touch=False)
    assert list(d.controller_state.click_queue) == [cf]
    # The lift already consumed the touch — further release frames are quiet.
    t += 0.02
    d.frame(0, cf, 0, t, real_touch=False)
    assert list(d.controller_state.click_queue) == [cf]


def test_liftoff_disabled_inserts_nothing():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    d = _D()
    cf = _a_center_cf(kb)
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.08
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.01
    d.frame(0, cf, 0, t, real_touch=False)
    assert list(d.controller_state.click_queue) == []


def test_liftoff_suppressed_after_click_activity():
    """A touch that entered a key through the click path (pad press bit) must
    never fire a second insert when the finger lifts."""
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    state.set_diacritics_enabled(False)
    d = _D()
    cf = _a_center_cf(kb)
    t = 1000.0
    # Pad-click rising edge while touching: the immediate-type path fires.
    d.frame(LPADTOUCH | LPAD, cf, 0, t)
    inserted = list(d.controller_state.click_queue)
    assert inserted == [cf]
    t += 0.05
    d.frame(LPADTOUCH | LPAD, cf, 0, t)
    t += 0.05
    d.frame(LPADTOUCH, cf, 0, t)  # click released, finger still down
    t += 0.08
    d.frame(0, cf, 0, t, real_touch=False)  # finger lifts
    assert list(d.controller_state.click_queue) == inserted


def test_liftoff_ignores_brush_shorter_than_min_touch():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    d = _D()
    cf = _a_center_cf(kb)
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.02  # well under LIFTOFF_MIN_TOUCH
    d.frame(0, cf, 0, t, real_touch=False)
    assert list(d.controller_state.click_queue) == []


def test_liftoff_silent_when_no_key_resolves():
    """With no published keyboard (nothing resolves under the finger) a lift
    must stay silent — covers the _liftoff_resolves guard deterministically."""
    state.set_sc_liftoff_enter(True)
    # The kb persists across a session reset by design, so clear it
    # explicitly: nothing may resolve under the finger.
    state.set_virtual_kb(None)
    d = _D()
    cf = CoordFraction.from_absolute(100, 100)
    t = 1000.0
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.08
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.01
    d.frame(0, cf, 0, t, real_touch=False)
    assert list(d.controller_state.click_queue) == []


def test_liftoff_per_pad_independent():
    kb = _build_kb()
    state.set_virtual_kb(kb)
    state.set_sc_liftoff_enter(True)
    d = _D()
    cf = _a_center_cf(kb)
    t = 1000.0
    # Only the LEFT pad's real finger lifts here; the right pad never
    # touched, so nothing may fire for it (per-pad trackers keyed by mask).
    d.frame(LPADTOUCH, cf, 0, t)
    t += 0.08
    d.frame(0, cf, 0, t, real_touch=False)
    assert len(d.controller_state.click_queue) == 1


def test_release_held_clears_liftoff_trackers():
    from triton.controller import ControllerManager

    class _FakeSC:
        def addExit(self):
            pass

    mgr = ControllerManager.__new__(ControllerManager)
    # Only exercise the tracker-reset part via the dicts the mixin reads;
    # full __init__ needs HID hardware, so build just these attributes.
    mgr._deferred_base = {}
    mgr._a_deferred_cell = None
    mgr._mouse_l_active = False
    mgr._mouse_r_active = False
    mgr._shift_active = False
    mgr._enter_active = False
    mgr._select_pad = None
    mgr._kb = _KB()
    mgr._liftoff_touch = {LT: True}
    mgr._liftoff_coord = {LT: _a_center_cf(_build_kb())}
    mgr._liftoff_clicked = {LT: False}
    mgr._liftoff_t0 = {LT: 1.0}
    # Hover-to-open trackers are cleared alongside the lift-off ones.
    mgr._hover_start = {LT: 2.0}
    mgr._hover_rc = {LT: (3, 1)}
    mgr._diacritic_hover = {LT: True}
    mgr.release_held()
    assert mgr._liftoff_touch == {}
    assert mgr._liftoff_coord == {}
    assert mgr._liftoff_clicked == {}
    assert mgr._liftoff_t0 == {}
    assert mgr._hover_start == {}
    assert mgr._hover_rc == {}
    assert mgr._diacritic_hover == {}


def test_state_roundtrip():
    state.set_kb_layout("Dvorak")
    assert state.get_kb_layout() == "Dvorak"
    state.set_kb_layout(None)
    assert state.get_kb_layout() is None
    state.set_sc_liftoff_enter(True)
    assert state.is_sc_liftoff_enabled() is True
    state.set_sc_liftoff_enter(False)
    assert state.is_sc_liftoff_enabled() is False
