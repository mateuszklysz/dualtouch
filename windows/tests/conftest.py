"""Pytest bootstrap for DualTouch headless tests.

Everything here must run WITHOUT SDL, Steam, or a controller attached:
the point of this suite is fast, deterministic logic verification before
the user does live testing on the real hardware.

TRITON_DATA must be set before any `triton.*` import (triton/resources.py
captures it at import time), pointing at the repo's windows/data dir —
the same contract tray.py and lockscreen_osk.py rely on.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # windows/
os.environ.setdefault("TRITON_DATA", os.path.join(_ROOT, "data"))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# Tests live in per-module subdirs (tests/<module>/...), so pytest's
# per-file sys.path insertion no longer exposes tests/ itself — add it
# explicitly for the shared harness lib (osk_sim).
_TESTS = os.path.dirname(os.path.abspath(__file__))
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

# Headless tests must never write into the REAL %APPDATA%\DualTouch\dualtouch.log
# (the diagnostic log the user reads after live tests). The injection/pad
# diagnostics (uinput._diag, pad._PadMixin._diag) are gated by the tray
# logging toggle; force it off for the whole test session.
import applog  # noqa: E402 -- must import after the sys.path bootstrap above

applog.set_logging_enabled(False)


# --- OskSim harness fixtures (tests/osk_sim.py) ---------------

import time  # noqa: E402
from collections.abc import Iterator  # noqa: E402

import osk_sim  # noqa: E402
import pytest  # noqa: E402
import steamcontroller.uinput as sui  # noqa: E402
from steamcontroller import events as sc_events  # noqa: E402
from triton import vkb as _vkb  # noqa: E402


@pytest.fixture
def runner(monkeypatch) -> Iterator[osk_sim.OskSim]:
    """A headless OSK session with every OS-injection surface recorded.

    Patches, in order, BEFORE the ControllerManager is built:
      • time.monotonic -> VirtualClock (hold-to-repeat cadence is exact);
      • pynput Keyboard/Mouse -> recorders (vkb.kb, manager._kb/_mouse and
        EventMapper's own keyboard all become recording instances sharing
        one event stream).
    """
    clock = osk_sim.VirtualClock()
    monkeypatch.setattr(time, "monotonic", clock)
    monkeypatch.setattr(sui, "Keyboard", osk_sim.RecordingKeyboard)
    monkeypatch.setattr(sui, "Mouse", osk_sim.RecordingMouse)
    monkeypatch.setattr(sc_events, "Keyboard", osk_sim.RecordingKeyboard)
    monkeypatch.setattr(_vkb, "kb", osk_sim.RecordingKeyboard())
    yield osk_sim.OskSim(clock)
    state_cleanup()


def state_cleanup():
    """Drop hooks/flags a test may have flipped so later tests start clean."""
    from triton import state

    state.set_key_sound(None)
    state.set_haptic_tick(None)
    state.set_pad_click_haptic(None)
    state.set_key_sound_enabled(False)
    state.reset_session()
