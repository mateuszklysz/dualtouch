"""Layer 1: cursor state machine (vkb.step_cursor) — no controller needed.

Plus Layer 2: the left-stick navigation zones driven through the REAL
ControllerManager (edge -> hold-delay -> repeat cadence, virtual clock).
"""

import pytest
from steamcontroller import SCButtons
from triton import state, vkb


@pytest.fixture
def kb(runner):
    return runner.virtual_kb


def test_left_wraps_to_row_end(runner, kb):
    state.set_cursor(2, 0)
    vkb.step_cursor(kb, "LEFT")
    assert state.get_cursor() == (2, len(kb.keys[2]) - 1)


def test_right_wraps_to_row_start(runner, kb):
    state.set_cursor(2, len(kb.keys[2]) - 1)
    vkb.step_cursor(kb, "RIGHT")
    assert state.get_cursor() == (2, 0)


def test_up_maps_by_pixel_column(runner, kb):
    r, c = 2, 5  # 't'
    layout = kb.get_key_layout(r, c)
    x_center = layout.x + layout.w // 2
    expected = kb.find_col_at_x(r - 1, x_center)
    state.set_cursor(r, c)
    vkb.step_cursor(kb, "UP")
    assert state.get_cursor() == (r - 1, expected)


def test_down_honors_key_override(runner, kb):
    # The ',' key pins dpad_down: col 3 (see keyboard-layout.yaml).
    comma = runner.cell_rc(",")
    state.set_cursor(*comma)
    vkb.step_cursor(kb, "DOWN")
    assert state.get_cursor() == (comma[0] + 1, 3)
    assert state.is_cursor_used()


def test_up_clamps_at_top_row(runner, kb):
    state.set_cursor(0, 4)
    vkb.step_cursor(kb, "UP")
    assert state.get_cursor() == (0, 4)


def test_stick_edge_then_hold_repeat_cadence(runner):
    """One step on crossing the deadzone, then hold-delay 0.35 s, then a
    step every 0.15 s — explicit clock steps so no frame lands on a
    threshold."""
    runner.auto_advance = False
    start = state.get_cursor()
    # Rising edge: exactly one step.
    runner.advance(0.03)
    runner.push(lstick=(20000, 0))
    assert state.get_cursor() == (start[0], start[1] + 1)
    # Hold 0.9 s in 0.06 s frames: repeats at 0.39/0.57/0.75/0.93 -> +4.
    for _ in range(15):
        runner.advance(0.06)
        runner.push(lstick=(20000, 0))
    assert state.get_cursor() == (start[0], start[1] + 5)
    assert runner.haptics == 1 + 5  # open tick + one per stick step
    # Release: zone reset, no phantom extra steps.
    runner.advance(0.05)
    runner.push()
    runner.advance(0.05)
    runner.push()
    assert state.get_cursor() == (start[0], start[1] + 5)


def test_stick_below_deadzone_does_not_navigate(runner):
    start = state.get_cursor()
    for _ in range(10):
        runner.push(lstick=(10000, 0))  # < KBD_STICK_DEADZONE (18480)
    assert state.get_cursor() == start


def test_stick_steps_carry_haptic_tick(runner):
    # Baseline frame already spent the open tick; each stick step buzzes.
    base = runner.haptics
    runner.push(lstick=(20000, 0))
    assert runner.haptics == base + 1


def test_steam_held_routes_stick_to_media(runner):
    """Steam + stick = media transport, NOT cursor navigation."""
    start = state.get_cursor()
    runner.push(lstick=(0, -20000), buttons=SCButtons.STEAM)
    for _ in range(10):
        runner.advance(0.03)
        runner.push(lstick=(0, -20000), buttons=SCButtons.STEAM)
    assert state.get_cursor() == start
    names = [ev[1] for ev in runner.events() if ev[0] == "down"]
    assert any(n.startswith("KEY_VOLUME") for n in names)
