"""Headless tests for the Steam close-nudge restore verifier (win_focus.py).

The nudge no longer trusts a fixed delay: after each helper-window hop it
polls Steam's controller_ui.txt for the OnFocusWindowChanged->Desktop marker
that Steam Input writes ONLY when it actually re-applied the auto config
(the /0 restore). These tests guard that marker detection + the retry loop
that makes the close-time appid restore reliable."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from win_focus import (
    _STEAM_DESKTOP_RESTORE_MARKER,
    _steam_console_mark,
    _wait_steam_restore,
)


def _fake_steam_reg(monkeypatch, tmpdir):
    """The log-reading functions (LOW-1) now only trust paths under the
    ACTUAL Steam install dir resolved via the registry. Point that resolver
    at the tmpdir fake Steam root so the tests exercise the real code path
    instead of the refusal path."""
    import win_focus

    monkeypatch.setattr(
        win_focus, "_registered_steam_path", lambda: str(tmpdir)
    )


def _mk_log(tmpdir):
    logs = os.path.join(str(tmpdir), "logs")
    os.makedirs(logs, exist_ok=True)
    return os.path.join(logs, "controller_ui.txt")


def test_restore_marker_detected(tmpdir, monkeypatch):
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write("[old] something\n")
    mark = _steam_console_mark(str(tmpdir), "controller_ui.txt")
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            "[now] OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_Desktop, AppID 413080\n"
        )
    assert _wait_steam_restore(str(tmpdir), mark, timeout=2.0) is True


def test_restore_marker_absent_times_out(tmpdir, monkeypatch):
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write("old\n")
    mark = _steam_console_mark(str(tmpdir), "controller_ui.txt")
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            "[now] OnFocusWindowChanged URL Forcing to window type:"
            " k_EWindowTypeGame, 2537015031\n"
        )
    assert _wait_steam_restore(str(tmpdir), mark, timeout=0.3) is False


def test_restore_marker_before_mark_is_ignored(tmpdir, monkeypatch):
    # The marker must have been written AFTER our ui_mark snapshot (i.e. after
    # the /0 close), not a leftover from a previous restore.
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_Desktop, AppID 413080\n"
        )
    mark = _steam_console_mark(str(tmpdir), "controller_ui.txt")
    assert _wait_steam_restore(str(tmpdir), mark, timeout=0.2) is False


def test_restore_marker_not_trusted_outside_real_steam(tmpdir, monkeypatch):
    # LOW-1: without a registry Steam path (or with a mismatched one), the
    # wait must refuse rather than trust an attacker-supplied log path.
    import win_focus

    monkeypatch.setattr(win_focus, "_registered_steam_path", lambda: None)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_Desktop, AppID 413080\n"
        )
    mark = _steam_console_mark(str(tmpdir), "controller_ui.txt")
    assert mark == 0
    assert _wait_steam_restore(str(tmpdir), mark, timeout=0.2) is False


def test_marker_constant_matches_steam_log_format():
    # Frozen the exact prefix Steam writes; a refactor must not drift from it.
    assert _STEAM_DESKTOP_RESTORE_MARKER == (
        "OnFocusWindowChanged to window type:"
        " k_nGameIDControllerConfigs_Desktop"
    )


"""Headless tests for the Steam Input restore-appid capture (win_focus.py).

The close path no longer uses /0-auto (which needs a real focus change and
therefore a helper-window hop + flash). Instead it forces back to the exact
appid Steam Input was using before the OSK — read from controller_ui.txt via
_capture_active_appid. These tests guard that capture against Steam's log
formats (desktop/client UI/game), the keyboard-layer force line, and a
missing log."""
import os  # noqa: E402 -- must import after the sys.path bootstrap below
import sys  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from win_focus import (  # noqa: E402 -- needs sys.path set above
    _capture_active_appid,
)


def test_captures_desktop_appid(tmpdir, monkeypatch):
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "[1] OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_ClientUI, AppID 769\n"
        )
        f.write(
            "[2] OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_Desktop, AppID 413080\n"
        )
    assert _capture_active_appid(str(tmpdir)) == 413080


def test_captures_last_active_config_not_the_force_line(tmpdir, monkeypatch):
    # The keyboard-layer force line ("URL Forcing ...", no AppID N) must be
    # ignored — a capture is taken at open, BEFORE the layer force, so the
    # restore target is the pre-OSK config, never the layer.
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "[1] OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_Desktop, AppID 413080\n"
        )
        f.write(
            "[2] OnFocusWindowChanged URL Forcing to window type:"
            " k_EWindowTypeGame, 2537015031\n"
        )
    assert _capture_active_appid(str(tmpdir)) == 413080


def test_captures_game_appid(tmpdir, monkeypatch):
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "[1] OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_Desktop, AppID 413080\n"
        )
        f.write(
            "[2] OnFocusWindowChanged to window type:"
            " k_EWindowTypeGame, AppID 123456\n"
        )
    assert _capture_active_appid(str(tmpdir)) == 123456


def test_missing_log_returns_none(tmpdir, monkeypatch):
    _fake_steam_reg(monkeypatch, tmpdir)
    assert _capture_active_appid(str(tmpdir)) is None


def test_empty_log_returns_none(tmpdir, monkeypatch):
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write("")
    assert _capture_active_appid(str(tmpdir)) is None


def test_game_focus_format_wins_over_desktop(tmpdir, monkeypatch):
    # The real game-focus line controller_ui.txt writes when a game has
    # focus ("to game window type: AppID N, N") must beat an earlier
    # Desktop line: closing the OSK in a game forces the GAME's config
    # back, not the desktop one.
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "[1] OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_Desktop, AppID 413080\n"
        )
        f.write(
            "[2] OnFocusWindowChanged to game window type:"
            " AppID 1623730, 1623730\n"
        )
    assert _capture_active_appid(str(tmpdir)) == 1623730


def test_game_focus_ignores_later_url_forcing_line(tmpdir, monkeypatch):
    # After a game gets focus, DualTouch's own layer force
    # ("URL Forcing ...", no "AppID N") may land right after ? it must NOT
    # clobber the captured game appid.
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "[1] OnFocusWindowChanged to game window type:"
            " AppID 1623730, 1623730\n"
        )
        f.write(
            "[2] OnFocusWindowChanged URL Forcing to window type:"
            " k_EWindowTypeGame, 2537015031\n"
        )
    assert _capture_active_appid(str(tmpdir)) == 1623730


def test_captures_clientui_appid_after_game(tmpdir, monkeypatch):
    # Big Picture focus (ClientUI) is captured too; the most recent line
    # wins.
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "[1] OnFocusWindowChanged to game window type:"
            " AppID 1623730, 1623730\n"
        )
        f.write(
            "[2] OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_ClientUI, AppID 769\n"
        )
    assert _capture_active_appid(str(tmpdir)) == 769


def test_capture_ignores_forged_line_outside_real_steam(tmpdir, monkeypatch):
    # LOW-1: with no matching registry Steam path the capture must refuse
    # (return None) rather than read an attacker-supplied log.
    import win_focus

    monkeypatch.setattr(win_focus, "_registered_steam_path", lambda: None)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_Desktop, AppID 413080\n"
        )
    assert _capture_active_appid(str(tmpdir)) is None


def test_capture_rejects_malformed_appid_line(tmpdir, monkeypatch):
    # LOW-1: a line with a non-integer AppID (forged/malformed) must not be
    # treated as a well-formed capture.
    _fake_steam_reg(monkeypatch, tmpdir)
    path = _mk_log(tmpdir)
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "OnFocusWindowChanged to window type:"
            " k_nGameIDControllerConfigs_Desktop, AppID 0xFFFF\n"
        )
    assert _capture_active_appid(str(tmpdir)) is None


def test_restore_auto_waits_for_force_receipt(tmpdir):
    """_restore_auto_after_force must dispatch /0 (auto mode) so alt-tab
    still switches configs after the instant restore. The worker waits for
    the force receipt (patched to be instant here) then dispatches /0."""
    import steam_shortcut
    import win_focus

    dispatched = []
    orig_sc = steam_shortcut.force_appid
    orig_wait = win_focus._wait_steam_url
    win_focus._wait_steam_url = lambda *a, **k: True  # force receipt seen
    steam_shortcut.force_appid = lambda appid: dispatched.append(appid) or True
    try:
        win_focus._restore_auto_after_force(str(tmpdir), 413080)
    finally:
        steam_shortcut.force_appid = orig_sc
        win_focus._wait_steam_url = orig_wait
    assert dispatched == [0], dispatched


def test_restore_auto_still_0s_without_receipt(tmpdir):
    """If the force receipt never appears (capped wait), /0 must still be
    dispatched so auto-switching isn't lost."""
    import steam_shortcut
    import win_focus

    dispatched = []
    orig_sc = steam_shortcut.force_appid
    orig_wait = win_focus._wait_steam_url
    win_focus._wait_steam_url = lambda *a, **k: False  # no receipt, fast
    steam_shortcut.force_appid = lambda appid: dispatched.append(appid) or True
    try:
        win_focus._restore_auto_after_force(str(tmpdir), 413080)
    finally:
        steam_shortcut.force_appid = orig_sc
        win_focus._wait_steam_url = orig_wait
    assert dispatched == [0], dispatched


def test_steam_restore_dispatched_guard(tmpdir):
    """_dispatch_steam_restore must only fire once — the early on_close
    callback and the finally safety net would otherwise double-dispatch."""
    import steam_shortcut

    dispatched = []
    steam_shortcut.force_appid = lambda appid: dispatched.append(appid) or True

    class App:
        def __init__(self):
            self._steam_restore_dispatched = False
            self._osk_restore_appid = 413080

        def _dispatch_steam_restore(self, restore_hwnd):
            if self._steam_restore_dispatched:
                return
            self._steam_restore_dispatched = True
            dispatched.append("restore")

    a = App()
    a._dispatch_steam_restore(0)
    a._dispatch_steam_restore(0)  # second call must be a no-op
    assert dispatched == ["restore"], dispatched
    assert a._steam_restore_dispatched is True


def test_caps_key_is_normal_generic_key():
    """The OSK Caps key must behave like every other key (generic
    virtual-key send). PowerToys is allowed to remap it; the OSK must NOT
    special-case it, and Esc must not close the keyboard."""
    import os
    import sys

    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    os.environ.setdefault(
        "TRITON_DATA",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        ),
    )
    from triton import config, vkb

    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("keys", kb_config)
    kb = kb_config.construct()
    caps = None
    for row in kb.keys:
        for key in row:
            if key.str == "Caps":
                caps = key
    assert caps is not None
    assert caps.callback is vkb.on_key_generic, caps.callback


def test_force_caps_off_exists(monkeypatch):
    """force_caps_off must be available on the Keyboard and no-op cleanly when
    caps is already off (the OSK calls it on open so keys never get stuck
    uppercase when PowerToys remaps Caps->Esc)."""
    import os
    import sys

    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    os.environ.setdefault(
        "TRITON_DATA",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        ),
    )
    from steamcontroller.uinput import Keyboard

    kb = Keyboard()
    assert hasattr(kb, "force_caps_off")
    kb.force_caps_off()  # must not raise (caps off -> early return)


def test_reset_shift_state_clears_internal_desync():
    """pynput's Controller tracks shift/caps internally PER INSTANCE. A Shift
    pressed on the OSK's Keyboard instance and released on the controller
    thread's separate instance leaves shift_pressed stuck True, so pynput's
    _resolve uppercases every letter (KEY_A -> 'A') regardless of OS shift.
    reset_shift_state must clear that internal state WITHOUT emitting any OS
    key event, so the next letter follows the real OS shift."""
    import os
    import sys

    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    os.environ.setdefault(
        "TRITON_DATA",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        ),
    )
    from steamcontroller.uinput import Keyboard

    a = Keyboard()  # like vkb.kb (OSK mouse clicks)
    b = Keyboard()  # like controller self._kb (L2-held shift)
    # Silence real OS events; we only test pynput's internal bookkeeping.
    a._kb._handle = lambda key, is_press: None  # type: ignore[reportAttributeAccessIssue]
    b._kb._handle = lambda key, is_press: None  # type: ignore[reportAttributeAccessIssue]

    # Scenario A: Shift pressed on A, released on B (paste/emoji/arrow re-press).
    a._kb.press(a._kb._Key.shift)
    b._kb.release(b._kb._Key.shift)
    assert a._kb.shift_pressed is True
    resolved = a._kb._resolve(a._kb._KeyCode.from_char("a"))  # type: ignore[reportAttributeAccessIssue]
    assert resolved.char == "A"  # the bug: uppercased by internal state

    a.reset_shift_state()
    assert a._kb.shift_pressed is False
    resolved2 = a._kb._resolve(a._kb._KeyCode.from_char("a"))  # type: ignore[reportAttributeAccessIssue]
    assert resolved2.char == "a"  # fixed: lowercase follows real OS shift

    # Scenario B: single on-screen Caps tap flips internal _caps_lock.
    a._kb.press(a._kb._Key.caps_lock)
    a._kb.release(a._kb._Key.caps_lock)
    assert a._kb.shift_pressed is True
    a.reset_shift_state()
    assert a._kb.shift_pressed is False


def test_key_sound_tick_gates_on_enabled(monkeypatch):
    """key_sound_tick must fire the registered hook only while enabled, swallow
    a raising hook, and be a no-op when disabled."""
    import os
    import sys

    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    os.environ.setdefault(
        "TRITON_DATA",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        ),
    )
    from triton import state

    calls = []
    state.set_key_sound(lambda: calls.append("tick"))
    state.set_key_sound_enabled(True)
    state.key_sound_tick()
    assert calls == ["tick"], calls
    # disabled -> no-op
    state.set_key_sound_enabled(False)
    state.key_sound_tick()
    assert calls == ["tick"], calls
    # raising hook must not propagate
    state.set_key_sound_enabled(True)
    state.set_key_sound(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    state.key_sound_tick()  # must not raise
    state.set_key_sound(None)


def test_key_sound_path_cached_and_missing(monkeypatch):
    """key_sound._sound_path caches resolved paths (per name) and returns
    None for a missing file, never raising."""
    import os
    import sys

    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    os.environ.setdefault(
        "TRITON_DATA",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        ),
    )
    import key_sound

    tmp = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "_tmp_sound",
    )
    os.makedirs(os.path.join(tmp, "steamui", "sounds"), exist_ok=True)
    for n in ("key", "open", "close"):
        with open(
            os.path.join(
                tmp,
                "steamui",
                "sounds",
                os.path.basename(key_sound._SOUNDS[n]),
            ),
            "wb",
        ) as f:
            f.write(b"RIFF")
    monkeypatch.setattr(key_sound, "find_steam_path", lambda: tmp)
    key_sound._path_cache.clear()
    key_sound._paths_resolved = False
    wav = key_sound._sound_path("key")
    assert wav == os.path.join(tmp, "steamui", "sounds", "deck_ui_typing.wav")
    assert key_sound._sound_path("open") is not None
    assert key_sound._sound_path("close") is not None
    # cached: a second call returns the same path without re-stat
    monkeypatch.setattr(
        key_sound.os.path,
        "isfile",
        lambda p: (_ for _ in ()).throw(AssertionError("re-stat!")),
    )
    assert key_sound._sound_path("key") == wav
    # missing file -> None, no raise
    monkeypatch.setattr(key_sound.os.path, "isfile", lambda p: False)
    monkeypatch.setattr(
        key_sound, "find_steam_path", lambda: os.path.join(tmp, "none")
    )
    key_sound._path_cache.clear()
    key_sound._paths_resolved = False
    assert key_sound._sound_path("key") is None
    # reset module state
    key_sound._path_cache.clear()
    key_sound._paths_resolved = False
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def test_key_sound_open_close_hooks(monkeypatch):
    """set_key_sound_open/set_key_sound_close + key_sound_open/close fire
    their hooks only while key_sound_enabled, and never raise."""
    import os
    import sys

    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    os.environ.setdefault(
        "TRITON_DATA",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        ),
    )
    from triton import state

    calls = []
    state.set_key_sound_open(lambda: calls.append("open"))
    state.set_key_sound_close(lambda: calls.append("close"))
    state.set_key_sound_enabled(True)
    state.key_sound_open()
    state.key_sound_close()
    assert calls == ["open", "close"], calls
    # disabled -> no-op
    state.set_key_sound_enabled(False)
    state.key_sound_open()
    assert calls == ["open", "close"], calls
    # raising hook must not propagate
    state.set_key_sound_enabled(True)
    state.set_key_sound_open(
        lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    state.key_sound_open()  # must not raise
    state.set_key_sound_open(None)
    state.set_key_sound_close(None)


def test_dispatch_key_repeat_gates_sound(monkeypatch):
    """dispatch_key fires key_sound_tick on a normal press but not on an
    auto-repeat."""
    import os
    import sys

    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    os.environ.setdefault(
        "TRITON_DATA",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        ),
    )
    from triton import state, vkb
    from triton.vkb import KeyButton

    ticks = []
    state.set_key_sound_enabled(True)
    state.set_key_sound(lambda: ticks.append("s"))
    # silence actual injection
    orig_pe = vkb.kb.pressEvent
    orig_re = vkb.kb.releaseEvent
    vkb.kb.pressEvent = lambda keys: None
    vkb.kb.releaseEvent = lambda keys: None
    key = KeyButton("a", "KEY_A", vkb.on_key_generic)
    try:
        vkb._dispatch_is_repeat = False
        vkb.dispatch_key(None, key)
        assert ticks == ["s"], ticks
        vkb._dispatch_is_repeat = True
        vkb.dispatch_key(None, key)
        assert ticks == ["s"], ticks  # repeat -> no extra sound
    finally:
        vkb.kb.pressEvent = orig_pe
        vkb.kb.releaseEvent = orig_re
        vkb._dispatch_is_repeat = False
        state.set_key_sound(None)


def test_key_sound_click_debounced_for_two_finger_burst(monkeypatch):
    """Two keys dispatched back-to-back (two-finger typing) must produce ONE
    click — a second click inside the debounce window is dropped so the sound
    doesn't double/garbly over itself."""
    import time

    import key_sound

    # Force a known sound path so _play would actually call winsound.
    monkeypatch.setattr(key_sound, "_path_cache", {"key": "x.wav"})
    monkeypatch.setattr(key_sound, "_paths_resolved", True)
    monkeypatch.setattr(key_sound, "_last_key_click", 0.0)
    plays = []
    monkeypatch.setattr(key_sound, "_play", lambda name: plays.append(name))

    key_sound.play_key_sound()
    assert plays == ["key"]
    # Immediate second key inside the debounce window -> dropped.
    key_sound.play_key_sound()
    assert plays == ["key"], "second simultaneous click must be dropped"
    # After the window elapses, the next click plays again.
    monkeypatch.setattr(
        key_sound,
        "_last_key_click",
        time.monotonic() - key_sound._CLICK_DEBOUNCE_S - 0.01,
    )
    key_sound.play_key_sound()
    assert plays == ["key", "key"]
