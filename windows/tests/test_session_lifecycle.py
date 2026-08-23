"""Layer 2: session lifecycle — update() gating, the first-frame guard,
the open-close grace boundary, stale-queue cleanup on reopen,
release_held() teardown and feedback gating flags."""

import pytest
from steamcontroller import SCButtons
from triton import controller, state
from triton.geometry import CoordFraction
from triton.screen import set_dims
from triton.triton import drain_input_work


def _raw_frame(status):
    from sc_runner import make_frame

    return make_frame(buttons=SCButtons.A)._replace(status=status)


def _seed_pointers(cstate):
    from triton import state as _st
    from triton.vptr import VirtualPointer

    center = CoordFraction.from_absolute(643, 184)
    idle = VirtualPointer(_st.InputState.INACTIVE, center)
    cstate.set_pointers(
        idle, VirtualPointer(_st.InputState.INACTIVE, center)
    )


def test_update_ignores_non_input_status(runner):
    # SCStatus only defines INPUT; any other status value is inert
    other = 999
    before = runner.manager.sc_input_previous
    controller.update(runner.src, _raw_frame(other), runner.manager)
    assert runner.manager.sc_input_previous is before
    assert not state.should_close()


def test_update_signals_exit_once_closed(runner):
    state.close()
    runner.src.exits = 0
    runner.push()  # update() sees should_close -> addExit, skips input
    assert runner.src.exits == 1


def test_first_frame_only_guards_pads(runner):
    """A fresh manager swallows frame 1 for pad handling (edge baseline);
    from frame 2 on the pointer tracks the finger."""
    m = controller.ControllerManager(runner.cstate)
    aim = runner.raw_at_cell("j")
    # Frame 1: aimed at 'j', but the pad section is skipped.
    controller.update(
        runner.src, sc_make_frame(0, aim), m
    )
    x, y = runner.cstate.get_pointers()[0].coord_frac.to_absolute()
    seeded = CoordFraction.from_absolute(643, 184).to_absolute()
    assert (round(x), round(y)) == (round(seeded[0]), round(seeded[1]))
    # Frame 2: same aim now publishes.
    controller.update(runner.src, sc_make_frame(0, aim), m)
    px, py = runner.cell_center("j")
    x, y = runner.cstate.get_pointers()[0].coord_frac.to_absolute()
    assert (round(x), round(y)) == (px, py)
    drain_input_work(runner.cstate, runner.virtual_kb)


def sc_make_frame(_buttons, kw):
    from sc_runner import make_frame

    return make_frame(**kw)


def test_grace_boundary_is_strict(runner):
    runner.auto_advance = False
    open_t = runner.manager._open_t
    # Advance to EXACTLY the boundary, then release Steam right on it.
    runner.push(buttons=SCButtons.STEAM)  # rising edge: chord unused
    runner.advance(open_t + 1.0 - runner.clock.t)
    runner.push(buttons=SCButtons.STEAM)
    runner.push()  # release at exactly +1.0 s
    assert not state.should_close()
    # The close only ever fires on a Steam FALLING edge past the grace,
    # so a release that landed inside it needs one more press->release.
    runner.advance(0.05)
    runner.push(buttons=SCButtons.STEAM)
    runner.advance(1.05)
    runner.push(buttons=SCButtons.STEAM)
    runner.push()
    assert state.should_close()


def test_reopen_drains_stale_click_queue():
    """The click queue is class-shared; a fresh session must never replay
    items a previous session left queued (phantom key on open)."""
    stale = CoordFraction.from_absolute(100, 100)
    controller.ControllerState.click_queue.append(stale)
    try:
        cstate = controller.ControllerState()
        _seed_pointers(cstate)
        manager = controller.ControllerManager(cstate)
        assert list(cstate.click_queue) == []
        del manager
    finally:
        controller.ControllerState.click_queue.clear()


def test_release_held_drops_everything_os_side(runner):
    import sc_runner

    m = runner.manager

    # Artificially engage every hold the manager can strand.
    m._shift_active = True
    m._enter_active = True
    m._mouse_l_active = True  # holds RIGHT button (swapped)
    m._mouse_r_active = True  # holds LEFT button (swapped)
    m._select_pad = int(SCButtons.LT)
    state.set_select_active(True)
    m._deferred_base[int(SCButtons.LT)] = CoordFraction.from_absolute(
        10, 10
    )
    m._a_deferred_cell = (3, 1)

    m.release_held()

    ups = {
        ev[1] for ev in sc_runner.RecordingKeyboard.EVENTS
        if ev[0] == "up"
    }
    assert "KEY_LEFTSHIFT" in ups  # shift + select teardown release it
    assert "KEY_ENTER" in ups
    mouse = [
        e for e in sc_runner.RecordingMouse.EVENTS if e[0] == "release"
    ]
    assert ("release", "right") in mouse
    assert ("release", "left") in mouse
    assert not m._shift_active and not m._enter_active
    assert m._select_pad is None
    assert not state.is_select_active()
    assert not m._deferred_base and m._a_deferred_cell is None


def test_steam_running_silences_haptics_not_key_sound(runner):
    state.set_steam_running(True)
    state.set_cursor(*runner.cell_rc("a"))
    runner.push(buttons=SCButtons.A)
    runner.push()
    assert runner.typed() == "a"  # typing unaffected...
    base_h, base_p = runner.haptics, runner.pad_haptics
    state.haptic_tick()
    state.pad_click_haptic()
    assert runner.haptics == base_h  # ...haptics silenced by Steam
    assert runner.pad_haptics == base_p
    assert runner.sounds >= 1  # key sound is NOT steam-gated
    state.set_steam_running(False)


def test_rumble_and_sound_flags_gate_feedback(runner):
    state.set_rumble_enabled(False)
    base = runner.haptics
    state.haptic_tick()
    assert runner.haptics == base

    state.set_rumble_enabled(True)
    state.set_key_sound_enabled(False)
    state.set_cursor(*runner.cell_rc("l"))
    runner.push(buttons=SCButtons.A)
    runner.push()
    assert runner.typed() == "l"
    assert runner.sounds == 0


@pytest.mark.parametrize(
    "w,h", [(1286, 369), (1920, 540), (900, 300)]
)
def test_screen_dims_sync_geometry(w, h):
    try:
        set_dims(w, h)
        from triton import geometry, screen

        assert geometry.width == w and geometry.height == h
        cf = geometry.CoordFraction.from_absolute(w / 2, h / 2)
        assert cf.x_fraction == pytest.approx(0.5)
        assert cf.y_fraction == pytest.approx(0.5)
        del screen
    finally:
        set_dims(1286, 369)
