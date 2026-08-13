"""Headless tests for triton/windowpos.py per-app position helpers.

Pure logic (no SDL window / display): _position_for_app/_save_position_for_app
only read the {exe name: index} map and the global _position_index fallback.
"""

from triton import windowpos


def _reset_global():
    windowpos._position_index[0] = 0


def test_position_for_app_falls_back_to_global():
    _reset_global()
    assert windowpos._position_for_app("notepad.exe", {}) == 0
    # None exe (non-positionable foreground) always falls back to the global.
    windowpos._position_index[0] = 4
    assert windowpos._position_for_app(None, {"notepad.exe": 2}) == 4
    assert windowpos._position_for_app("notepad.exe", {}) == 4


def test_position_for_app_uses_stored_index_and_clamps():
    _reset_global()
    per_app = {"notepad.exe": 3, "wordpad.exe": 5}
    assert windowpos._position_for_app("notepad.exe", per_app) == 3
    assert windowpos._position_for_app("wordpad.exe", per_app) == 5
    assert windowpos._position_for_app("chrome.exe", {"chrome.exe": 9}) == 3
    assert windowpos._position_for_app("chrome.exe", {"chrome.exe": -2}) == 4


def test_save_position_for_app_records_clamps_and_never_mutates():
    _reset_global()
    per_app = {"notepad.exe": 1}
    out = windowpos._save_position_for_app("notepad.exe", 4, per_app)
    assert out == {"notepad.exe": 4}
    # The input map is left untouched (pure function).
    assert per_app == {"notepad.exe": 1}
    out = windowpos._save_position_for_app("wordpad.exe", 7, out)
    assert out["wordpad.exe"] == 1
    # None exe name never persists - returns the same map object.
    assert windowpos._save_position_for_app(None, 2, out) is out


def test_position_persist_updates_state_immediately_and_flushes_at_close():
    """The debounced settings write: _persist_position_for_app must update the
    in-memory per-app map instantly (a re-open within the session sees it),
    mark the disk write dirty, and only touch settings.json when the debounce
    interval elapses or _flush_position_persist runs (the OSK-close flush).
    Monkeypatched: no real foreground lookup or settings.json I/O."""
    import appsettings as appset
    import triton.state as state
    import triton.triton as T

    state.reset_session()
    state.set_window_position_per_app({})

    import time

    T._foreground_exe_name = lambda: "notepad.exe"
    T._position_persist_dirty = False
    T._position_persist_at = time.monotonic()  # last flush "just now"
    saved = []
    appset._load_settings = lambda: {}
    appset._save_settings = lambda s: saved.append(dict(s))

    # First persist: state updates NOW, disk write deferred (within the
    # debounce interval), dirty set.
    T._persist_position_for_app(2)
    assert state.get_window_position_per_app() == {"notepad.exe": 2}
    assert T._position_persist_dirty is True
    assert saved == [], "disk write must be debounced, not immediate"

    # A later persist for the same app+index is a no-op (no dirty re-set).
    T._persist_position_for_app(2)
    assert T._position_persist_dirty is True  # still dirty from before
    assert saved == []

    # Moving to a new index re-arms; the flush (OSK-close path) writes it once.
    T._persist_position_for_app(4)
    assert state.get_window_position_per_app() == {"notepad.exe": 4}
    T._flush_position_persist()
    assert T._position_persist_dirty is False
    assert saved and saved[-1]["window_position_per_app"] == {"notepad.exe": 4}

    # A subsequent flush with nothing dirty writes nothing.
    before = len(saved)
    T._flush_position_persist()
    assert len(saved) == before

    # None foreground never persists.
    T._foreground_exe_name = lambda: None
    T._position_persist_dirty = False
    T._persist_position_for_app(3)
    assert T._position_persist_dirty is False
    assert state.get_window_position_per_app() == {"notepad.exe": 4}
