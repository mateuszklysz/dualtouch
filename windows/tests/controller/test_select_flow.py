"""Layer 2: the Select-key text-selection session at SceneRunner level —
enter via pad click, drag arrows, freeze on finger-lift, roll-back
cancel on release, and clean teardown."""

from steamcontroller import SCButtons
from triton import state


def _select_pos(runner):
    return runner.raw_at_cell("Select")


def _arrows(runner, name):
    return [ev for ev in runner.events() if ev[0] == "down" and ev[1] == name]


def test_click_on_select_enters_select_mode(runner):
    pos = _select_pos(runner)
    runner.push(buttons=SCButtons.LB | SCButtons.LPADTOUCH, **pos)
    assert state.is_select_active()
    downs = [ev[1] for ev in runner.events() if ev[0] == "down"]
    assert "KEY_LEFTSHIFT" in downs  # OS shift held for the session
    runner.push(buttons=0, **pos)
    assert not state.is_select_active()


def test_select_drag_fires_arrows_1to1(runner):
    pos = _select_pos(runner)
    x = pos["lpad"][0]
    runner.push(buttons=SCButtons.LB | SCButtons.LPADTOUCH, **pos)
    # Drag right one SELECT_DRAG_STEP (0x1000) in 2 k frames.
    for i in range(1, 5):
        kw = {"lpad": (x + i * 0x400, 0)}
        runner.push(buttons=SCButtons.LB | SCButtons.LPADTOUCH, **kw)
    rights = len(_arrows(runner, "KEY_RIGHT"))
    assert rights == 1  # 0x1000 of travel exactly
    for i in range(5, 9):  # another full step
        kw = {"lpad": (x + i * 0x400, 0)}
        runner.push(buttons=SCButtons.LB | SCButtons.LPADTOUCH, **kw)
    assert len(_arrows(runner, "KEY_RIGHT")) == 2


def test_select_freeze_when_finger_lifts(runner):
    """Click button held but finger off the pad: selection freezes."""
    pos = _select_pos(runner)
    runner.push(buttons=SCButtons.LB | SCButtons.LPADTOUCH, **pos)
    before = len(_arrows(runner, "KEY_RIGHT"))
    # Same raw coords but real_touch now False (no LPADTOUCH synthesized:
    # with LB held TOUCH is synthesized only from the click button...).
    runner.push(buttons=SCButtons.LB, lpad=pos["lpad"])
    runner.push(
        buttons=SCButtons.LB,
        lpad=(pos["lpad"][0] + 0x800, 0),
    )
    assert len(_arrows(runner, "KEY_RIGHT")) == before


def test_select_reverse_inside_window_cancelled_on_release(runner):
    """A reverse arrow fired just before lift-off is the natural roll-back
    and gets cancelled with one compensating arrow."""

    pos = _select_pos(runner)
    x0 = pos["lpad"][0]
    runner.push(buttons=SCButtons.LB | SCButtons.LPADTOUCH, **pos)
    # Forward two steps slowly (deliberate travel).
    for i in range(1, 9):
        kw = {"lpad": (x0 + i * 0x400, 0)}
        runner.push(buttons=SCButtons.LB | SCButtons.LPADTOUCH, **kw)
        runner.clock.t += 0.03
    fwd = len(_arrows(runner, "KEY_RIGHT"))
    assert fwd >= 2
    # Quick reverse of EXACTLY one step within the 0.12 s roll-back
    # window (the anchor sits at x0 + 0x2000 after forward travel).
    runner.push(
        buttons=SCButtons.LB | SCButtons.LPADTOUCH,
        lpad=(x0 + 0x1000, 0),
    )
    revs_before = len(_arrows(runner, "KEY_LEFT"))
    assert revs_before == 1  # fired immediately...
    runner.push(buttons=0, lpad=(x0 + 0x1000, 0))  # lift right away
    lefts_after = len(_arrows(runner, "KEY_LEFT"))
    rights_after = len(_arrows(runner, "KEY_RIGHT"))
    # The compensating RIGHT neutralises the accidental reverse: net
    # selection travel equals the deliberate forward drag again.
    assert rights_after - lefts_after == fwd
    assert lefts_after == revs_before
    assert not state.is_select_active()
