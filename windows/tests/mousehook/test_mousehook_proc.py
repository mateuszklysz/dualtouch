"""Layer 2: the low-level mouse hook decision function with synthetic
events — injected vs real, OSK-owned moves, controller-correlation gate."""

import time

import pytest
from triton import mousehook, state


class _FakeUser32:
    def __init__(self):
        self.next_calls = 0

    def CallNextHookEx(self, *a):
        self.next_calls += 1
        return 7  # sentinel "passed through"


@pytest.fixture(autouse=True)
def fake_hook(monkeypatch):
    fake = _FakeUser32()
    monkeypatch.setattr(mousehook, "_hook_user32", lambda: fake)
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    yield fake, clock


def _event(injected=False):
    ev = mousehook._MSLLHOOKSTRUCT()
    ev.flags = 1 if injected else 0  # LLMHF_INJECTED
    return ev


def _proc(fake, wParam, ev):
    import ctypes

    ptr = ctypes.pointer(ev)
    return mousehook._ll_mouse_proc(0, wParam, ptr)


def test_non_mouse_message_passes_through(fake_hook):
    fake, _ = fake_hook
    assert _proc(fake, 0x9999, _event()) == 7
    assert fake.next_calls == 1


def test_osk_own_move_passes_within_injection_window(fake_hook):
    fake, clock = fake_hook
    state.set_last_controller_activity(clock["t"])
    state.set_osk_mouse_inject(clock["t"] - 0.05)  # just injected
    before = dict(mousehook._hook_stats)
    assert _proc(fake, mousehook._WM_MOUSEMOVE, _event()) == 7
    # The exempt path must not count as swallowed/injected traffic.
    assert mousehook._hook_stats["swallowed"] == before["swallowed"]


def test_injected_click_is_swallowed(fake_hook):
    fake, clock = fake_hook
    state.set_last_controller_activity(clock["t"])
    state.set_osk_mouse_inject(clock["t"] - 5.0)  # injection stale
    before = dict(mousehook._hook_stats)
    assert (
        _proc(
            fake,
            mousehook._WM_MOUSEMOVE + 1,  # a non-move (click) message
            _event(injected=True),
        )
        == 1
    )
    assert mousehook._hook_stats["injected"] == before["injected"] + 1
    assert mousehook._hook_stats["swallowed"] == before["swallowed"] + 1


def test_steam_input_trailing_move_swallowed_by_correlation(fake_hook):
    """Not flagged injected, but lands right after controller activity —
    that's Steam Input's emulated mouse, not a real one."""
    fake, clock = fake_hook
    state.set_last_controller_activity(clock["t"] - 0.02)
    state.set_osk_mouse_inject(clock["t"] - 5.0)
    before = dict(mousehook._hook_stats)
    assert _proc(fake, mousehook._WM_MOUSEMOVE, _event()) == 1
    assert mousehook._hook_stats["recent"] == before["recent"] + 1


def test_real_physical_mouse_passes(fake_hook):
    fake, clock = fake_hook
    state.set_last_controller_activity(clock["t"] - 99.0)
    state.set_osk_mouse_inject(clock["t"] - 99.0)
    before = dict(mousehook._hook_stats)
    assert _proc(fake, mousehook._WM_MOUSEMOVE, _event()) == 7
    assert mousehook._hook_stats["swallowed"] == before["swallowed"]
