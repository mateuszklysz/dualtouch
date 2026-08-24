"""Layer 2: chord matrix (Steam+L3 / Steam+VIEW / X-under-Steam /
position-cycle buttons), media stick roles and right-stick mouse move."""

from steamcontroller import SCButtons
from triton import state


def test_x_is_suppressed_while_steam_held(runner):
    """Steam+X is the open chord: X must NOT delete while Steam is down."""
    state.set_cursor(0, 13)
    runner.push(buttons=SCButtons.STEAM | SCButtons.X)
    for _ in range(6):
        runner.advance(0.05)
        runner.push(buttons=SCButtons.STEAM | SCButtons.X)
    runner.push(buttons=0)
    names = [ev[1] for ev in runner.events() if ev[0] == "down"]
    assert "KEY_BACKSPACE" not in names


def test_steam_l3_is_playpause_and_marks_chord_used(runner):
    runner.push(buttons=SCButtons.STEAM | SCButtons.L3)
    downs = [ev[1] for ev in runner.events() if ev[0] == "down"]
    assert "KEY_PLAYPAUSE" in downs
    assert "KEY_CAPSLOCK" not in downs  # Steam reroutes L3 away from caps
    for _ in range(24):
        runner.advance(0.05)
        runner.push(buttons=SCButtons.STEAM)
    runner.push()
    assert not state.should_close()  # the chord marked Steam "used"


def test_steam_view_is_alt_tab_chord(runner):
    runner.push(buttons=SCButtons.STEAM | SCButtons.VIEW)
    events = [(ev[0], ev[1]) for ev in runner.events()]
    assert ("down", "KEY_LEFTALT") in events
    assert ("down", "KEY_TAB") in events
    assert ("up", "KEY_TAB") in events
    assert ("up", "KEY_LEFTALT") not in events  # Alt held for the hold
    runner.push(buttons=SCButtons.STEAM)  # release VIEW, keep Steam...
    runner.push(buttons=0)  # ...then drop Alt with Steam
    events = [(ev[0], ev[1]) for ev in runner.events()]
    assert ("up", "KEY_LEFTALT") in events


def test_view_alone_requests_position_cycle(runner):
    runner.tap_button(SCButtons.VIEW)
    assert state.take_position_cycle_request()
    assert not state.take_position_cycle_request()  # one shot per tap


def test_start_alone_requests_position_cycle(runner):
    runner.tap_button(SCButtons.START)
    assert state.take_position_cycle_request()


def test_media_track_skip_fires_once_per_deflection(runner):
    btn = SCButtons.STEAM

    def count_song_events():
        return sum(
            1
            for ev in runner.events()
            if ev[1] in ("KEY_PREVIOUSSONG", "KEY_NEXTSONG")
        )

    runner.push(lstick=(-20000, 0), buttons=btn)  # LEFT zone edge
    for _ in range(20):
        runner.advance(0.03)
        runner.push(lstick=(-20000, 0), buttons=btn)
    first = count_song_events()
    assert first >= 1
    for _ in range(20):  # held: track skip never repeats
        runner.advance(0.03)
        runner.push(lstick=(-20000, 0), buttons=btn)
    assert count_song_events() == first


def test_media_volume_ramps_while_held(runner):
    btn = SCButtons.STEAM
    runner.auto_advance = False
    runner.advance(0.02)
    runner.push(lstick=(0, -20000), buttons=btn)  # edge: one step

    def downs():
        return sum(
            1
            for ev in runner.events()
            if ev[0] == "down" and ev[1] == "KEY_VOLUMEDOWN"
        )

    base = downs()
    assert base == 1
    for _ in range(
        22
    ):  # ~0.66 s held: past STICK_HOLD_DELAY (0.5 s), ramp is 21 ms
        runner.advance(0.03)
        runner.push(lstick=(0, -20000), buttons=btn)
    assert downs() > base


def test_right_stick_moves_system_mouse(runner):
    import osk_sim

    for _ in range(12):
        runner.push(rstick=(20000, 0))
    moves = [e for e in osk_sim.RecordingMouse.EVENTS if e[0] == "move"]
    dxs = [e[1] for e in moves]
    assert dxs and all(d > 0 for d in dxs)  # rightward drift accumulates


def test_right_stick_deadzone_does_not_move_mouse(runner):
    import osk_sim

    for _ in range(12):
        runner.push(rstick=(3000, -3000))  # inside deadzone
    assert not [e for e in osk_sim.RecordingMouse.EVENTS if e[0] == "move"]


def test_right_stick_y_axis_inverts(runner):
    import osk_sim

    for _ in range(12):
        runner.push(rstick=(0, 20000))  # stick up
    dys = [e[2] for e in osk_sim.RecordingMouse.EVENTS if e[0] == "move"]
    assert dys and all(d < 0 for d in dys)  # screen y shrinks
