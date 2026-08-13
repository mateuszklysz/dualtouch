"""Steam Controller Keyboard — system-tray launcher.

This is the bundled entry point for the portable EXE. It:
  * Runs a tray icon (right-click menu: Launch at PC start, Exit). Settings
    persist in `settings.json` next to the EXE.
  * Requires Steam: nothing runs until the Steam client is up, and the
    launcher waits for it (the keyboard layer, the watcher and the OSK all
    assume Steam Input is present — there is no standalone/no-Steam mode).
  * Watches the Steam Controller for the Steam+X chord and brings up the
    on-screen keyboard in-process (no subprocess startup cost). The app keeps
    working alongside Steam; Steam Input may still hold the controller and
    the OSK clips the cursor while open.
"""

import os
import sys

# ---- local modules (no triton imports — safe before TRITON_DATA) ------------
from applog import _bundle_dir

# IMPORTANT: TRITON_DATA must be set before importing triton.* — triton.resources
# captures its env-var search path at import time.
os.environ["TRITON_DATA"] = os.path.join(_bundle_dir(), "data")
# (SDL3 DLLs are located by sdl3w/_loader.py via sys._MEIPASS — no env var needed.)

from .app import App, main  # noqa: E402
from .helpers import _relaunch_elevated  # noqa: E402

__all__ = ["sys", "App", "main", "_relaunch_elevated"]
