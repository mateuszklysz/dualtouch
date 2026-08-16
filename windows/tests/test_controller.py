"""Headless tests for triton/controller.py pad-to-pixel mapping.

The OSK is 1286x369 (medium). `adjust_raw_x/y` overshoots the window by
design so every key is reachable, but the raw pad extremes land FAR outside
the window (e.g. y=-258 / +627), where a pad click resolves to NO key — the
top/bottom/left/right edges of the keyboard were dead. The functions now
clamp into the window so the full pad surface maps to clickable keys.
"""

from triton import controller, screen


def test_adjust_raw_y_clamps_into_window():
    screen.width, screen.height = 1286, 369
    assert controller.adjust_raw_y(0x10000, 1 / 2) == 0
    assert controller.adjust_raw_y(-0x10000, 1 / 2) == screen.height
    assert controller.adjust_raw_y(0, 1 / 2) == screen.height // 2


def test_adjust_raw_x_clamps_into_window():
    screen.width, screen.height = 1286, 369
    assert controller.adjust_raw_x(-0x20000, 3 / 4) == 0
    assert controller.adjust_raw_x(0x20000, 3 / 4) == screen.width
    assert controller.adjust_raw_x(-0x20000, 1 / 4) == 0
    assert controller.adjust_raw_x(0x20000, 1 / 4) == screen.width


def test_adjust_raw_never_returns_outside_window():
    screen.width, screen.height = 1286, 369
    for rx in range(-0x20000, 0x20001, 0x4000):
        for cf in (1 / 4, 3 / 4):
            assert 0 <= controller.adjust_raw_x(rx, cf) <= screen.width
    for ry in range(-0x10000, 0x10001, 0x2000):
        assert 0 <= controller.adjust_raw_y(ry, 1 / 2) <= screen.height


def test_adjust_raw_x_span_maps_full_int16_range():
    """Split-layout pad span mapping: the touchpad raw X is the HID report's
    int16 (±0x8000), so the pad's whole travel must land in its band. (The
    old ±0x20000 scale squeezed every pad into the middle 25% of its span,
    leaving the outer keys unreachable.)"""
    screen.width, screen.height = 2560, 369
    assert controller.adjust_raw_x_span(-0x8000, 0, 730) == 0
    assert controller.adjust_raw_x_span(0, 0, 730) == 365
    assert controller.adjust_raw_x_span(0x7FFF, 0, 730) == 730
    assert controller.adjust_raw_x_span(-0x8000, 1830, 2560) == 1830
    assert controller.adjust_raw_x_span(0, 1830, 2560) == 2195
    assert controller.adjust_raw_x_span(0x7FFF, 1830, 2560) == 2560
    assert controller.adjust_raw_x_span(-0x10000, 0, 730) == 0  # clamped
    assert controller.adjust_raw_x_span(0x10000, 1830, 2560) == 2560


"""Headless tests for the Select key (iOS hold-space text selection)."""


from triton import (  # noqa: E402 -- intentional section import
    config,
    state,
    vkb,
)


def _build_kb():
    cfg = config.YamlFile("keyboard-layout.yaml")
    cfg.read()
    cfg.add_to_config("keys", vkb.VirtualKeyboardConfig())
    kb = vkb.VirtualKeyboardConfig().construct()
    kb.update_dimensions()
    return kb


def test_select_key_replaces_right_shift():
    kb = _build_kb()
    select_keys = [
        (r, c)
        for r, row in enumerate(kb.keys)
        for c, key in enumerate(row)
        if key.is_select
    ]
    # Exactly one Select key, in the same bottom-right slot the right Shift
    # occupied (last key of the letter row, far right).
    assert len(select_keys) == 1
    r, c = select_keys[0]
    key = kb.keys[r][c]
    assert key.str == "Select"
    assert key.keycode == vkb.SELECT_KEYCODE
    # It must sit at the far right of its row (was the right Shift).
    assert c == len(kb.keys[r]) - 1


def test_select_key_reachable_at_window_edge():
    kb = _build_kb()
    r, c = [
        (r, c)
        for r, row in enumerate(kb.keys)
        for c, key in enumerate(row)
        if key.is_select
    ][0]
    layout = kb.get_key_layout(r, c)
    assert layout is not None
    # A press on the Select key's own center must resolve back to it.
    key = kb.find_key_expanded(
        layout.x + layout.w // 2, layout.y + layout.h // 2
    )
    assert key is not None and key.is_select


def test_select_state_flag_roundtrip():
    state.set_select_active(True)
    assert state.is_select_active() is True
    state.set_select_active(False)
    assert state.is_select_active() is False


"""Headless tests for the Select-key drag: edge-pin extension + reverse
suppression while pinned (the coast/inertia was removed — lifting always
stops the selection exactly where the drag stopped)."""

from triton.pad import _PadMixin  # noqa: E402 -- intentional section import


class _KB:
    def __init__(self):
        self.downs = []

    def pressEvent(self, keys):
        self.downs.append(keys)

    def releaseEvent(self, keys):
        pass


class _Dummy(_PadMixin):
    _kb: _KB

    def __init__(self):
        self._kb = _KB()
        self._select_pad = 1
        self._select_anchor_x = 0
        self._select_edge_repeat_at = 0.0
        self._select_edge_pin_dir = 0
        self._select_dir = 0
        self._select_base_dir = 0
        self._select_reverse_buffer = []


def test_drag_fires_arrows_in_travel_direction():
    d = _Dummy()
    d._select_anchor_x = 0
    for i, x in enumerate(range(0, 0x8000, 0x2000)):
        d._select_drag(x, 100.0 + i * 0.016)
    assert d._kb.downs
    assert all(k == ["KEY_RIGHT"] for k in d._kb.downs)


def test_drag_reverse_fires_left():
    d = _Dummy()
    d._select_anchor_x = 0x8000
    for i, x in enumerate(range(0x8000, 0, -0x2000)):
        d._select_drag(x, 100.0 + i * 0.016)
    assert d._kb.downs
    assert all(k == ["KEY_LEFT"] for k in d._kb.downs)


def test_fresh_placement_does_not_fire_arrows():
    # Placing the finger on the pad (to type) must NOT drag the selection:
    # the anchor re-anchors on a fresh touch, so only horizontal travel
    # after the placement fires. (Regression for "typing on the touchpad
    # drags the selection" / "place on left selects left".)
    d = _Dummy()
    d._select_anchor_x = 0
    # Fresh placement on the LEFT edge: with the old anchor at 0 this would
    # have fired LEFT arrows; with re-anchor it must fire nothing.
    d._select_anchor_x = raw_x = -0x7000
    # Simulate handle_pad_input's fresh-touch re-anchor branch.
    d._select_dir = 0
    d._select_base_dir = 0
    d._select_reverse_buffer = []
    d._select_drag(raw_x, 100.0)
    assert d._kb.downs == []
    # Then dragging right from there fires RIGHT.
    d._select_drag(-0x7000 + 0x2000, 100.016)
    assert d._kb.downs and all(k == ["KEY_RIGHT"] for k in d._kb.downs)


def test_midpad_roll_back_on_lift_cancelled():
    # Roll-back fires LEFT immediately (precision) but is cancelled on lift.
    d = _Dummy()
    d._select_anchor_x = 0x5000
    d._select_dir = 1
    d._select_base_dir = 1
    d._select_reverse_buffer = []
    t = 100.0
    for rx in (0x3000, 0x1000):
        d._select_drag(rx, t)
        t += 0.016
    lefts = sum(1 for k in d._kb.downs if k == ["KEY_LEFT"])
    assert lefts > 0
    d._end_select(t)
    net = sum(1 for k in d._kb.downs if k == ["KEY_LEFT"]) - sum(
        1 for k in d._kb.downs if k == ["KEY_RIGHT"]
    )
    assert net == 0


def test_micro_adjust_fires_every_arrow_immediately():
    # THE reported bug: rapid right-left-right must fire each arrow 1:1 with
    # no swallowed travel (the previous model's debounce swallowed reverses).
    d = _Dummy()
    d._select_anchor_x = 0
    t = 100.0
    d._select_drag(0x1000, t)
    t += 0.016
    d._select_drag(0x0000, t)
    t += 0.016
    d._select_drag(0x1000, t)
    assert d._kb.downs == [["KEY_RIGHT"], ["KEY_LEFT"], ["KEY_RIGHT"]]


def test_deliberate_reverse_mostly_survives_lift():
    # A deliberate, SLOWLY-held drag-back (spanning well past the 0.12s
    # roll-back window) keeps most of its reverse arrows after lift; only
    # the tail within the window is cancelled as possible roll-back.
    d = _Dummy()
    d._select_anchor_x = 0x5000
    d._select_dir = 1
    d._select_base_dir = 1
    d._select_reverse_buffer = []
    t = 100.0
    # Reverse over ~0.5s (slow: 0x1000 per 0.05s), 10 steps.
    for i in range(10):
        d._select_drag(0x5000 - (i + 1) * 0x1000, t)
        t += 0.05
    fired = sum(1 for k in d._kb.downs if k == ["KEY_LEFT"])
    d._end_select(t)
    net = sum(1 for k in d._kb.downs if k == ["KEY_LEFT"]) - sum(
        1 for k in d._kb.downs if k == ["KEY_RIGHT"]
    )
    assert fired >= 5  # the deliberate reverse did fire
    assert net > 0  # most of it survives the lift
    assert net < fired  # only the recent tail is cancelled


def test_clickbutton_select_place_then_drag():
    # User repro: hold Select via the click button (R1), which synthesizes
    # TOUCH while held. Lift the finger, place it elsewhere, and the
    # selection must NOT jump — only a real drag selects. This drives the
    # real handle_pad_input with real_touch transitions.
    from triton import state as _st
    from triton.screen import CoordFraction

    class _LocalKB:
        def __init__(self):
            self.downs = []
            self.shift = 0

        def pressEvent(self, keys):
            if keys == ["KEY_LEFTSHIFT"]:
                self.shift += 1
            else:
                self.downs.append(keys)

        def releaseEvent(self, keys):
            if keys == ["KEY_LEFTSHIFT"]:
                self.shift -= 1

    class _P:
        def __init__(self, buttons):
            self.buttons = buttons

    class _CS:
        def __init__(self):
            self.click_queue = []

    class _D(_PadMixin):
        _kb: _LocalKB
        controller_state: _CS

        def __init__(self):
            self._kb = _LocalKB()
            self._prev_buttons = 0
            self._select_pad = None
            self._select_anchor_x = 0.0
            self._select_dir = 0
            self._select_base_dir = 0
            self._select_reverse_buffer = []
            self._select_real_touch = False
            self._click_settle_at = {}
            self._click_repeat_at = {}
            self.controller_state = _CS()

        def _is_select_key(self, coord_frac):
            return True

    d = _D()
    RPADTOUCH, RPAD, RT = 0x00200000, 0x00400000, 0x00800000
    _st.set_select_active(False)

    def frame(buttons, real_touch, raw_x, now):
        d.sc_input_previous = _P(d._prev_buttons)
        ret = d.handle_pad_input(
            CoordFraction(0.9, 0.9),
            buttons,
            RPADTOUCH,
            RT,
            click_button_mask=RPAD,
            allow_click=True,
            now=now,
            trigger_pressed=False,
            trigger_prev=False,
            raw_x=raw_x,
            real_touch=real_touch,
        )
        d._prev_buttons = buttons
        return ret

    t = 100.0
    # Enter select: R1 rising edge, finger on pad.
    frame(RT | RPADTOUCH | RPAD, True, 0x3000, t)
    assert d._select_pad is not None, "select mode should be entered"
    t += 0.016
    # Finger lifts while R1 held: freeze, selection must not move.
    before = len(d._kb.downs)
    frame(RT | RPADTOUCH | RPAD, False, 0x3000, t)
    assert len(d._kb.downs) == before
    t += 0.016
    # Place finger on the LEFT edge: must re-anchor, no arrows.
    frame(RT | RPADTOUCH | RPAD, True, -0x7000, t)
    assert len(d._kb.downs) == before, "placement must not fire arrows"
    # Drag right: fires RIGHT.
    frame(RT | RPADTOUCH | RPAD, True, -0x5000, t + 0.016)
    assert d._kb.downs and all(
        k == ["KEY_RIGHT"] for k in d._kb.downs[before:]
    )
    _st.set_select_active(False)
