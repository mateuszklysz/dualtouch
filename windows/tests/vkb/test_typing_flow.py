"""Layer 2: typing flows through the real pipeline — chords, button roles,
modifier latches, hold-to-repeat cadence and feedback hooks."""

from steamcontroller import SCButtons
from triton import state, vkb


def _type_at(runner, label):
    rc = runner.cell_rc(label)
    state.set_cursor(*rc)
    runner.push(buttons=SCButtons.A)
    runner.push()


def test_a_inserts_key_under_cursor(runner):
    _type_at(runner, "h")
    _type_at(runner, "i")
    assert runner.typed() == "hi"


def test_y_and_grips_are_space(runner):
    runner.tap_button(SCButtons.Y)
    runner.tap_button(SCButtons.RGRIP1)
    runner.tap_button(SCButtons.RGRIP2)
    assert runner.typed() == "   "


def test_x_taps_backspace(runner):
    _type_at(runner, "a")
    _type_at(runner, "b")
    runner.tap_button(SCButtons.X)
    assert runner.typed() == "a"


def test_x_hold_repeats_on_backspace_cadence(runner):
    """Delete on press, then every KEY_REPEAT_INTERVAL after KEY_REPEAT_DELAY
    — exact counts under the virtual clock (explicit 49 ms steps never land
    on a threshold)."""
    runner.auto_advance = False
    for label in ("a", "s", "d", "f", "g", "h", "j", "k", "l"):
        _type_at(runner, label)
    runner.advance(0.02)
    runner.push(buttons=SCButtons.X)  # press: delete #1
    for _ in range(22):  # hold ~1.08 s more
        runner.advance(0.049)
        runner.push(buttons=SCButtons.X)
    runner.advance(0.02)
    runner.push()
    # Repeats at ~0.46/0.61/0.76/0.90/1.05 -> 5 repeats + press delete.
    assert runner.typed() == "asd"
    # Repeats must not machine-gun the click sound.
    assert runner.sounds >= 1


def test_b_closes_keyboard_and_update_signals_exit(runner):
    runner.tap_button(SCButtons.B)
    assert state.should_close()
    runner.src.exits = 0
    runner.push()
    assert runner.src.exits == 1


def test_lgrip_closes_keyboard(runner):
    runner.push(buttons=SCButtons.LGRIP)
    assert state.should_close()


def test_steam_alone_release_closes_after_grace(runner):
    """The opening chord's Steam is seeded consumed; a fresh Steam
    press->release past the 1.0 s grace closes the keyboard."""
    runner.auto_advance = False
    runner.advance(0.05)
    runner.push(buttons=SCButtons.STEAM)  # rising edge: marks unused
    for _ in range(24):
        runner.advance(0.05)
        runner.push(buttons=SCButtons.STEAM)
    runner.advance(0.05)
    runner.push()  # release, well past the grace window
    assert state.should_close()


def test_steam_within_grace_release_does_not_close(runner):
    runner.auto_advance = False
    runner.advance(0.05)
    runner.push(buttons=SCButtons.STEAM)
    runner.advance(0.2)
    runner.push(buttons=SCButtons.STEAM)
    runner.advance(0.05)
    runner.push()
    assert not state.should_close()


def test_steam_plus_x_chord_keeps_open_after_release(runner):
    """Steam+X marks the Steam press 'used', so its release never closes."""
    runner.auto_advance = False
    runner.advance(0.05)
    runner.push(buttons=SCButtons.STEAM | SCButtons.X)
    for _ in range(24):
        runner.advance(0.05)
        runner.push(buttons=SCButtons.STEAM | SCButtons.X)
    runner.advance(0.05)
    runner.push()
    assert not state.should_close()


def test_l3_caps_lock_event_and_highlight(runner):
    runner.push(buttons=SCButtons.L3)
    names = [ev[1] for ev in runner.events()]
    assert "KEY_CAPSLOCK" in names
    assert vkb.sui.Keys.KEY_CAPSLOCK in state.get_highlighted()


def test_shift_latch_via_click_button(runner):
    runner.click_cell("Shift")
    assert state.is_shift_latched()
    assert state.is_shift_held()
    runner.click_cell("a")
    assert runner.typed() == "A"
    runner.click_cell("Shift")
    assert not state.is_shift_latched()
    runner.click_cell("a")
    assert runner.typed() == "Aa"


def test_ctrl_alt_latches_via_click_button(runner):
    runner.click_cell("Ctrl")
    assert state.is_ctrl_latched()
    runner.push_idle(4)  # clear the pad-click settle window
    runner.click_cell("Alt")
    assert state.is_alt_latched()
    marked = set(state.get_highlighted())
    assert {
        vkb.sui.Keys.KEY_LEFTCTRL,
        vkb.sui.Keys.KEY_LEFTALT,
    } <= marked


def test_key_sound_ticks_once_per_press_not_repeat(runner):
    state.set_cursor(0, 13)  # Backspace under the cursor
    runner.push(buttons=SCButtons.A)  # press edge: one click sound
    for _ in range(12):  # held 0.6 s -> several silent auto-repeats
        runner.advance(0.05)
        runner.push(buttons=SCButtons.A)
    runner.push()
    assert runner.sounds == 1


def test_a_paints_cursor_cell_while_held(runner):
    rc = (3, 1)
    state.set_cursor(*rc)
    runner.push(buttons=SCButtons.A)
    assert state.get_mouse_press_cell() == rc
    runner.push()
    assert state.get_mouse_press_cell() is None


def test_x_highlights_backspace_while_held(runner):
    runner.push(buttons=SCButtons.X)
    assert vkb.sui.Keys.KEY_BACKSPACE in state.get_highlighted()
