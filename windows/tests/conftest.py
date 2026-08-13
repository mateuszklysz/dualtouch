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

# Headless tests must never write into the REAL %APPDATA%\DualTouch\dualtouch.log
# (the diagnostic log the user reads after live tests). The injection/pad
# diagnostics (uinput._diag, pad._PadMixin._diag) are gated by the tray
# logging toggle; force it off for the whole test session.
import applog  # noqa: E402 -- must import after the sys.path bootstrap above

applog.set_logging_enabled(False)
