"""Steam keyboard sounds: play Steam's own on-screen-keyboard audio.

Replicates the Steam OSK's audio by playing these files from the Steam
install (the exact files Steam's web keyboard / UI loads from the steamui
web root):
  - key press   -> steamui/sounds/deck_ui_typing.wav       (PN.Typing)
  - OSK open    -> steamui/sounds/deck_ui_side_menu_fly_in.wav  (PN.OpenSideMenu)
  - OSK close   -> steamui/sounds/deck_ui_side_menu_fly_out.wav (PN.CloseSideMenu)
Paths are located at runtime via steam_shortcut.find_steam_path() — the
same Steam-path lookup the skin system uses — so it works on any machine
with Steam and degrades to silence when a file isn't there.

Each path is resolved once and cached, keeping the per-key hot path cheap:
the playback hook only stats the file on the very first press.
"""

import os
import time
import winsound
from contextlib import suppress

from steam_shortcut import find_steam_path

# Sound names -> relative path under the Steam install root.
_SOUNDS = {
    "key": os.path.join("steamui", "sounds", "deck_ui_typing.wav"),
    "open": os.path.join("steamui", "sounds", "deck_ui_side_menu_fly_in.wav"),
    "close": os.path.join(
        "steamui", "sounds", "deck_ui_side_menu_fly_out.wav"
    ),
}

_path_cache = {}  # name -> absolute path or None (missing)
_paths_resolved = False

# Two-finger typing dispatches two keys back-to-back (one per finger); playing
# a second click over the first mid-sound doubles/garbles it. Debounce the
# per-key click so a burst of simultaneous keys produces ONE clean tick.
_CLICK_DEBOUNCE_S = 0.025
_last_key_click = 0.0


def _sound_path(name="key"):
    """Absolute path to a Steam keyboard sound, or None. Resolved once per
    name and cached (the per-key hot path must not re-stat every press)."""
    global _paths_resolved
    if name in _path_cache:
        return _path_cache[name]
    if not _paths_resolved:
        _paths_resolved = True
        try:
            steam = find_steam_path()
            for n, rel in _SOUNDS.items():
                cand = os.path.join(steam, rel) if steam else None
                _path_cache[n] = (
                    cand if cand and os.path.isfile(cand) else None
                )
        except Exception:
            pass
    return _path_cache.get(name)


def _play(name):
    p = _sound_path(name)
    if not p:
        return
    with suppress(Exception):
        winsound.PlaySound(
            p,
            winsound.SND_FILENAME
            | winsound.SND_ASYNC
            | winsound.SND_NODEFAULT,
        )


def play_key_sound():
    """Steam keyboard click (async, non-blocking). No-op if missing.
    SND_NODEFAULT avoids the system beep; SND_ASYNC restarts on each press
    so rapid typing keeps a crisp per-key tick without blocking the loop.
    Debounced: a burst of simultaneous keys (two-finger typing) collapses to
    one tick instead of two overlapping clicks."""
    global _last_key_click
    now = time.monotonic()
    if now - _last_key_click < _CLICK_DEBOUNCE_S:
        return
    _last_key_click = now
    _play("key")


def play_open_sound():
    """Steam keyboard-open (side-menu fly-in) sound."""
    _play("open")


def play_close_sound():
    """Steam keyboard-close (side-menu fly-out) sound."""
    _play("close")
