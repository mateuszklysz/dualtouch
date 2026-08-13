import ctypes
import os
import sys
import time
from contextlib import suppress
from ctypes import wintypes

import sdl3w as S
from applog import log_line

from triton import state

_IS_WINDOWS = sys.platform == "win32"


def _hwnd_of(sdl_window):
    # SDL3 dropped SDL_GetWindowWMInfo in favor of window properties; sdl3w
    # wraps the SDL.window.win32.hwnd lookup. Returns the HWND as an int.
    return S.get_win32_hwnd(sdl_window)


# Win32 constants used by the focus / z-order helpers below.
_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TOPMOST = 0x00000008
# Click-through: the mouse (move/click/wheel) passes straight to the window
# behind. Toggled live so the OSK can become a pure touchpad-typing overlay
# while the sticks/mouse drive the desktop. WS_EX_TRANSPARENT alone only passes
# through to SIBLING windows in our own process; to pass to OTHER apps the
# window must ALSO be WS_EX_LAYERED. SDL3 makes SDL_WINDOW_TRANSPARENT via DWM
# (NOT a layered window — verified at runtime), so we add WS_EX_LAYERED here.
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED = 0x00080000
_LWA_ALPHA = 0x00000002
_SW_SHOWNOACTIVATE = 4
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020
_SWP_SHOWWINDOW = 0x0040
# HWND_TOPMOST is the sentinel (-1) for SetWindowPos's hWndInsertAfter param.
# It must be passed as a 64-bit HANDLE on x64; using c_void_p(-1) coerces it
# to all-bits-set in the wider register.
_HWND_TOPMOST = ctypes.c_void_p(-1)
_HWND_BOTTOM = ctypes.c_void_p(1)


def _user32():
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_longlong
    user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_longlong
    user32.SetWindowLongPtrW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_longlong,
    ]
    user32.SetWindowPos.restype = ctypes.c_bool
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.ShowWindow.restype = ctypes.c_bool
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    user32.AttachThreadInput.restype = ctypes.c_bool
    user32.AttachThreadInput.argtypes = [
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_bool,
    ]
    user32.SetForegroundWindow.restype = ctypes.c_bool
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.AllowSetForegroundWindow.restype = ctypes.c_bool
    user32.AllowSetForegroundWindow.argtypes = [ctypes.c_ulong]
    user32.IsWindow.restype = ctypes.c_bool
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.restype = ctypes.c_bool
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    user32.SetLayeredWindowAttributes.restype = ctypes.c_bool
    user32.SetLayeredWindowAttributes.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_ubyte,
        ctypes.c_uint,
    ]
    return user32


def _force_topmost(hwnd):
    """Make `hwnd` topmost without stealing focus. On Windows, a non-foreground
    process can have its SetWindowPos(HWND_TOPMOST) silently downgraded — most
    common workaround is to briefly attach our input queue to the foreground
    thread's, then issue the SetWindowPos. The attach makes the elevation
    check pass; SWP_NOACTIVATE + WS_EX_NOACTIVATE on the window keep focus
    where it was."""
    user32 = _user32()
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong

    flags = _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE
    foreground = user32.GetForegroundWindow()
    fg_thread = (
        user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
    )
    cur_thread = kernel32.GetCurrentThreadId()

    attached = False
    if fg_thread and fg_thread != cur_thread:
        attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
    try:
        user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, flags)
    finally:
        if attached:
            user32.AttachThreadInput(cur_thread, fg_thread, False)


def _set_always_on_top_portable(sdl_window):
    """Ask SDL to keep the window above others without stealing focus. Used on
    Linux/X11 to request _NET_WM_STATE_ABOVE; on Windows the z-order is owned
    by the Win32 WS_EX_TOPMOST path instead."""
    with suppress(Exception):
        S.SDL_SetWindowAlwaysOnTop(sdl_window, True)


def _reassert_topmost(sdl_window):
    """Re-assert the OSK window's topmost z-order. The open animation's settle
    phase issues a burst of SDL_SetWindowPosition calls (~30 in well under a
    second); each is a chance for Windows to silently re-resolve z-order
    against another always-on-top window (e.g. a fullscreen game), which can
    leave the OSK visible but no longer the window that receives mouse input.
    Called once the animation finishes settling."""
    if _IS_WINDOWS:
        hwnd = _hwnd_of(sdl_window)
        if hwnd is not None:
            _force_topmost(hwnd)
    else:
        _set_always_on_top_portable(sdl_window)


def _make_window_non_activating(sdl_window):
    """Apply WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW to the SDL window's HWND
    while it's still hidden. NOACTIVATE keeps the user's target app (e.g.
    a browser search field) focused. TOPMOST is deliberately NOT set here:
    the window must be sinkable to the bottom of the normal z-band at show
    time (see _show_window_noactivate — a topmost window would be promoted
    into a transiently-NULL foreground and flash the app's taskbar button);
    the OSK is forced topmost by _force_topmost right after it is shown.

    On non-Windows the heavy lifting is done by the SDL hint set at window
    creation (SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN=0) plus always-on-top;
    this function is a no-op there."""
    if not _IS_WINDOWS:
        return
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        print("warning: win32 HWND lookup failed; window will steal focus")
        return
    user32 = _user32()
    current = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    user32.SetWindowLongPtrW(
        hwnd, _GWL_EXSTYLE, current | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW
    )
    # Flush the latent ex-style change (SetWindowLong values only take
    # effect after a SetWindowPos with SWP_FRAMECHANGED).
    user32.SetWindowPos(
        hwnd,
        None,
        0,
        0,
        0,
        0,
        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
    )


def _set_click_through(sdl_window, enabled):
    """Add/remove WS_EX_TRANSPARENT on the OSK HWND. When enabled the mouse
    ignores the OSK entirely — moves, clicks and the scroll wheel fall through
    to the app behind it — so the right-stick mouse and left-stick scroll drive
    the desktop while the touchpads still type on the keyboard. No-op off Windows
    (X11 click-through is a separate mechanism — see the Linux tree's TODO)."""
    if not _IS_WINDOWS:
        return
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        return
    user32 = _user32()
    current = user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
    bits = _WS_EX_LAYERED | _WS_EX_TRANSPARENT
    new = (current | bits) if enabled else (current & ~bits)
    if new == current:
        return
    user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, new)
    if enabled:
        # A freshly-layered window has undefined alpha until we set it; force it
        # fully opaque so the keyboard stays visible (uniform alpha overrides the
        # per-pixel transparent-skin see-through while click-through is active).
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, _LWA_ALPHA)
    # Flush the latent ex-style change so hit-testing picks it up immediately.
    user32.SetWindowPos(
        hwnd,
        None,
        0,
        0,
        0,
        0,
        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
    )


def _show_window_noactivate(sdl_window):
    """Bring the OSK on screen without stealing focus and force it into the
    topmost z-order. WS_EX_NOACTIVATE on the HWND means even Win32 calls
    that would normally activate the window (SetForegroundWindow,
    SetWindowPos without SWP_NOACTIVATE) leave focus alone.

    The window is sunk to the bottom of the z-order BEFORE it is shown:
    around the show the foreground can be transiently NULL (Windows'
    no-activate bookkeeping), and the system then promotes the TOPMOST
    window into that NULL foreground. If that were the just-shown OSK,
    the app's taskbar button would unpress and the post-show restore
    would re-press it — the "flash when opening the keyboard". With the
    OSK at the bottom, the promotion lands on the app's own window (no
    visible change); the OSK is forced topmost right after, and its first
    frame is fully transparent (the open animation), so the z-order jump
    is invisible.

    When the always-visible focus-flash fix is active (settings
    "focus_fix_open": "always-visible"), the window was created VISIBLE and
    parked off-screen (see screen.Screen): "opening" is just moving it onto
    the display (already done by _begin_open_anim) plus forcing topmost —
    there is NO ShowWindow, so the transiently-NULL foreground (and the
    app's dim→brighten flash) never happens. The sink/show/topmost
    sequence below only runs in the legacy hidden-window mode.

    On X11 we fall back to SDL's portable show + always-on-top; combined
    with SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN=0 (set in Screen.__init__)
    most compositors will skip focus on map."""
    if not _IS_WINDOWS:
        S.SDL_ShowWindow(sdl_window)
        _set_always_on_top_portable(sdl_window)
        return
    hwnd = _hwnd_of(sdl_window)
    if hwnd is None:
        S.SDL_ShowWindow(sdl_window)
        return
    user32 = _user32()

    # Focus-flash diagnostic: the open flash is a transiently-NULL foreground
    # (the app dims) followed by a restore (brightens). Logging the foreground
    # at each step pinpoints which Win32 call produces the NULL.
    def _fg_trace(tag):
        # pid + class only (no window title / HWND) — see _fg_desc.
        _focus_log(f"show {tag} {_fg_desc()}")

    if state.is_osk_always_visible():
        _fg_trace("av-before")
        if not bool(user32.IsWindowVisible(ctypes.c_void_p(hwnd))):
            # Defensive: something hid the window (shouldn't happen in
            # always-visible mode) — bring it back without stealing focus.
            _fg_trace("av-reveal")
            user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
        _force_topmost(hwnd)
        _fg_trace("av-after")
        return
    _fg_trace("before")
    user32.SetWindowPos(
        hwnd,
        _HWND_BOTTOM,
        0,
        0,
        0,
        0,
        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
    )
    _fg_trace("sunk")
    user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
    _fg_trace("shown")
    _force_topmost(hwnd)
    _fg_trace("topmost")


# Window classes that are never a "real" user app for focus purposes: the
# shell/desktop/taskbar, plus Windows' transient UI shells (Start search,
# alt-tab, multitasking). Mirrors tray.py's _SHELL_WINDOW_CLASSES (triton must
# not import the tray). If the foreground is one of these, an injected click
# probably stole focus to it and restoring the saved window is wanted — NOT a
# deliberate user action to back off from.
_SHELL_FG_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow",
    "ForegroundStaging",
    "MultitaskingViewFrame",
    "XamlExplorerHostIslandWindow",
}


def _classname(hwnd):
    user32 = _user32()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
    return buf.value


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _kernel32():
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    # Without argtypes, a 64-bit HANDLE is coerced to c_int and truncated on
    # the way in. Windows kernel handles fit 32 bits so it's benign in
    # practice, but declare it properly so CloseHandle can never get a
    # mangled handle.
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


# Exe names that are never a "positionable app" for the per-app remembered
# OSK position: our own process (python.exe from source / DualTouch-windows.exe
# frozen), the desktop shell (the Progman/WorkerW/Shell_TrayWnd window classes
# are all hosted by explorer.exe), Steam's own windows (steam.exe client,
# steamwebhelper.exe Big Picture overlay), and the Start-menu hosts. For these
# _foreground_exe_name() returns None and the OSK falls back to the global
# default position (never persisting per-app).
_NON_POSITIONABLE_EXES = {
    "python.exe",
    "pythonw.exe",
    "dualtouch-windows.exe",
    "explorer.exe",
    "steam.exe",
    "steamwebhelper.exe",
    "startmenuexperiencehost.exe",
    "searchhost.exe",
    "searchapp.exe",
}

# (foreground pid, exe name, expires_at) -- bounds the OpenProcess /
# QueryFullProcessImageNameW lookup to at most one per pid per TTL. The
# foreground pid itself is re-read on every call (cheap GetForegroundWindow), so
# an app switch is still detected immediately; only the expensive process-name
# resolution is cached.
_FOREGROUND_EXE_CACHE = None
_FOREGROUND_EXE_TTL = 2.0


def _window_title(hwnd):
    user32 = _user32()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, 256)
    return buf.value


def _foreground_exe_name():
    """Lowercased exe name of the current foreground window's process, or None
    when it isn't a positionable app (no foreground, shell/desktop, our own
    process, Steam, Start menu). TTL-cached per foreground pid so the hot main
    loop never re-opens a process handle every tick."""
    global _FOREGROUND_EXE_CACHE
    if not _IS_WINDOWS:
        return None
    user32 = _user32()
    hwnd = user32.GetForegroundWindow() if user32 else None
    pid = 0
    if hwnd:
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid = pid.value
    now = time.monotonic()
    cached = _FOREGROUND_EXE_CACHE
    if cached is not None and cached[0] == pid and cached[2] > now:
        return cached[1]
    name = None
    if pid:
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            try:
                buf = ctypes.create_unicode_buffer(260)
                size = wintypes.DWORD(260)
                if kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)
                ):
                    exe = buf.value.rsplit("\\", 1)[-1].lower()
                    if exe not in _NON_POSITIONABLE_EXES:
                        name = exe
            except Exception:
                name = None
            finally:
                kernel32.CloseHandle(handle)
    _FOREGROUND_EXE_CACHE = (pid, name, now + _FOREGROUND_EXE_TTL)
    return name


def _focus_log(msg):
    """Append a line to dualtouch.log in the per-user appdata dir (same
    location as the tray's log), via the applog gate so logging-off never
    touches the log path."""
    log_line("triton", msg)


def _fg_desc(osk_hwnd: int | None = 0):
    """Identify the current foreground window (pid + class ONLY — never the
    window TITLE or HWND, which are surveillance-sensitive in the log),
    plus whether the OSK still has its WS_EX_NOACTIVATE bit (a lost bit
    means SDL re-styled the window)."""
    try:
        user32 = _user32()
        fg = user32.GetForegroundWindow()
        hwnd = int(fg) if fg else 0
        if not hwnd:
            parts = ["fg=none"]
        else:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            parts = [f"fg pid={pid.value} class={_classname(hwnd)!r}"]
        if osk_hwnd:
            ex = user32.GetWindowLongPtrW(
                ctypes.c_void_p(osk_hwnd), _GWL_EXSTYLE
            )
            parts.append(f"osk_noactivate={bool(ex & _WS_EX_NOACTIVATE)}")
            if hwnd and int(osk_hwnd) == hwnd:
                parts.append("fg_is_osk=True")
        return " ".join(parts)
    except Exception as e:
        return f"fg_desc err {e!r}"


def _restore_foreground(hwnd):
    """Re-focus the window the user was typing in before the OSK opened.

    A controller-open fires the firmware lizard's mouse-click (X's desktop
    action) which can land off the target text field and steal its focus. The
    OSK window is WS_EX_NOACTIVATE so it never takes focus itself, so
    re-activating the saved window restores the caret while the OSK stays on
    top. No-op off Windows, when nothing was saved, or if the window is gone.

    Returns True if the target is (or is now) foreground, False if the
    activation was refused or focus is on a real app we must not fight —
    the caller must then STOP retrying: every refused SetForegroundWindow
    flashes the target's taskbar button. Returns None when the foreground
    is NULL — nothing was attempted, so the caller SHOULD retry: the
    foreground can reappear (typically as our own OSK window) a moment
    later."""
    if not _IS_WINDOWS or not hwnd:
        return False
    user32 = _user32()
    if not user32.IsWindow(ctypes.c_void_p(hwnd)):
        return False
    try:
        cur_fg = int(user32.GetForegroundWindow())
    except Exception:
        cur_fg = 0
    if cur_fg == hwnd:
        return True
    # No foreground at all — a transient state around the open (the previous
    # session's OSK window being destroyed, or the noactivate show racing
    # Windows' foreground bookkeeping). Restoring the saved sample into a
    # NULL foreground would yank an old window to the front for no reason —
    # e.g. the user closed their game moments before the chord, and our stale
    # target is Explorer — so nothing is attempted HERE, but this is NOT a
    # refusal: the foreground can reappear (typically as our own OSK window —
    # see fg_is_osk in the log) a moment later, and the caller must keep
    # retrying to catch that. Return None = retry; no SetForegroundWindow was
    # made, so there is nothing to flash.
    if not cur_fg:
        return None
    # Don't fight the user: if focus has moved to a real, different app (not
    # the shell/desktop/taskbar, not our own windows), that's a deliberate
    # click, not an injected one stealing focus — leave it alone. This
    # includes Steam's own windows (Big Picture overlay, Steam client):
    # activating against them is REFUSED (foreground lock), and each refused
    # attempt flashes their taskbar button, so no exemption is made. (The
    # chord is delivered to this app by the raw-HID watcher — see tray.py —
    # so no Ctrl+Alt+K path is involved here.)
    if cur_fg:
        try:
            cur_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(
                ctypes.c_void_p(cur_fg), ctypes.byref(cur_pid)
            )
            if (
                cur_pid.value
                and cur_pid.value != os.getpid()
                and _classname(cur_fg) not in _SHELL_FG_CLASSES
            ):
                return False
        except Exception:
            pass
    # SetForegroundWindow from a background process is refused unless we
    # attach our input queue to the current foreground thread's (the same
    # elevation trick as _force_topmost). BringWindowToTop inside the attach
    # plus the return-value check tell us whether the activation was accepted.
    fg_thread = (
        user32.GetWindowThreadProcessId(ctypes.c_void_p(cur_fg), None)
        if cur_fg
        else 0
    )
    cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = False
    if fg_thread and fg_thread != cur_thread:
        attached = bool(user32.AttachThreadInput(cur_thread, fg_thread, True))
    ok = False
    try:
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        ok = bool(user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
    finally:
        if attached:
            user32.AttachThreadInput(cur_thread, fg_thread, False)
    return ok


# Processes that host the Start menu and its search-results view, across
# different Windows builds. On 24H2+ both the Start launcher and its search
# view run as SearchApp.exe; older builds use StartMenuExperienceHost/
# SearchHost. While typing into Start's search box (e.g. via the OSK), focus
# hands between these without the menu visually closing, so
# _is_start_menu_open() must treat any of them as "Start is open".
_START_MENU_PROCESSES = {
    "startmenuexperiencehost.exe",
    "searchhost.exe",
    "searchapp.exe",
}


def _is_start_menu_open():
    """True if the Windows Start menu (or its search-results view) is
    currently open and focused. Start opens from the bottom-center and grows
    upward, covering the keyboard at its usual remembered spot — so the open
    call sites force _POS_UP_RIGHT instead while this is true."""
    if not _IS_WINDOWS:
        return False
    user32 = _user32()
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return False
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
    )
    if not handle:
        return False
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buf, ctypes.byref(size)
        ):
            return False
        return buf.value.rsplit("\\", 1)[-1].lower() in _START_MENU_PROCESSES
    finally:
        kernel32.CloseHandle(handle)
