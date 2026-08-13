"""Minimal file logger for the tray app.

A windowed exe has no stdout, so every print() in the app is invisible to
the user and to us. All launcher/steam-watch events and exceptions are
mirrored into dualtouch.log next to the exe/script so a misbehaving build
can be diagnosed from the log file.
"""

import os
import sys
import threading
import time

# --- Resource / path helpers ------------------------------------------------


def _is_frozen():
    return getattr(sys, "frozen", False)


def _bundle_dir():
    """Directory containing read-only bundled resources (data/, glyphs)."""
    if _is_frozen():
        # PyInstaller sets sys._MEIPASS at bootstrap; not in typeshed stubs.
        return getattr(
            sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))
        )
    return os.path.dirname(os.path.abspath(__file__))


def _exe_dir():
    """Directory we treat as the install location (for portable settings)."""
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def user_data_dir():
    """Per-user writable directory for settings/state/logs: %APPDATA% + "DualTouch"
    (created on demand). Fall back to _exe_dir() when APPDATA isn't set (rare,
    e.g. a service context) so the app always has somewhere writable. This is
    where settings.json, steam_shortcut.json and dualtouch.log live — NOT next
    to the exe — so the app no longer needs a writable install folder.

    SECURITY: this directory is trusted by the (elevated) process, so it must
    never be a reparse point. A same-user process could otherwise pre-create
    it as a directory junction to anywhere the elevated token can write and
    redirect our reads/writes there (an elevated-follows-user-symlink
    primitive). We therefore try, in order: %APPDATA%\\DualTouch (must be a
    real non-reparse dir), then %LOCALAPPDATA%\\DualTouch, then the exe dir.
    The first clean location wins, so callers always get a usable path."""
    candidates = []
    for var in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidates.append(os.path.join(base, "DualTouch"))
    candidates.append(_exe_dir())
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            continue
        if _is_reparse_point(d):
            continue
        if not os.path.isdir(d):
            continue
        return d
    return candidates[-1]


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path):
    """True if `path` is a directory junction / symlink / mount point (the
    Windows reparse-point attribute). A junction needs no admin to create, so
    an attacker could point our trusted appdata dir anywhere. Checked via the
    st_file_attributes bit on the directory itself."""
    try:
        st = os.lstat(path)
        return bool(st.st_file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
    except (OSError, AttributeError):
        return False


_LOG_PATH = None

# Logging gate: dualtouch.log is only written while enabled (tray toggle).
# Default OFF — the persisted app default (DEFAULT_SETTINGS["logging_enabled"])
# is False, and every log write funnels through this single gate (see
# log_line/_log). When disabled, every log call is a cheap no-op — no file
# I/O, no directory creation.
_logging_enabled = False


def set_logging_enabled(enabled):
    """Enable/disable file logging (tray toggle). When disabled, _log and the
    other writers (steam_shortcut, triton focus log) become no-ops."""
    global _logging_enabled
    _logging_enabled = bool(enabled)


def is_logging_enabled():
    return _logging_enabled


def log_path():
    """The dualtouch.log path in the per-user data dir (may not exist yet).
    Single source of truth for the tray "View Log" item and _log."""
    return os.path.join(user_data_dir(), "dualtouch.log")


def resolve_log_action():
    """Pure decision for the tray "View Log" item: returns ("open", path) when
    logging is enabled AND the log file exists, else ("enable-first", path).
    The caller opens `path` with the default handler in the first case and
    prompts the user to enable logging first in the second. Logging is a
    tri-state here: enabled-but-never-written (file absent) must NOT silently
    open nothing — it is a "enable first" prompt like logging-off."""
    path = log_path()
    if _logging_enabled and os.path.isfile(path):
        return ("open", path)
    return ("enable-first", path)


# --- Log-file ACL hardening -------------------------------------------------
# dualtouch.log is a surveillance target (activity log). Its default
# inherited ACL is readable by anything the current user can read. On
# Windows we replace it with an explicit, PROTECTED DACL granting the
# current user full control and SYSTEM read-only, so a same-user
# bystander / low-integrity process can no longer read the log. The file
# lives in a per-user dir, so the user's own reading (troubleshooting)
# keeps working. Best-effort: any failure is swallowed — logging must
# never take the app down.
_ACL_APPLIED = set()


def _current_user_sid():
    """SID string (e.g. S-1-5-21-...) of the current process user, or None
    if it can't be resolved. Used to lock the log file to its owner."""
    try:
        import ctypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        TOKEN_QUERY = 0x0008
        TOKEN_USER = 1
        advapi32.OpenProcessToken.restype = ctypes.c_int
        advapi32.OpenProcessToken.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        h = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(h)
        ):
            return None
        try:
            advapi32.GetTokenInformation.restype = ctypes.c_int
            advapi32.GetTokenInformation.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
            advapi32.ConvertSidToStringSidW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            size = ctypes.c_ulong(0)
            advapi32.GetTokenInformation(
                h, TOKEN_USER, None, 0, ctypes.byref(size)
            )
            if not size.value:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                h, TOKEN_USER, buf, size.value, ctypes.byref(size)
            ):
                return None
            # TOKEN_USER is { SID_AND_ATTRIBUTES User; } — its first member
            # is a PSID pointing into this buffer.
            sid_ptr = ctypes.c_void_p.from_buffer(buf).value
            out = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(out)):
                return None
            try:
                return out.value
            finally:
                # `out` points to memory the API LocalAlloc'd; cast() is the
                # only way to recover its raw address from a c_wchar_p.
                kernel32.LocalFree(ctypes.cast(out, ctypes.c_void_p))
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None


def _restrict_log_acl(path):
    """Best-effort: replace `path`'s inherited DACL with an explicit one
    that only the current user (full) and SYSTEM (read) can access,
    protected so the parent directory's inheritable ACEs don't leak back
    in. Applied at most once per path per process (re-applied after the
    log rotates). Never raises. Paths outside Windows are a no-op."""
    if os.name != "nt":
        return
    if path in _ACL_APPLIED:
        return
    try:
        import ctypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        sid = _current_user_sid() or "OW"  # OW = file owner (= the user)
        sddl = f"D:P(A;;FA;;;{sid})(A;;0x1200a9;;;SY)"
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        advapi32.SetFileSecurityW.restype = ctypes.c_int
        advapi32.SetFileSecurityW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
        psd = ctypes.c_void_p()
        if advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(psd), None
        ):
            try:
                ok = advapi32.SetFileSecurityW(path, 0x0004, psd)  # DACL
            finally:
                kernel32.LocalFree(psd)
            # Only remember the path as locked when the apply actually
            # succeeded. SetFileSecurityW fails with ERROR_FILE_NOT_FOUND if
            # the file doesn't exist yet — the caller must create/append first
            # and then lock; caching a failed apply would leave the fresh log
            # permanently permissive (the surveillance vector this closes).
            if ok:
                _ACL_APPLIED.add(path)
    except Exception:
        pass


def log_line(tag, msg):
    """Append a tagged line to dualtouch.log via _log (same gate).

    The one public write path for every module that mirrors diagnostics
    (steam_shortcut, triton focus/diacritic, uinput, pad, cursor_helper):
    no caller opens the file itself, so when logging is disabled nothing
    touches the log path at all. `tag` becomes the [tag] prefix that
    diagnostics grep for."""
    _log(f"[{tag}] {msg}")


def _log(msg):
    """Append a timestamped line to dualtouch.log next to the exe/script.

    A windowed exe has no stdout, so every print() in this file is invisible
    to the user and to us. All launcher/steam-watch events and exceptions are
    mirrored here so a misbehaving build can be diagnosed from the log file.
    Capped at 1MB (rotated to .old) so a crash loop can't eat the disk; any
    logging failure is swallowed — logging must never take the app down.
    """
    global _LOG_PATH
    if not _logging_enabled:
        return
    try:
        # Re-resolve the dir on every write: user_data_dir() re-checks for a
        # junction/reparse point, so a same-user swap of %APPDATA%\DualTouch
        # after our first write can't redirect the log to an attacker-chosen
        # target (the cached path would have followed it blindly).
        _LOG_PATH = os.path.join(user_data_dir(), "dualtouch.log")
        try:
            if os.path.getsize(_LOG_PATH) > 1_000_000:
                os.replace(_LOG_PATH, _LOG_PATH + ".old")
                # The rotated-away file keeps its locked ACL; the fresh file
                # inherits the dir ACL again, so re-lock it after the append.
                _ACL_APPLIED.discard(_LOG_PATH)
        except OSError:
            pass
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"[{threading.current_thread().name}] {msg}\n"
            )
        # Lock AFTER the append: SetFileSecurityW needs the file to exist.
        # Caching only on success (see _restrict_log_acl) means a first-ever
        # launch or a post-rotation fresh file gets locked here, not left
        # permissively readable by same-user processes.
        _restrict_log_acl(_LOG_PATH)
    except Exception:
        pass
