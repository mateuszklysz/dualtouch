import ctypes
import time
from contextlib import suppress
from ctypes import wintypes
from threading import Lock, Thread

from triton import state
from triton.win32 import _focus_log

# While the OSK is open, the PHYSICAL cursor is frozen in place by the
# controller-mouse hook below — Steam Input's emulated mouse (the touchpad
# moving the real cursor), the firmware lizard, an accidental hand bump can't
# move the pointer at all, so they can't disturb the desktop while you type.
# Deliberately NOT using ClipCursor: clipping a cursor that sits outside the
# OSK window's rect snaps it INTO the rect (it would "teleport to the
# keyboard", then freeze). The hook freezes it exactly where it was when the
# keyboard opened instead. A real physical mouse is neither injected nor
# correlated with controller activity, so it still passes through.
_INJECTED_CLICK_GATE = (
    0.25  # ignore external OSK mouse events this long after use
)


# --- Controller-mouse gate (WH_MOUSE_LL) -----------------------------------
# While the OSK is open, Steam Input's desktop config keeps emulating a mouse
# from the controller — the touchpad moves the REAL cursor the whole time the
# keyboard is open. A low-level mouse hook swallows the controller's mouse
# events outright (moves, clicks, wheel) so the cursor freezes in place,
# unless the OSK itself injected them (the right-stick mouse, flagged via
# state.set_osk_mouse_inject — passed so stick-pointing still reaches the
# keys). That exemption is MOVES ONLY: the OSK's right stick injects moves
# and nothing else, so an injected CLICK inside the exemption window can only
# be Steam Input's — a trigger / pad click emulating a mouse button — and it
# must never ride the exemption, or pressing R2/L2 to type on the OSK would
# also click the game under the frozen cursor. Two discriminators, because
# Steam Input's events are not always flagged LLMHF_INJECTED: the INJECTED
# flag, and correlation with recent controller activity (its emulated moves
# trail the controller action, so they always land inside
# _INJECTED_CLICK_GATE of it). A real physical mouse is neither, so it always
# passes. Armed whenever the OSK is visible — in click-through mode too:
# "Sticks Control Keyboard" OFF still must not let the touchpad drive the
# desktop while the keyboard is open.
_WH_MOUSE_LL = 14
_HC_ACTION = 0
_LLMHF_INJECTED = 0x00000001
_WM_MOUSEMOVE = 0x0200
_WM_MOUSEHWHEEL = 0x020E
_WM_QUIT = 0x0012


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_ulong),
    ]


_MouseProc = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)


_hook_u_cache = {}


def _hook_user32():
    u = _hook_u_cache.get("u")
    if u is not None:
        return u
    u = ctypes.windll.user32
    u.SetWindowsHookExW.restype = ctypes.c_void_p  # HHOOK
    u.SetWindowsHookExW.argtypes = [
        ctypes.c_int,
        _MouseProc,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    u.UnhookWindowsHookEx.restype = wintypes.BOOL
    u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    u.CallNextHookEx.restype = ctypes.c_long
    u.CallNextHookEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    u.GetMessageW.restype = wintypes.BOOL
    u.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        ctypes.c_void_p,
        wintypes.UINT,
        wintypes.UINT,
    ]
    u.PeekMessageW.restype = wintypes.BOOL
    u.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        ctypes.c_void_p,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    u.PostThreadMessageW.restype = wintypes.BOOL
    u.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _hook_u_cache["u"] = u
    return u


# Hook traffic counters, read at disarm for the log (the GIL makes the
# plain-int increments safe across the hook thread and the logger).
_hook_stats = {"seen": 0, "injected": 0, "recent": 0, "swallowed": 0}


def _ll_mouse_proc(nCode, wParam, lParam):
    """Low-level mouse hook: swallow the controller's mouse events while the
    OSK is open — unless the OSK itself injected them (the right-stick
    mouse, flagged via state.set_osk_mouse_inject). See the section comment
    for the injected/correlation discriminators."""
    if nCode == _HC_ACTION and _WM_MOUSEMOVE <= wParam <= _WM_MOUSEHWHEEL:
        try:
            ev = ctypes.cast(lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            now = time.monotonic()
            # Only the OSK's OWN right-stick injections may ride the exemption
            # — and they are moves exclusively. An injected click inside the
            # window is Steam Input's (trigger / pad click emulating a mouse
            # button): it must not pass or typing on the OSK would also click
            # the game under the frozen cursor.
            osk_own = (
                wParam == _WM_MOUSEMOVE
                and now - state.get_osk_mouse_inject_t() < 0.1
            )
            if not osk_own:
                _hook_stats["seen"] += 1
                if ev.flags & _LLMHF_INJECTED:
                    _hook_stats["injected"] += 1
                elif (
                    now - state.get_last_controller_activity()
                    < _INJECTED_CLICK_GATE
                ):
                    # Steam Input's emulated moves trail the controller
                    # action, so they always land inside this window; a real
                    # mouse never does.
                    _hook_stats["recent"] += 1
                else:
                    # Real physical mouse: pass it through.
                    return _hook_user32().CallNextHookEx(
                        None, nCode, wParam, lParam
                    )
                _hook_stats["swallowed"] += 1
                return 1  # swallow this event
        except Exception:
            pass
    return _hook_user32().CallNextHookEx(None, nCode, wParam, lParam)


class _MouseSwallow:
    """Installs/uninstalls the injected-mouse hook from its own thread (a
    WH_MOUSE_LL proc runs on the installing thread's message loop). start()
    and stop() are idempotent and safe to race each other — the queue is
    created before the hook installs, so a stop() that lands mid-install
    still delivers its WM_QUIT."""

    def __init__(self):
        self._lock = Lock()
        self._thread = None
        self._tid = 0

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            for k in _hook_stats:
                _hook_stats[k] = 0
            self._thread = Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            thread, self._thread = self._thread, None
            tid = self._tid
        if thread is not None and thread.is_alive() and tid:
            with suppress(Exception):
                _hook_user32().PostThreadMessageW(tid, _WM_QUIT, 0, 0)
            thread.join(timeout=1.0)
        _focus_log(
            "mousehook disarm seen={seen} injected={injected} "
            "recent={recent} swallowed={swallowed}".format(**_hook_stats)
        )

    def _run(self):
        u = _hook_user32()
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        # Create this thread's message queue BEFORE installing the hook, so a
        # stop() racing the install can always deliver its WM_QUIT.
        msg = wintypes.MSG()
        u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        proc = _MouseProc(_ll_mouse_proc)  # must stay referenced while hooked
        hk = u.SetWindowsHookExW(_WH_MOUSE_LL, proc, None, 0)
        if not hk:
            try:
                err = ctypes.get_last_error()
            except Exception:
                err = -1
            _focus_log(f"mousehook install FAILED err={err}")
            self._tid = 0
            return
        _focus_log(f"mousehook installed tid=0x{self._tid:x}")
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            u.UnhookWindowsHookEx(hk)
        self._tid = 0


_mouse_swallow = _MouseSwallow()


def _recent_controller_input():
    """True if the controller was ACTIVELY used within _INJECTED_CLICK_GATE.
    Mouse events landing on the OSK in that window are Steam Input's injected
    duplicates of the controller action (its desktop config emulates a mouse
    from the pads/sticks), so the OSK must ignore them: no highlight chase, no
    random key presses."""
    return (
        time.monotonic() - state.get_last_controller_activity()
    ) < _INJECTED_CLICK_GATE


def _mouse_highlight_allowed():
    """Whether OSK mouse-motion should move the hover highlight. True for a
    real physical mouse with the controller idle, and for mouse movement the
    OSK injected itself (the right-stick mouse — flagged via
    state.set_osk_mouse_inject). False while the controller is actively in
    use: that movement is Steam Input's emulation and would chase the
    highlight across the keyboard."""
    now = time.monotonic()
    if now - state.get_osk_mouse_inject_t() < 0.1:
        return True
    return (now - state.get_last_controller_activity()) >= _INJECTED_CLICK_GATE
