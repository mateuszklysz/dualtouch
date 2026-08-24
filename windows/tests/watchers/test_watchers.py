"""Layer 2: tray-side watcher (_Watcher) — the OSK-closed chord listener."""

import watchers
from steamcontroller import SCButtons


class _FakeSC:
    def __init__(self):
        self.exits = 0

    def addExit(self):
        self.exits += 1


def _frame(buttons, status=66):
    from osk_sim import make_frame as _mf

    return _mf(buttons=buttons)._replace(status=status)


def test_non_input_frame_ignored():
    w = watchers._Watcher(lambda: False)
    sc = _FakeSC()
    w.on_input(sc, _frame(SCButtons.STEAM | SCButtons.X, status=0))
    assert not w.triggered and sc.exits == 0


def test_abort_releases_chord_and_exits():
    w = watchers._Watcher(lambda: True)
    sc = _FakeSC()
    w._chord.alt_held = True
    w.on_input(sc, _frame(0))
    assert not w._chord.alt_held
    assert sc.exits == 1
    assert not w.triggered


def test_steam_plus_open_chord_triggers():
    w = watchers._Watcher(lambda: False)
    sc = _FakeSC()
    w.on_input(sc, _frame(SCButtons.X))  # chord alone: nothing
    assert not w.triggered and sc.exits == 0
    w.on_input(sc, _frame(0))  # release so the next press is a rising edge
    w.on_input(sc, _frame(SCButtons.STEAM | SCButtons.X))
    assert w.triggered and sc.exits == 1


def test_qam_counts_as_steam():
    w = watchers._Watcher(lambda: False)
    sc = _FakeSC()
    w.on_input(sc, _frame(SCButtons.QAM | SCButtons.X))
    assert w.triggered


def test_custom_open_chord_respected():
    w = watchers._Watcher(lambda: False, open_chord=SCButtons.B)
    sc = _FakeSC()
    w.on_input(sc, _frame(SCButtons.STEAM | SCButtons.X))
    assert not w.triggered  # X is not this config's chord
    w.on_input(sc, _frame(0))
    w.on_input(sc, _frame(SCButtons.STEAM | SCButtons.B))
    assert w.triggered


def test_chord_held_across_frames_fires_once():
    w = watchers._Watcher(lambda: False)
    sc = _FakeSC()
    w.on_input(sc, _frame(SCButtons.STEAM | SCButtons.X))
    w.on_input(sc, _frame(SCButtons.STEAM | SCButtons.X))
    assert sc.exits == 1  # rising edge only


def test_chord_state_release_alt():
    cs = watchers._ChordState()
    cs.alt_held = True
    cs.release_alt()
    assert not cs.alt_held
    cs.alt_held = True
    cs.release_all_held()
    assert not cs.alt_held
