"""Per-user "launch at logon" via a Task Scheduler task running elevated.

Why a scheduled task instead of a Startup-folder shortcut:
  * The tray requires elevation (UIPI typing into games/Big Picture) but the
    exe has NO admin manifest (it must also run non-elevated as the cursor
    helper). A Startup-folder .lnk launches the exe non-elevated, which then
    self-elevates via ShellExecute "runas" — showing a UAC prompt at logon
    that must be clicked, so autostart silently fails if nobody is there.
  * A scheduled task with "/RL HIGHEST" starts the target ALREADY elevated,
    with no UAC prompt at all — logon autostart just works.
  * The task triggers at LOGON and is re-created with /F on every apply so
    the exe path stays current (portable exe moved -> re-register).

Implementation notes:
  * Pure `schtasks` via subprocess (same mechanism the cursor helper uses,
    see cursor_ctrl.py), with CREATE_NO_WINDOW so no console flashes.
  * Only touches the Task Scheduler; no registry Run key, no Startup folder.
"""

import os
import subprocess
import sys
from contextlib import suppress

# Name of the scheduled task that starts the elevated tray at logon.
TASK_NAME = "DualTouchAutostart"

# Legacy autostart mechanisms older builds used; we remove them whenever
# autostart state is applied so migrating users aren't left with a stale
# Startup .lnk (which would double-launch alongside the task) or a
# Defender-flagged HKCU\...\Run value.
_LEGACY_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_LEGACY_RUN_NAME = "SteamControllerKeyboard"
_LEGACY_LNK = "DualTouch.lnk"


def _is_frozen():
    return getattr(sys, "frozen", False)


def _startup_dir():
    """Absolute path to the user's Start Menu Startup folder, or None (used
    to clean up the legacy .lnk)."""
    import ctypes

    try:
        buf = ctypes.create_unicode_buffer(260)
        hr = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0007, None, 0, buf)
        if hr == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(
            appdata,
            "Microsoft",
            "Windows",
            "Start Menu",
            "Programs",
            "Startup",
        )
    return None


def _command():
    """(executable, arguments) the scheduled task should run. Frozen: the EXE
    itself. From source: the current interpreter running the tray package."""
    if _is_frozen():
        return os.path.abspath(sys.executable), ""
    return os.path.abspath(sys.executable), "-m tray"


def _task_commandline():
    """The /TR value: a quoted command line with any arguments."""
    exe, args = _command()
    if args:
        return f'"{exe}" {args}'
    return f'"{exe}"'


def _schtasks(args):
    """Run schtasks quietly (no console flash). Returns (returncode, stderr)."""
    r = subprocess.run(
        ["schtasks"] + args,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return r.returncode, r.stderr.strip()


def enable():
    """Create (or refresh) the logon scheduled task running elevated. Returns
    True on success."""
    rc, err = _schtasks(
        [
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            _task_commandline(),
            "/SC",
            "ONLOGON",
            "/RL",
            "HIGHEST",
            "/F",
        ]
    )
    return rc == 0


def disable():
    """Remove the scheduled task. Returns True if it is gone afterwards."""
    rc, _ = _schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    if rc == 0:
        return True
    # Already absent (task never created) is also a success.
    rc2, _ = _schtasks(["/Query", "/TN", TASK_NAME])
    return rc2 != 0


def _remove_legacy_run_key():
    """Delete the old HKCU\\...\\Run value if present (best effort)."""
    try:
        import winreg

        with (
            suppress(FileNotFoundError),
            winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _LEGACY_RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key,
        ):
            winreg.DeleteValue(key, _LEGACY_RUN_NAME)
    except OSError:
        pass


def _remove_legacy_lnk():
    """Delete the old Startup-folder shortcut if present (best effort), so it
    can't double-launch alongside the scheduled task."""
    d = _startup_dir()
    if d:
        path = os.path.join(d, _LEGACY_LNK)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass


def set_enabled(enabled):
    """Apply the desired autostart state and always clean up the legacy
    mechanisms (Run key + Startup .lnk) so migrating users aren't double-
    launched or tripping the persistence detection."""
    _remove_legacy_run_key()
    _remove_legacy_lnk()
    return enable() if enabled else disable()
