"""Layer 1: inputsrc frame merging + the InputMerger facade/haptic routing
(with fake sources — SteamHidSource itself needs hidapi hardware)."""

import time

from steamcontroller import SCButtons, SCStatus, SteamControllerInput
from triton import inputsrc


def _frame(
    buttons=0,
    lpad=(0, 0),
    rpad=(0, 0),
    lstick=(0, 0),
    rstick=(0, 0),
    **kw,
):
    fields = dict(
        status=SCStatus.INPUT,
        seq=1,
        buttons=buttons,
        ltrig=0.0,
        rtrig=0.0,
        lpad_x=lpad[0],
        lpad_y=lpad[1],
        rpad_x=rpad[0],
        rpad_y=rpad[1],
        lstick_x=lstick[0],
        lstick_y=lstick[1],
        rstick_x=rstick[0],
        rstick_y=rstick[1],
        lpad_press=0.0,
        rpad_press=0.0,
    )
    fields.update(kw)
    return SteamControllerInput(**fields)


def test_frame_has_activity_gates():
    assert not inputsrc._frame_has_activity(_frame())
    assert inputsrc._frame_has_activity(_frame(buttons=SCButtons.A))
    assert inputsrc._frame_has_activity(_frame(lstick_x=8001))
    assert inputsrc._frame_has_activity(_frame(lstick_y=-8001))
    assert inputsrc._frame_has_activity(_frame(rstick_x=20000))
    # Resting drift stays quiet.
    assert not inputsrc._frame_has_activity(_frame(lstick_x=3000))


def test_frame_has_activity_counts_analog_trigger_pull():
    """A half-pull with no digital bit is activity (haptics routing)."""
    assert inputsrc._frame_has_activity(_frame(ltrig=1000))
    assert inputsrc._frame_has_activity(_frame(rtrig=16000))
    assert not inputsrc._frame_has_activity(_frame(ltrig=999))


def test_merge_inputs_or_and_max():
    a = _frame(
        buttons=SCButtons.A, ltrig=100, lpad=(10, 20), lstick=(500, 0)
    )
    b = _frame(
        buttons=SCButtons.B,
        ltrig=300,
        rtrig=50,
        lpad=(-5, -6),
        lstick=(-900, 0),
        lpad_press=7.5,
    )
    m = inputsrc.merge_inputs(a, b)
    assert m.buttons == SCButtons.A | SCButtons.B
    assert m.ltrig == 300  # max
    assert m.rtrig == 50
    assert m.lpad_press == 7.5
    assert (m.lstick_x, m.lstick_y) == (-900, 0)  # larger magnitude wins


def test_merge_inputs_touch_priority():
    # b touches the left pad while a doesn't -> b's pad coords win.
    a = _frame(lpad=(111, 222))
    b = _frame(
        buttons=SCButtons.LPADTOUCH, lpad=(-1, -2)
    )
    m = inputsrc.merge_inputs(a, b)
    assert (m.lpad_x, m.lpad_y) == (-1, -2)
    # Both touching -> a wins ("a" is the tuned SC source by convention).
    a2 = _frame(buttons=SCButtons.LPADTOUCH, lpad=(111, 222))
    m2 = inputsrc.merge_inputs(a2, b)
    assert (m2.lpad_x, m2.lpad_y) == (111, 222)


class _FakeSrc:
    def __init__(self, frames=None, fail_poll=False):
        self.frames = list(frames or [])
        self.calls: list = []
        self.fail_poll = fail_poll

    def poll(self):
        if self.fail_poll:
            raise RuntimeError("hid gone")
        return self.frames.pop(0) if self.frames else None

    def set_lizard(self, on):
        self.calls.append(("lizard", on))

    def haptic_click(self):
        self.calls.append(("click",))

    def haptic_pad_click(self):
        self.calls.append(("pad",))

    def addExit(self):
        self.calls.append(("exit",))

    def close(self):
        self.calls.append(("close",))


def test_merger_polls_merges_and_routes_haptics(monkeypatch):
    clock_t = 1000.0
    monkeypatch.setattr(
        time, "monotonic", lambda: clock_t
    )
    merger = inputsrc.InputMerger()
    quiet = _FakeSrc()
    loud = _FakeSrc(frames=[_frame(buttons=SCButtons.X)])
    merger.add(quiet)
    merger.add(loud)

    merged = merger.poll()
    assert merged is not None and merged.buttons & SCButtons.X
    # Next poll: both sources dry -> None.
    assert merger.poll() is None

    # The active source gets the tick; the quiet one does not.
    merger.haptic_click()
    merger.haptic_pad_click()
    assert ("click",) in loud.calls
    assert ("pad",) in loud.calls
    assert quiet.calls == []

    # Past the activity window the fan-out falls back to every source.
    monkeypatch.setattr(
        time, "monotonic", lambda: clock_t + 2.0
    )
    merger.haptic_click()
    assert ("click",) in quiet.calls

    # Facade fan-out reaches all sources; addExit/close included.
    merger.set_lizard(False)
    merger.addExit()
    merger.close()
    assert ("lizard", False) in quiet.calls
    assert ("exit",) in quiet.calls and ("close",) in quiet.calls
    assert ("exit",) in loud.calls


def test_merger_survives_failing_source(capsys):
    merger = inputsrc.InputMerger()
    bad = _FakeSrc(fail_poll=True)
    good = _FakeSrc(frames=[_frame(buttons=SCButtons.Y)])
    merger.add(bad)
    merger.add(good)
    merged = merger.poll()
    assert merged is not None and merged.buttons & SCButtons.Y
    assert "InputMerger" in capsys.readouterr().out
