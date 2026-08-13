"""Shared tray helpers.

Module-level helpers split out of tray.py: Steam-running detection,
Startup-folder autostart, and self-elevation.
"""

import ctypes
import hashlib
import sys
from ctypes import wintypes

import autostart
from applog import _log
from appsettings import STEAM_PROC_NAME


def _steam_running():
    """True if a steam.exe process is currently running."""
    try:
        import psutil
    except ImportError:
        return False
    for proc in psutil.process_iter(attrs=["name"]):
        try:
            if (proc.info.get("name") or "").lower() == STEAM_PROC_NAME:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _apply_autostart(enabled):
    autostart.set_enabled(bool(enabled))


# Single-instance guard: only ONE tray process may run at a time. Two
# instances would BOTH read the shared Steam Controller HID and BOTH dispatch
# the same key press -> 2-3 letters typed at once (observed). The mutex name
# is scoped to the current user (a SID hash suffix) and protected by a
# user-only DACL, so no other user or integrity level can pre-create the
# well-known name (a persistent local DoS) or probe the app's liveness.
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000
_MUTEX_ALL_ACCESS = 0x001F0001
_ACL_REVISION = 2
_SECURITY_MAX_SID_SIZE = 68


def _current_user_sid_str():
    """The current user's SID string (e.g. S-1-5-21-...-1001), or None on
    failure. Used to scope the single-instance mutex name and DACL."""
    advapi32 = ctypes.windll.advapi32
    advapi32.GetUserNameW.argtypes = [
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetUserNameW.restype = wintypes.BOOL
    advapi32.LookupAccountNameW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.LookupAccountNameW.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    name = ctypes.create_unicode_buffer(256)
    nlen = wintypes.DWORD(len(name))
    if not advapi32.GetUserNameW(name, ctypes.byref(nlen)):
        return None
    sid_buf = ctypes.create_string_buffer(_SECURITY_MAX_SID_SIZE)
    cb_sid = wintypes.DWORD(len(sid_buf))
    domain = ctypes.create_unicode_buffer(256)
    cb_domain = wintypes.DWORD(len(domain))
    use = wintypes.DWORD()
    if not advapi32.LookupAccountNameW(
        None,
        name.value,
        sid_buf,
        ctypes.byref(cb_sid),
        domain,
        ctypes.byref(cb_domain),
        ctypes.byref(use),
    ):
        return None
    p_str = ctypes.c_void_p()
    if not advapi32.ConvertSidToStringSidW(
        ctypes.cast(sid_buf, ctypes.c_void_p), ctypes.byref(p_str)
    ):
        return None
    try:
        v = p_str.value
        if v is None:
            return None
        return ctypes.wstring_at(v)
    finally:
        kernel32.LocalFree(p_str.value)


def _tray_mutex_name():
    """Per-user mutex name: the well-known name plus a hash of the current
    user's SID, so another user or integrity level cannot pre-create it. Falls
    back to the plain well-known name only if the SID lookup fails (effectively
    impossible)."""
    key = _current_user_sid_str()
    if not key:
        return "Local\\DualTouch_Tray_SingleInstance"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return "Local\\DualTouch_Tray_SingleInstance_" + digest


_MUTEX_NAME = _tray_mutex_name()


class _SECURITY_DESCRIPTOR(ctypes.Structure):
    _fields_ = [
        ("Revision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("Control", wintypes.WORD),
        ("Owner", ctypes.c_void_p),
        ("Group", ctypes.c_void_p),
        ("Sacl", ctypes.c_void_p),
        ("Dacl", ctypes.c_void_p),
    ]


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


def _user_only_dacl():
    """Build a PACL that grants ONLY the current user full access to a mutex
    and nothing to anyone else (other users and integrity levels are denied).
    Returns the ACL buffer (a plain Python allocation — no free needed) or
    None on failure."""
    sid_str = _current_user_sid_str()
    if not sid_str:
        return None
    advapi32 = ctypes.windll.advapi32
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.InitializeAcl.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    p_sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(sid_str, ctypes.byref(p_sid)):
        return None
    try:
        acl = ctypes.create_string_buffer(1024)
        if not advapi32.InitializeAcl(acl, len(acl), _ACL_REVISION):
            return None
        if not advapi32.AddAccessAllowedAceEx(
            acl, _ACL_REVISION, 0, _MUTEX_ALL_ACCESS, p_sid.value
        ):
            return None
        return acl
    finally:
        kernel32.LocalFree(p_sid.value)


def _create_tray_mutex():
    """Create the single-instance mutex, scoped per user (SID-hashed name)
    with a user-only DACL, so no other user or integrity level can pre-create
    it or probe it. Returns the handle (keep alive for the process lifetime)
    or None if another instance already holds it."""
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    advapi32.InitializeSecurityDescriptor.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL
    advapi32.SetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        ctypes.c_void_p,
        wintypes.BOOL,
    ]
    advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.restype = wintypes.DWORD

    p_dacl = _user_only_dacl()
    sa = _SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
    sa.bInheritHandle = False
    if p_dacl is None:
        # DACL build failed (effectively impossible) — fall back to the
        # process default security, never a NULL DACL.
        sa.lpSecurityDescriptor = 0
    else:
        sd = _SECURITY_DESCRIPTOR()
        advapi32.InitializeSecurityDescriptor(ctypes.byref(sd), 1)
        advapi32.SetSecurityDescriptorDacl(
            ctypes.byref(sd), True, ctypes.addressof(p_dacl), False
        )
        sa.lpSecurityDescriptor = ctypes.cast(
            ctypes.byref(sd), ctypes.c_void_p
        ).value
    h = kernel32.CreateMutexW(ctypes.byref(sa), False, _MUTEX_NAME)
    if not h:
        return None
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        # Another live process holds the name. With the SID-scoped name a
        # hostile pre-create by a different user/integrity level is
        # impossible; the owning PID of a mutex is not queryable via
        # GetSecurityInfo / GetWindowThreadProcessId (windows only), so
        # best-effort is to confirm the object is reachable and log it —
        # it is a real conflict either way.
        try:
            kernel32.OpenMutexW.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.LPCWSTR,
            ]
            kernel32.OpenMutexW.restype = wintypes.HANDLE
            probe = kernel32.OpenMutexW(SYNCHRONIZE, False, _MUTEX_NAME)
            if probe:
                kernel32.CloseHandle(probe)
                detail = "held by a live same-user tray instance"
            else:
                detail = (
                    f"exists but not openable (err={kernel32.GetLastError()})"
                )
        except Exception:
            detail = "conflict"
        _log(f"[dualtouch] single-instance mutex {_MUTEX_NAME}: {detail}")
        kernel32.CloseHandle(h)
        return None
    return h


def _tray_mutex_held():
    """True if another tray instance currently holds the single-instance
    mutex (probe only, no ownership)."""
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    h = kernel32.OpenMutexW(SYNCHRONIZE, False, _MUTEX_NAME)
    if h:
        kernel32.CloseHandle(h)
        return True
    return False


def _relaunch_elevated():
    """Re-run this exe elevated (UAC prompt) and exit this instance. The
    tray needs elevation to type into Steam Big Picture / games (UIPI),
    but the exe is deliberately built WITHOUT an admin manifest so the
    same binary can also run non-elevated as the --cursor-helper
    (scheduled-task) cursor worker. Returns True if the elevated copy was
    launched."""
    try:
        import ctypes
        from ctypes import wintypes

        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = wintypes.HINSTANCE
        shell32.ShellExecuteW.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_int,
        ]
        res = shell32.ShellExecuteW(
            None, "runas", sys.executable, " ".join(sys.argv[1:]), None, 1
        )
        return int(res) > 32
    except Exception:
        return False
