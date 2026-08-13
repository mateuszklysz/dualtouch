"""Win32 window-focus helpers.

* The Steam Input focus nudge: after the OSK closes and
  steam://forceinputappid/0 is dispatched, Steam only re-evaluates the
  active app on a real window-activation change. A hidden helper window
  takes the foreground, then hands it back to the app (the manual alt-tab
  equivalent), after Steam's console log confirms it processed the URL.
* Lock-screen guard: detects the secure desktop owning input (lock screen,
  UAC, Ctrl+Alt+Del) so the OSK never opens behind it.
* The "window the user was typing in" sampler for the OSK's focus restore.
"""

import ctypes
import os
import time
from ctypes import wintypes

from applog import _log

# Synthetic WinEvent foreground/focus signaling for the close nudge (see
# _synthetic_foreground_signal). EVENT_SYSTEM_FOREGROUND is the standard event
# apps register for via SetWinEventHook to notice a foreground change; firing
# it manually AT an already-foreground window makes a WinEvent-based watcher
# re-read the foreground without any window ever deactivating.
_EVENT_SYSTEM_FOREGROUND = 0x0003
_EVENT_OBJECT_FOCUS = 0x8005
_OBJID_WINDOW = 0
_CHILDID_SELF = 0


# --- Steam keyboard-layer close: focus nudge --------------------------------
#
# On OSK close we dispatch steam://forceinputappid/0 to restore auto mode.
# Evidence (controller_ui.txt, 2026-08-09): the /0 IS processed by Steam
# ("ExecuteSteamURL" in console_log.txt) but Steam Input does NOT re-evaluate
# the active app — no "OnFocusWindowChanged", no config reload — so the
# controller keeps the keyboard-layer config (game-mode defaults) and Big
# Picture stays dead until the user alt-tabs. Steam only re-evaluates on a
# real window-activation change. Alt-tab is exactly that: the task-switcher
# takes the foreground, then the app re-activates. We synthesize the same hop
# with a hidden helper window: helper takes the foreground, we hand it back to
# the app. The /0 URL also reaches Steam asynchronously (~1-2s through the
# explorer.exe shell hop, up to ~14s observed), so the nudge runs on a daemon
# thread that first waits for Steam's console log to confirm the URL, then
# nudges.

_NUDGE_CLASS = "DualTouchFocusNudge"
_nudge_wndproc_ref = None  # keep the WndProc callback alive for the process


class _NudgeWndClass(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


# WS_EX_NOACTIVATE: the hop-noactivate mode creates the helper with this
# ex-style so the app's window never deactivates while the helper holds the
# foreground (flashing_issue.md open question #6). SetForegroundWindow still
# moves the foreground (Steam's WinEvent hook sees the change), but the
# helper being NOACTIVATE means the system doesn't dim the app that lost the
# foreground - no dim->brighten flash. If Steam instead reads the active
# window state, the app still re-activates on the hand-back.
_WS_EX_NOACTIVATE = 0x08000000


def _create_nudge_window(noactivate=False):
    """A 1x1 hidden helper window used for the foreground hop. Returns its
    HWND or None. Style 0 = not visible, no taskbar button, no flicker.
    `noactivate` (hop-noactivate mode) also applies WS_EX_NOACTIVATE so the
    app's window never dims while the helper holds the foreground."""
    global _nudge_wndproc_ref
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if _nudge_wndproc_ref is None:
            # True Win32 signatures — without them DefWindowProcW's 64-bit
            # lparam is truncated to 32-bit (OverflowError in the callback,
            # which fails CreateWindowExW).
            user32.DefWindowProcW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_ssize_t,
                ctypes.c_ssize_t,
            ]
            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            _nudge_wndproc_ref = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_ssize_t,
                ctypes.c_ssize_t,
            )(lambda h, m, w, lp: user32.DefWindowProcW(h, m, w, lp))
        hinstance = kernel32.GetModuleHandleW(None)
        wc = _NudgeWndClass()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(_nudge_wndproc_ref, ctypes.c_void_p)
        wc.hInstance = hinstance
        wc.lpszClassName = _NUDGE_CLASS
        user32.RegisterClassW(ctypes.byref(wc))  # 0 if already registered
        return user32.CreateWindowExW(
            _WS_EX_NOACTIVATE if noactivate else 0,
            _NUDGE_CLASS,
            _NUDGE_CLASS,
            0,
            0,
            0,
            1,
            1,
            None,
            None,
            hinstance,
            None,
        )
    except Exception:
        return None


def _nudge_focus_hop(target_hwnd, attempts=3, hold=0.03, noactivate=False):
    """Synthesize the window-activation change that makes Steam Input
    re-evaluate the auto config: give the hidden helper the foreground, then
    hand the foreground back to the app (the manual alt-tab equivalent).
    SetForegroundWindow into a foreign foreground is refused unless our input
    queue is attached to the current foreground thread's (same trick as
    triton's _restore_foreground). Returns True if the hop completed.

    `hold` is how long the helper keeps the foreground before handing it back:
    0.03 s (default) is long enough that Steam reliably sees the helper, at
    the cost of one visible dim->brighten on the app (the close flash).
    "hop-fast" (~0.002 s) relies on Steam's WinEvent delivery being
    asynchronous — both the helper and the hand-back events queue regardless
    of the gap, so Steam still sees the change while DWM likely never presents
    the inactive frame."""
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    if not target_hwnd:
        return False
    if not bool(user32.IsWindow(ctypes.c_void_p(target_hwnd))):
        return (
            False  # target window is gone — never hop to a dead/recycled hwnd
        )
    # Never steal focus: if a real, DIFFERENT app took the foreground since
    # the OSK closed, the user's click was already the activation change
    # Steam needs (it re-evaluates on the next one) — hopping now would
    # yank their new window to the front. Only hop while our own windows,
    # the shell, or the saved target itself hold the foreground.
    real_fg = _foreground_target_hwnd()
    if real_fg is not None and real_fg != target_hwnd:
        return False
    # The OSK is OPEN again: if the foreground is OUR OWN window (the OSK
    # or the tray popup), the /0 from the previous close is already moot —
    # the new open re-forced the layer appid — and hopping would visibly
    # yank the user's app to the front while they type. The close-nudge
    # thread can fire up to ~30 s after close, so this race is real.
    try:
        fg = user32.GetForegroundWindow()
        if fg:
            fg_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(
                ctypes.c_void_p(fg), ctypes.byref(fg_pid)
            )
            if fg_pid.value == os.getpid():
                return False
    except Exception:
        pass
    fg = int(user32.GetForegroundWindow())
    fg_thread = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None)
    cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    for _ in range(attempts):
        helper = None
        attached = False
        try:
            if fg_thread and fg_thread != cur_thread:
                attached = bool(
                    user32.AttachThreadInput(cur_thread, fg_thread, True)
                )
            helper = _create_nudge_window(noactivate=noactivate)
            if not helper:
                return False
            helper_ok = bool(
                user32.SetForegroundWindow(ctypes.c_void_p(helper))
            )
            # Keep the app's button unpress window at ~1 frame (default hold):
            # the helper holds the foreground just long enough for the
            # activation change to register, then the app re-activates
            # immediately (we own the foreground window, so the hand-back is
            # accepted — no refusal, no attention flash).
            time.sleep(hold)
            user32.SetForegroundWindow(ctypes.c_void_p(target_hwnd))
            if helper_ok:
                return True
        except Exception:
            return False
        finally:
            if attached:
                user32.AttachThreadInput(cur_thread, fg_thread, False)
            if helper:
                user32.DestroyWindow(ctypes.c_void_p(helper))
        time.sleep(0.1)
    return False


def _synthetic_foreground_signal(target_hwnd, bursts=3):
    """The close nudge's "invisible trigger": fire synthetic
    EVENT_SYSTEM_FOREGROUND / EVENT_OBJECT_FOCUS WinEvents at the app's window
    AFTER steam://forceinputappid/0 has been processed. Steam Input re-evaluates
    its forced appid on a real window-activation change; if it watches the
    system through a SetWinEventHook (the standard mechanism), this makes Steam
    re-read the foreground (the app — and now-unforced config) WITHOUT the app
    ever deactivating, so there is no dim->brighten flash. If Steam ignores
    synthetic events (hook-free polling, or compares against a cached value),
    the caller falls back to the real helper-window hop. Returns True if the
    events were signaled."""
    if not target_hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.NotifyWinEvent.restype = ctypes.c_int
        user32.NotifyWinEvent.argtypes = [
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_long,
        ]
        for _ in range(max(1, bursts)):
            user32.NotifyWinEvent(
                _EVENT_SYSTEM_FOREGROUND,
                ctypes.c_void_p(target_hwnd),
                _OBJID_WINDOW,
                _CHILDID_SELF,
            )
            user32.NotifyWinEvent(
                _EVENT_OBJECT_FOCUS,
                ctypes.c_void_p(target_hwnd),
                _OBJID_WINDOW,
                _CHILDID_SELF,
            )
            time.sleep(0.05)
    except Exception:
        return False
    return True


def _registered_steam_path():
    """The actual Steam install dir, resolved via the registry (HKCU
    Software\\Valve\\Steam -> SteamPath). Returns a str or None. Used to
    refuse log paths that don't live under the real Steam directory."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"
        ) as key:
            path, _ = winreg.QueryValueEx(key, "SteamPath")
        if path:
            return path.strip().replace("/", os.sep)
    except OSError:
        pass
    return None


def _steam_log_path(steam_path, filename):
    """The absolute path of a Steam log file ONLY when `steam_path` is the
    actual Steam install dir (resolved via the registry) — never a
    user-supplied path. The log contents gate the restore control-flow
    (LOW-1): a forged path could point at an attacker-controlled file that
    satisfies the wait. Returns the log path, or None when unverifiable."""
    real = _registered_steam_path()
    if not real:
        return None
    try:
        same = os.path.normcase(
            os.path.realpath(steam_path)
        ) == os.path.normcase(os.path.realpath(real))
    except OSError:
        return None
    if not same:
        return None
    return os.path.join(steam_path, "logs", filename)


def _steam_console_mark(steam_path, filename="console_log.txt"):
    """Size of a Steam log file at call time — the offset after which new
    entries are appended (used to only match lines dispatched by us, not
    earlier ones — a pre-existing forged line can't satisfy the wait).
    Returns 0 if the log can't be stat'd or the path isn't the real Steam
    dir."""
    try:
        path = _steam_log_path(steam_path, filename)
        if path is None:
            return 0
        return os.path.getsize(path)
    except OSError:
        return 0


def _wait_steam_url(steam_path, url_fragment, mark, timeout=30.0):
    """Wait until Steam's console log records the receipt of
    steam://<url_fragment> after `mark`. The dispatch is asynchronous, so
    this is the reliable "Steam processed the URL" signal. Returns True on
    receipt, False on timeout/error.

    Only the real Steam log (under the registry-resolved install dir) is
    read, and only bytes appended AFTER `mark` (the offset captured when the
    wait started) are matched — a forged line that predates the wait can
    never satisfy it (LOW-1)."""
    path = _steam_log_path(steam_path, "console_log.txt")
    if path is None:
        return False
    needle = 'ExecuteSteamURL: "steam://' + url_fragment + '"'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            size = os.path.getsize(path)
            if size > mark:
                with open(path, encoding="utf-8", errors="replace") as f:
                    f.seek(mark)
                    new = f.read()
                if needle in new:
                    return True
                mark = size  # don't re-read the same bytes next poll
        except OSError:
            pass
        time.sleep(0.05)
    return False


# Steam Input re-evaluation confirmations in controller_ui.txt. After the
# /0 is processed AND Steam Input actually re-evaluates the active window,
# it logs an OnFocusWindowChanged back to the Desktop config. This is the
# definitive "the appid returned to 0 and the auto config is active" signal:
# it appears on every successful restore and NEVER while the force stays
# stuck (observed across many open/close cycles). The nudge uses it to
# VERIFY a hop worked instead of trusting a fixed delay, and to retry only
# when the previous hop missed (too-early hop re-evaluates against the
# still-forced state, so Steam stays forced until a later activation change).
_STEAM_DESKTOP_RESTORE_MARKER = (
    "OnFocusWindowChanged to window type: k_nGameIDControllerConfigs_Desktop"
)


# Matches Steam Input's "active config" lines in controller_ui.txt:
#   OnFocusWindowChanged to window type: k_nGameIDControllerConfigs_Desktop, AppID 413080
#   OnFocusWindowChanged to window type: k_nGameIDControllerConfigs_ClientUI, AppID 769
# These say which controller config Steam Input is applying for the focused
# window. Deliberately does NOT match the keyboard-layer force line
# ("OnFocusWindowChanged URL Forcing ...", no "AppID N") so a capture taken
# at open is the pre-OSK config, never the layer itself.
def _wait_steam_restore(steam_path, mark, timeout=2.5):
    """Wait until controller_ui.txt records Steam Input re-evaluating back
    to the Desktop config after `mark` (the /0 restore signal). Returns True
    when seen, False on timeout. Polls fast (0.05s) so the hop-to-confirm
    latency stays low. Only the real Steam log is read, and only bytes
    appended after `mark` match (LOW-1)."""
    path = _steam_log_path(steam_path, "controller_ui.txt")
    if path is None:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            size = os.path.getsize(path)
            if size > mark:
                with open(path, encoding="utf-8", errors="replace") as f:
                    f.seek(mark)
                    new = f.read()
                if _STEAM_DESKTOP_RESTORE_MARKER in new:
                    return True
                mark = size
        except OSError:
            pass
        time.sleep(0.05)
    return False


def _capture_active_appid(steam_path):
    """Last Steam Input controller-config appid that was active for the
    focused window (from controller_ui.txt), or None if unknown. Read at
    OSK-open time BEFORE forcing the keyboard layer: this is the config to
    restore by forcing that SAME appid back on close. Restoring with a
    specific-appid force is applied by Steam IMMEDIATELY (no activation
    change needed), unlike forceinputappid/0 auto which only re-evaluates
    on a real focus change — the reason the old close path needed a
    helper-window hop (flash + unreliable timing).

    controller_ui.txt writes two focus formats, both matched:
      * game:     "OnFocusWindowChanged to game window type:
                   AppID 1623730, 1623730"
      * desktop:  "OnFocusWindowChanged to window type:
                   k_nGameIDControllerConfigs_Desktop, AppID 413080"
      * Big Pic:  "OnFocusWindowChanged to window type:
                   k_nGameIDControllerConfigs_ClientUI, AppID 769"
    Returns the MOST RECENT matching AppID (game/desktop/ClientUI): a
    capture taken at open is the pre-OSK config, so a game line beats an
    earlier Desktop line and the game's bindings come back on close. The
    keyboard-layer force line ("... URL Forcing ... 2537015031", no
    "AppID N") never matches. Only the real Steam log is read, and every
    matched line must be well-formed (the "AppID N" token with N an
    integer) (LOW-1)."""
    import re

    path = _steam_log_path(steam_path, "controller_ui.txt")
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            last = None
            for line in f:
                m = re.search(
                    r"OnFocusWindowChanged to (?:game )?window type: "
                    r"(?:k_\w+, )?AppID (\d+)\b",
                    line,
                )
                if m:
                    last = int(m.group(1))
            return last
    except OSError:
        return None


def _restore_auto_after_force(steam_path, restore_appid):
    """Daemon-thread worker: after the close path forced back to the
    captured appid (instant restore), dispatch forceinputappid/0 so Steam
    Input returns to AUTO-switching mode and follows the user's focus
    changes again (alt-tab to a game / Big Picture picks the right config).

    The /0 must land AFTER the specific-appid force is applied: both URLs
    travel through the async explorer.exe shell hop, so we wait for the
    force URL's receipt in Steam's console log first, then /0. No hop, no
    flash — the current app already has the correct config (the force
    applied it), /0 only re-enables auto-switching for the future."""
    try:
        mark = _steam_console_mark(steam_path)
        # Wait for the force URL's receipt (capped at 5s so /0 can never
        # be stalled if the receipt doesn't show), then a small settle so
        # Steam Input finishes applying the forced config before /0 flips
        # it back to auto. The settle is the ordering guarantee that
        # matters, not the receipt.
        _wait_steam_url(
            steam_path, f"forceinputappid/{restore_appid}", mark, timeout=5.0
        )
        time.sleep(0.2)
        from steam_shortcut import force_appid

        if force_appid(0):
            _log("steam input: dispatched /0 to restore auto-switching")
        else:
            _log(
                "steam input: /0 dispatch failed — Steam Input stays "
                f"forced to appid {restore_appid} (alt-tab won't switch configs)"
            )
    except Exception as e:
        _log(f"steam input: restore-auto /0 error: {e!r}")


def _nudge_after_restore(target_hwnd, steam_path, mode="hop", delay=1.0):
    """Daemon-thread worker: after /0, wait for Steam to actually process
    the URL, then make Steam Input re-evaluate in auto mode and re-apply the
    app's config.

    Hops then VERIFIES against Steam's controller_ui.txt (the Desktop
    OnFocusWindowChanged marker) that the appid actually returned to 0, and
    retries if not. This replaces the old hop-once-after-a-fixed-delay guess:
    the fixed delay was either too short (Steam's force-removal lag varies, so
    the appid stayed forced — "sometimes no controls") or too long (slow).
    Now the first hop runs after a short `delay`, and if Steam doesn't confirm
    the restore within a verification window, a later hop retries — the
    first-too-early hop re-evaluates against the still-forced state, but the
    NEXT activation change lands after the force removal and restores.

    Hop mode (settings "steam_input_nudge"):
      - "hop-noactivate": helper is WS_EX_NOACTIVATE — the app's window stays
        lit while the helper holds the foreground (no dim, no flash), and the
        hand-back still triggers Steam. Best no-flash candidate that actually
        restores (user-verified).
      - "hop": plain helper-window foreground hop — one visible dim->brighten.
      - "hop-fast": same hop held ~2ms — DWM likely never presents the
        inactive frame.
      - "event": synthetic WinEvents only — known NOT to make Steam
        re-evaluate (verified); kept for A/B. Falls back to a real hop.
    `delay` (settings "steam_input_nudge_delay", default 1.0) is the initial
    wait after /0 before the first hop; tune down to speed up the common case.
    Retries back off automatically so the slow-force-removal case still
    restores."""
    try:
        mark = _steam_console_mark(steam_path)
        if not _wait_steam_url(steam_path, "forceinputappid/0", mark):
            _log(
                "steam input: /0 not confirmed in Steam log — "
                "nudging focus anyway"
            )
        # Snapshot the controller_ui.txt offset AFTER /0, so verification only
        # sees re-evaluation events that happen from here on.
        ui_mark = _steam_console_mark(steam_path, "controller_ui.txt")
        hold = 0.002 if mode == "hop-fast" else 0.03
        noactivate = mode == "hop-noactivate"
        first_delay = max(0.0, float(delay))
        # Hop, verify, and if Steam hasn't re-evaluated within the window hop
        # again. The first hop is the fast path; retries cover the variable
        # force-removal lag (a too-early hop re-evaluates against the
        # still-forced state, and only a LATER activation change fixes it).
        attempt = 0
        total_deadline = time.monotonic() + first_delay + 6.0
        while time.monotonic() < total_deadline:
            attempt += 1
            if attempt == 1:
                time.sleep(first_delay)
            else:
                time.sleep(
                    0.4
                )  # back off: force-removal may still be in flight
            if mode == "event":
                fired = _synthetic_foreground_signal(target_hwnd)
                if fired:
                    _log(
                        f"steam input: attempt {attempt} synthetic foreground event "
                        "fired"
                    )
                else:
                    _log(
                        f"steam input: attempt {attempt} synthetic event failed — "
                        "real hop fallback"
                    )
                    _nudge_focus_hop(target_hwnd, hold=hold, noactivate=True)
            else:
                _nudge_focus_hop(target_hwnd, hold=hold, noactivate=noactivate)
            # Verify Steam actually re-evaluated back to the Desktop config.
            if _wait_steam_restore(steam_path, ui_mark, timeout=0.9):
                _log(
                    f"steam input: focus nudged (mode={mode}, attempt={attempt}) — "
                    "Steam re-applied the app's auto config"
                )
                return
        _log(
            f"steam input: restore not confirmed by Steam after {attempt} attempts "
            "— appid may stay forced (manual alt-tab needed)"
        )
    except Exception as e:
        _log(f"steam input: focus nudge error: {e!r}")


# --- Lock-screen guard ------------------------------------------------------
#
# This tray app runs in the *interactive user session* and keeps reading the
# controller even while the PC is locked. Without this guard, pressing X on the
# lock screen would pop our keyboard up on the user's (Default) desktop —
# invisible *behind* the secure Winlogon lock screen — instead of doing nothing.
# (The lock screen has its own separate keyboard launched via the accessibility
# hook.) OpenInputDesktop succeeds only when the *Default* desktop owns input;
# while the secure desktop is up (lock screen, UAC, Ctrl+Alt+Del) it fails from
# a user-session process, which is exactly our "is it locked?" signal.

_user32 = ctypes.windll.user32
_user32.OpenInputDesktop.restype = wintypes.HANDLE
_user32.OpenInputDesktop.argtypes = [
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
_user32.CloseDesktop.argtypes = [wintypes.HANDLE]
_user32.CloseDesktop.restype = wintypes.BOOL


def _workstation_locked():
    """True while the secure desktop owns input (lock screen / UAC / Secure
    Attention Sequence), so we must NOT open the keyboard behind it."""
    hdesk = _user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not hdesk:
        return True
    _user32.CloseDesktop(hdesk)
    return False


# Shell / desktop / system window classes that are never a real "type into me"
# target — so a stray firmware click onto the empty desktop or taskbar (or our
# own OSK) doesn't get remembered as the window to restore focus to.
_SHELL_WINDOW_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow",
    "ForegroundStaging",
    "MultitaskingViewFrame",
    "XamlExplorerHostIslandWindow",
}


def _foreground_target_hwnd():
    """The foreground window the user is typing in: a normal window owned by
    ANOTHER process. Returns None for our own windows and for the shell/desktop,
    so those never get recorded as the focus-restore target. HWND as an int."""
    try:
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return None
        buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
        if buf.value in _SHELL_WINDOW_CLASSES:
            return None
        return int(hwnd)
    except Exception:
        return None
