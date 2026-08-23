"""Layer 2: SteamHidSource lifecycle against a FAKE SteamController driver
(patched into inputsrc) — attach/poll/stale/facade-forwarding/teardown."""

import time

import pytest
from sc_runner import make_frame
from triton import inputsrc, state


class FakeSC:
    instances: list = []

    def __init__(self, callback=None, restore_lizard_on_close=True):
        self.cb = callback
        self.restore = restore_lizard_on_close
        self.calls: list = []
        self.exited = False
        FakeSC.instances.append(self)

    def run(self):
        self.calls.append("run")
        while not self.exited and not state.should_close():
            time.sleep(0.001)
        if self.restore and not state.is_steam_running():
            self.calls.append("restore-lizard")

    def set_lizard(self, on):
        self.calls.append(("lizard", bool(on)))

    def haptic_click(self):
        self.calls.append("hclick")

    def haptic_pad_click(self):
        self.calls.append("hpad")

    def addExit(self):
        self.exited = True


@pytest.fixture(autouse=True)
def patch_driver(monkeypatch):
    FakeSC.instances = []
    monkeypatch.setattr(inputsrc, "SteamController", FakeSC)
    yield


def _wait_live(src, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if src._live() is not None:
            return src._live()
        time.sleep(0.002)
    raise AssertionError("driver never attached")


def test_poll_none_when_stale_or_absent(monkeypatch):
    t = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: t)
    src = inputsrc.SteamHidSource()
    sc = _wait_live(src)
    # No frame delivered yet (latest_t=0 -> stale) -> poll returns None.
    assert src.poll() is None
    sc.cb(None, make_frame(buttons=1))
    assert src.poll() is not None  # fresh frame flows through
    # Older than STALE_AFTER -> released device reads as absent.
    t += inputsrc.SteamHidSource.STALE_AFTER + 0.1
    assert src.poll() is None
    src.close()


def test_facade_forwards_to_live_device(monkeypatch):
    t = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: t)
    src = inputsrc.SteamHidSource()
    sc = _wait_live(src)
    src.set_lizard(False)
    src.haptic_click()
    src.haptic_pad_click()
    assert ("lizard", False) in sc.calls
    assert "hclick" in sc.calls and "hpad" in sc.calls
    src.close()
    assert sc.exited


def test_close_restores_lizard_on_steam_absence(monkeypatch):
    state.set_steam_running(False)
    t = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: t)
    src = inputsrc.SteamHidSource()
    sc = _wait_live(src)
    src.close()
    assert "restore-lizard" in sc.calls


def test_close_skips_lizard_restore_while_steam_runs(monkeypatch):
    """Steam Input owns lizard mode then; restoring would cause the ~1 s
    'lizard blip' after closing the OSK."""
    state.set_steam_running(True)
    try:
        t = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: t)
        src = inputsrc.SteamHidSource()
        sc = _wait_live(src)
        src.close()
        assert "restore-lizard" not in sc.calls
    finally:
        state.set_steam_running(False)


def test_reconnect_after_device_drop(monkeypatch):
    t = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: t)
    src = inputsrc.SteamHidSource()
    first = _wait_live(src)
    n0 = len(FakeSC.instances)
    first.addExit()  # simulate the device dropping
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        _wait_live(src)
        live = src._live()
        if live is not first:
            break
        time.sleep(0.01)
    assert len(FakeSC.instances) > n0  # a fresh driver instance appeared
    src.close()
