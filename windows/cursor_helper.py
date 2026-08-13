"""Non-elevated cursor helper for DualTouch. The tray re-invokes THIS
binary (the same exe) with --cursor-helper via a scheduled task, so it
runs at the interactive user's integrity level (non-elevated). It reads a
marker file (hide|show) written by the tray, does the cursor work on the
interactive session (which the elevated tray cannot touch), and clears it.

SECURITY: the marker is authenticated. The tray generates a per-session
random token, bakes it into this helper's task command line (--token ...)
and stamps every marker write with it. A same-user process that writes
"hide|1" (PID 1 = System) or any token-less/garbage marker is refused and
the cursors are restored — a marker can only be honored when it carries
the exact session token AND names a live process running the same binary
as this helper. A stale/missing marker (no valid write for a few seconds)
also restores the cursors and exits, so a blanked-cursor state can never
persist.

Usage (via schtasks /Run): DualTouch-windows.exe --cursor-helper --daemon --token <hex>
"""

import ctypes
import os
import secrets
import sys
from contextlib import suppress

_OCR_IDS = (
    32512,
    32513,
    32514,
    32515,
    32516,
    32642,
    32643,
    32644,
    32645,
    32646,
    32648,
    32649,
    32650,
)
_SPI_SETCURSORS = 0x0057
_SPIF_SENDCHANGE = 0x0002
# A valid marker must have been seen within this window, else the daemon
# restores the cursors and exits (self-termination on stale markers).
_STALE_TIMEOUT = 5.0
# A PRESENT but unauthenticated marker (attacker's forged "hide|1", or a
# torn read mid-rewrite) fails closed much faster than a missing marker —
# still a short grace so a transient torn read doesn't kill the daemon.
_INVALID_TIMEOUT = 1.0
# The session token this helper was launched with (argv --token), or None
# (unknown → refuse to act: fail closed).
_TOKEN = None
# The marker + log live in the per-user appdata dir, shared with the
# (elevated) tray via cursor_ctrl, so the helper and tray always agree
# regardless of where the exe lives. Same user, so %APPDATA% is accessible
# from the non-elevated scheduled task.
try:
    from applog import user_data_dir

    _BASE = user_data_dir()
except Exception:
    if getattr(sys, "frozen", False):
        _BASE = os.path.dirname(os.path.abspath(sys.executable))
    else:
        _BASE = os.path.dirname(os.path.abspath(__file__))
_MARKER = os.path.join(_BASE, "cursor_action.txt")


def _log(msg):
    try:
        from applog import log_line

        # Honor the tray's logging toggle: no log writes when the user turned
        # logging off (log_line gates it). applog defaults off; _sync_logging_toggle
        # publishes the persisted setting at startup.
        log_line("cursor_helper", msg)
    except Exception:
        pass


def _sync_logging_toggle():
    """Publish the tray's persisted logging setting into this process's
    applog gate so the helper's log writes follow the tray toggle exactly
    (the helper is a separate process, so it can't receive the in-process
    set_logging_enabled call)."""
    try:
        import json

        from applog import set_logging_enabled

        p = os.path.join(_BASE, "settings.json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                set_logging_enabled(
                    bool(json.load(f).get("logging_enabled", False))
                )
    except Exception:
        pass


def _load_token_from_args(argv):
    """Read the session auth token from the command line (--token <hex>),
    as baked in by cursor_ctrl.install_helper when it registered the
    scheduled task. Refuses to run when it is absent."""
    global _TOKEN
    try:
        i = argv.index("--token")
        _TOKEN = (argv[i + 1].strip() or None) if i + 1 < len(argv) else None
    except (ValueError, IndexError):
        _TOKEN = None


def _parse_marker(text, token):
    """Validate a marker line "mode|pid|token" against the session token.

    Returns (mode, pid) when the marker is genuine (exact token match,
    pid a real process id > 1, mode one of hide/show); None on ANY
    malformed / unauthenticated marker. Pure function (tested headless)."""
    if not text or not token:
        return None
    parts = text.split("|")
    if len(parts) != 3:
        return None
    mode, pid_str, tok = parts
    if mode not in ("hide", "show"):
        return None
    try:
        pid = int(pid_str)
    except ValueError:
        return None
    # PID 1 = System (always "alive"): the classic blank-forever DoS is a
    # marker naming PID 1. Refuse every pid <= 1.
    if pid <= 1:
        return None
    if not secrets.compare_digest(tok, token):
        return None
    return mode, pid


class _IconInfo(ctypes.Structure):
    _fields_ = [
        ("fIcon", ctypes.c_bool),
        ("xHotspot", ctypes.c_uint),
        ("yHotspot", ctypes.c_uint),
        ("hbmMask", ctypes.c_void_p),
        ("hbmColor", ctypes.c_void_p),
    ]


def _create_blank_cursor():
    try:
        u = ctypes.windll.user32
        g = ctypes.windll.gdi32
        g.CreateBitmap.restype = ctypes.c_void_p
        g.CreateBitmap.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_void_p,
        ]
        g.SetBitmapBits.restype = ctypes.c_long
        g.SetBitmapBits.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
        ]
        w = h = 32
        row_bytes = (w + 31) // 32 * 4
        hbm = g.CreateBitmap(w, h * 2, 1, 1, None)
        if not hbm:
            return None
        buf = (ctypes.c_ubyte * (row_bytes * h * 2))()
        for r in range(h):
            for b in range(row_bytes):
                buf[r * row_bytes + b] = 0xFF
        g.SetBitmapBits(hbm, len(buf), buf)
        u.CreateIconIndirect.restype = ctypes.c_void_p
        u.CreateIconIndirect.argtypes = [ctypes.POINTER(_IconInfo)]
        ii = _IconInfo()
        ii.fIcon = False
        ii.hbmMask = hbm
        cur = u.CreateIconIndirect(ctypes.byref(ii))
        g.DeleteObject(ctypes.c_void_p(hbm))
        return cur
    except Exception:
        return None


def _hide():
    u = ctypes.windll.user32
    u.CopyIcon.restype = ctypes.c_void_p
    u.CopyIcon.argtypes = [ctypes.c_void_p]
    u.SetSystemCursor.restype = ctypes.c_bool
    u.SetSystemCursor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    blank = _create_blank_cursor()
    if not blank:
        _log("hide: blank cursor creation FAILED")
        return 1
    hidden = 0
    for cid in _OCR_IDS:
        copy = u.CopyIcon(blank)
        if copy and u.SetSystemCursor(copy, cid):
            hidden += 1
    with suppress(Exception):
        u.DestroyCursor(ctypes.c_void_p(blank))
    _log(f"hide: replaced {hidden} system cursors")
    return 0 if hidden > 0 else 1


def _show():
    u = ctypes.windll.user32
    u.SystemParametersInfoW.restype = ctypes.c_bool
    u.SystemParametersInfoW.argtypes = [
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    ok = u.SystemParametersInfoW(_SPI_SETCURSORS, 0, None, _SPIF_SENDCHANGE)
    _log(f"show: SPI_SETCURSORS -> {ok}")
    return 0 if ok else 1


def _read_marker():
    try:
        with open(_MARKER, encoding="utf-8") as f:
            return f.read().strip().lstrip(chr(0xFEFF)).strip()
    except OSError:
        return None


def _apply_mode(mode):
    """Hide/show per a marker value. Returns the process exit code."""
    if mode == "hide":
        return _hide()
    if mode == "show":
        return _show()
    return 1


def _daemon_loop():
    """Persistent non-elevated helper: watch the marker file and act
    instantly (no per-hide exe boot). The tray writes "hide|pid|token" /
    "show|pid|token"; we apply the state and keep watching. We exit — and
    restore the cursors — when the tray's PID is gone, when the marker
    carries no valid token, or when no valid marker has been seen for
    STALE_TIMEOUT seconds, so cursors can never stay blanked forever."""
    import time

    _log(
        f"daemon start elevated={bool(ctypes.windll.shell32.IsUserAnAdmin())} token_set={bool(_TOKEN)}"
    )
    if not _TOKEN:
        # No way to authenticate markers — fail closed: restore + exit.
        _log("daemon refuse: no session token (restore + exit)")
        _show()
        return 1
    last = None
    stale_deadline = None  # monotonic deadline after the marker went bad
    while True:
        raw = _read_marker()
        now = time.monotonic()
        parsed = _parse_marker(raw, _TOKEN) if raw else None
        if parsed is None:
            # Marker missing/unreadable, or present but not carrying our
            # token. The tray NEVER removes the marker (it only overwrites
            # it), so this is anomalous — an attacker, a stale task from an
            # old token, or a crashed tray. Fail closed: restore the cursors
            # and exit rather than leave them blanked forever. A present
            # marker with a bad token (a forged "hide|1") is an attack and
            # times out much faster than a merely missing one.
            timeout = _STALE_TIMEOUT if raw is None else _INVALID_TIMEOUT
            if stale_deadline is None:
                stale_deadline = now + timeout
            elif now >= stale_deadline:
                _log("daemon bad marker: restore + exit")
                _show()
                return 0
            time.sleep(0.02)
            continue
        stale_deadline = None
        mode, parent = parsed
        if not _pid_is_trusted(parent):
            _log(f"daemon exit: marker pid {parent} not a trusted tray")
            _show()
            return 0
        if mode != last:
            last = mode
            _apply_mode(mode)
        time.sleep(0.02)


def _pid_is_trusted(pid):
    """True only if `pid` is a live process running the SAME binary as this
    helper (the tray's python/DualTouch exe). A marker is honored only when
    it names its own owner process — a random PID, PID 1 (System), or a
    dead PID is refused even if the token somehow matched. psutil reads the
    process table WITHOUT a handle, so it works across the elevation
    boundary (the tray runs elevated; we don't)."""
    if pid <= 1:
        return False
    try:
        import psutil

        p = psutil.Process(int(pid))
        if not p.is_running():
            return False
        name = (p.name() or "").lower()
        return name == os.path.basename(sys.executable).lower()
    except Exception:
        return False  # unknown: fail closed (don't honor the marker)


def main():
    import sys as _sys

    _load_token_from_args(_sys.argv)
    _sync_logging_toggle()
    if "--daemon" in _sys.argv:
        return _daemon_loop()
    if not _TOKEN:
        _log("refuse: no session token; nothing to do")
        return 1
    raw = _read_marker()
    if raw is None:
        _log("marker file missing; nothing to do")
        return 0
    parsed = _parse_marker(raw, _TOKEN)
    if parsed is None:
        _log("marker rejected (missing/invalid token or format)")
        return 1
    mode, parent = parsed
    if not _pid_is_trusted(parent):
        _log(f"marker rejected (pid {parent} not the tray)")
        return 1
    _log(
        f"invoked mode={mode} elevated={bool(ctypes.windll.shell32.IsUserAnAdmin())}"
    )
    rc = _apply_mode(mode)
    with suppress(OSError):
        os.remove(_MARKER)
    return rc


if __name__ == "__main__":
    sys.exit(main())
