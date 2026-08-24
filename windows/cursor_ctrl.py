"""Non-elevated system-cursor control for the OSK.

The DualTouch tray runs ELEVATED (UAC admin). An elevated process cannot
reliably change the interactive session's system cursors (SetSystemCursor
fails with ERROR_CURSOR_NOT_FOUND, SPI_SETCURSORS fails, GetCursorInfo
fails -- verified at runtime). The reliable path is to run a NON-elevated
helper in the interactive session.

Mechanism (one binary, two exe names):
  1. A Windows scheduled task "DualTouchCursor" re-invokes the
     DualTouch exe with --cursor-helper. A scheduled task starts at the
     interactive user's integrity level (non-elevated) regardless of how
     the registering process was elevated.
  2. To hide/show, we write a marker file (hide|show) and trigger the task
     via `schtasks /Run`. The re-invoked exe (in --cursor-helper mode)
     reads the marker, does the cursor work, and removes it.

The daemon runs from "DualTouch-cursor-helper.exe" — build.py ships a
copy of the tray exe under that name, carrying its own FileDescription
resource ("DualTouch Cursor Helper") so Task Manager shows which process
is which: "DualTouch-windows.exe" is the tray,
"DualTouch-cursor-helper.exe" is the non-elevated cursor daemon. If the
copy can't be maintained (read-only folder), the task falls back to
re-invoking the tray exe itself.
"""

import ctypes
import os
import secrets
import shutil
import struct
import subprocess
import sys
from contextlib import suppress

from applog import _log, user_data_dir

_TASK_NAME = "DualTouchCursor"
_MARKER_NAME = "cursor_action.txt"
# The helper is THIS SAME binary, re-invoked with --cursor-helper. The
# scheduled task runs it non-elevated (a scheduled task starts at the
# interactive user's integrity level, not the elevated tray's), so the
# same binary that runs the tray can also manipulate the session cursors.
_HELPER_FLAG = "--cursor-helper"
_HELPER_EXE_NAME = "DualTouch-cursor-helper.exe"

# Per-session marker authentication token. Generated ONCE per process. A
# same-user process could otherwise write "hide|1" (PID 1 = System, always
# "alive") into the marker and blank every system cursor forever — a
# UI-cover DoS. The token is baked into the scheduled task command line at
# install time and carried inside every marker write, so the helper (which
# re-invokes THIS exe) only honors markers that carry the exact token it was
# launched with.
_TOKEN = secrets.token_hex(16)


def _marker_path():
    # The non-elevated helper and the elevated tray share %APPDATA%\DualTouch,
    # so the marker survives regardless of where the exe lives.
    return os.path.join(user_data_dir(), _MARKER_NAME)


def _product_version(path):
    """ProductVersion string of an exe via the version API, or None when it
    has no readable VERSIONINFO."""
    try:
        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None
        ptr = ctypes.c_void_p()
        n = ctypes.c_uint()
        if (
            not ver.VerQueryValueW(
                buf,
                "\\VarFileInfo\\Translation",
                ctypes.byref(ptr),
                ctypes.byref(n),
            )
            or ptr.value is None
        ):
            return None
        langid, codepage = struct.unpack_from(
            "<HH", buf, ptr.value - ctypes.addressof(buf)
        )
        if (
            not ver.VerQueryValueW(
                buf,
                f"\\StringFileInfo\\{langid:04x}{codepage:04x}\\ProductVersion",
                ctypes.byref(ptr),
                ctypes.byref(n),
            )
            or ptr.value is None
        ):
            return None
        return ctypes.wstring_at(ptr.value)
    except OSError:
        return None


def _frozen_helper_exe():
    """Path of the renamed helper-exe copy next to the frozen tray exe,
    refreshed from the running binary when missing or stale. Returns None
    when the copy can't be maintained (e.g. read-only install folder) —
    the caller falls back to applog._exe_path. Both exes are identical
    standalone builds loading the same support files beside them; the
    shipped copy differs only in its FileDescription resource (so Task
    Manager can tell the two processes apart), which is why staleness is
    judged by ProductVersion rather than raw size."""
    try:
        from applog import _exe_path

        me = _exe_path()
        if os.path.basename(me).lower() == _HELPER_EXE_NAME.lower():
            return me  # already running as the helper copy
        target = os.path.join(os.path.dirname(me), _HELPER_EXE_NAME)
        try:
            if os.path.isfile(target) and (
                os.path.getsize(target) == os.path.getsize(me)
                or (
                    _product_version(target) is not None
                    and _product_version(target) == _product_version(me)
                )
            ):
                return target  # present and same build (shipped together)
        except OSError:
            pass
        shutil.copyfile(me, target)
        return target
    except OSError as e:
        _log(f"cursor: helper exe copy unavailable ({e!r}); using tray exe")
        return None


def install_helper():
    """Register a scheduled task that re-invokes the DualTouch exe with
    --cursor-helper. A scheduled task starts the target at the interactive
    user's integrity level (non-elevated), which is the only context that
    can change the session's cursors — the elevated tray cannot. The task
    is re-created with /F on every startup so the exe path stays current
    (portable exe moved to a new folder just re-registers).
    Returns True on success."""
    try:
        import applog

        if applog._is_frozen():
            # Frozen: launch the renamed helper copy so Task Manager shows
            # "DualTouch-cursor-helper.exe" for this daemon, distinct from
            # the "DualTouch-windows.exe" tray. --daemon makes it a
            # persistent worker (watch marker, hide/show instantly) instead
            # of a one-shot; --token passes the session auth token so the
            # daemon only honors THIS tray's markers (see _TOKEN above).
            exe = _frozen_helper_exe() or applog._exe_path()
            args = f"{_HELPER_FLAG} --daemon --token {_TOKEN}"
        else:
            # Source run: task runs python cursor_helper.py --daemon (no
            # admin manifest, so it stays non-elevated).
            exe = sys.executable
            helper = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "cursor_helper.py"
            )
            args = f'"{helper}" --daemon --token {_TOKEN}'
        cmd = [
            "schtasks",
            "/Create",
            "/TN",
            _TASK_NAME,
            "/TR",
            f'"{exe}" {args}',
            "/SC",
            "ONCE",
            "/ST",
            "23:59",
            "/F",
        ]
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode != 0:
            _log(f"cursor: schtasks /Create failed: {r.stderr.strip()}")
            return False
        # Write the "show|<tray_pid>" sentinel BEFORE starting the worker so
        # it has a marker to watch and stays alive (it exits when the tray
        # PID is gone). "show" also defensively un-blanks cursors left by a
        # crashed previous run; the next OSK open flips it to "hide".
        try:
            with open(_marker_path(), "w", encoding="utf-8") as f:
                f.write(f"show|{os.getpid()}|{_TOKEN}")
        except OSError:
            pass
        _start_daemon()
        return True
    except Exception as e:
        _log(f"cursor: install_helper error: {e!r}")
        return False


def _start_daemon():
    """Start the persistent non-elevated cursor daemon once (via the
    scheduled task, so it runs at normal integrity). It watches the
    marker file and applies hide/show instantly. Idempotent: if a daemon
    is already running (marker exists and is not a pure state write), we
    still re-trigger harmlessly; the daemon ignores unknown markers."""
    with suppress(Exception):
        subprocess.run(
            ["schtasks", "/Run", "/TN", _TASK_NAME],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def _trigger(mode):
    """Write the marker. The persistent non-elevated daemon (started at
    app launch) watches the marker file and applies hide/show within a
    frame or two — no per-hide exe boot, no Task Scheduler round-trip.
    The marker also carries this (elevated tray) process's PID so the
    daemon knows when to exit (when the tray quits), plus the session
    token the daemon authenticates every marker against (a foreign
    "hide|1" without the token is refused and restores the cursors)."""
    try:
        with open(_marker_path(), "w", encoding="utf-8") as f:
            f.write(f"{mode}|{os.getpid()}|{_TOKEN}")
        return True
    except Exception as e:
        _log(f"cursor: trigger error: {e!r}")
        return False


# Module-level flag so redundant hide/show calls don't re-trigger the helper.
_osk_cursor_hidden = False


def set_osk_cursor_visible(visible):
    """Hide/show the interactive session's system cursors while the OSK is
    open, via the non-elevated helper. Call with False on OSK open, True on
    close/exit. Idempotent per state (guarded by the module flag)."""
    global _osk_cursor_hidden
    if os.name != "nt":
        return
    want_hidden = not visible
    if want_hidden == _osk_cursor_hidden:
        return
    # The persistent daemon (started at install, killed at app exit) watches
    # the marker; just write the desired state. No per-hide restart — that
    # would race the daemon's startup read against the marker write.
    ok = _trigger("hide" if want_hidden else "show")
    if ok:
        _osk_cursor_hidden = want_hidden


def force_restore_cursor():
    """Un-hide the cursor unconditionally (used on exit / signal guards).
    The daemon exits on its own when this (tray) process dies, so there is
    no marker-removal race here."""
    global _osk_cursor_hidden
    if os.name == "nt":
        _trigger("show")
        _osk_cursor_hidden = False
