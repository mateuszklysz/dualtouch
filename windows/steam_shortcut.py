"""Steam non-Steam shortcut registrar + steam://forceinputappid switcher.

While the OSK is open the physical Steam Controller is consumed by triton, but
Steam Input still holds the controller and keeps injecting its bindings into
the focused app (Steam's desktop/game configs). This module mutes the
controller in Steam Input during the OSK by forcing Steam Input onto a dummy
non-Steam shortcut's config:

  * A non-Steam shortcut "DualTouch Keyboard Layer" (running cmd.exe, never
    actually launched) is registered in Steam's shortcuts.vdf (binary VDF)
    purely to host a Steam Input config.
  * The user configures that shortcut's controller config ONCE in the Steam UI
    (unbind LB/RB; everything else default).
  * On OSK open, the app calls steam://forceinputappid/<appid> — Steam Input
    switches to that config globally (desktop AND games, live, even while a
    game has focus). On OSK close, steam://forceinputappid/0 restores normal
    auto-switching (the caller puts focus back on the app first — Steam
    re-evaluates the focused window on /0 and re-initializes Steam Input if
    the app isn't focused yet).

Safety:

  * shortcuts.vdf is binary VDF. It is edited with the `vdf` package, written
    via a temp file + atomic rename, and backed up (timestamped .bak) before
    every write. Steam hot-reloads the file (observed on this machine: a
    newly written shortcut becomes visible live, no restart), so registration
    works with Steam running.
  * The AppID Steam assigns cannot be predicted (the legacy CRC32 formula
    does not match current Steam, verified against this machine's real
    entries), so the flow is: write the entry with a placeholder AppID, then
    read back the effective AppID from shortcuts.vdf. If Steam adopted the
    placeholder as-is (common — the entry becomes visible live), the
    placeholder becomes the real AppID after a short grace period; if Steam
    reassigns its own, the file changes and the new AppID is read back
    directly. The AppID is cached in a small state file next to the exe and
    re-verified against the file on every app start.
"""

import glob
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zlib
from contextlib import suppress
from datetime import datetime

from applog import log_line, user_data_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SHORTCUT_APPNAME = "DualTouch Keyboard Layer"
# The shortcut targets the RUNNING DualTouch executable (the frozen exe, or
# python launching the tray in source runs) — resolved at registration time
# from the actual process, so the library entry points at the real app, not a
# hardcoded dummy. See _shortcut_exe / _shortcut_startdir.
_STATE_FILENAME = "steam_shortcut.json"
# How long a freshly written placeholder AppID must survive unchanged in
# shortcuts.vdf before it is treated as adopted (Steam accepted it as-is —
# no restart involved). While the entry exists but is younger than this,
# cached_appid() returns None so nothing forces a possibly-wrong AppID.
_ADOPT_AFTER_SECONDS = 15.0

_STEAM_REG_KEY = r"Software\Valve\Steam"
_STEAM_REG_VALUE = "SteamPath"


def _exe_dir():
    """Directory treated as the install location (portable settings live
    here, next to the exe/script — same convention as tray.py)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _shortcut_exe():
    """The DualTouch executable the non-Steam shortcut points at, resolved
    from the RUNNING process (the frozen exe's own path, or python launching
    the tray package in source runs) — so the library entry targets the real
    app wherever it was launched from, instead of a hardcoded dummy."""
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'
    # Source run: Steam's Exe field must be an executable; point it at the
    # interpreter and carry the tray package in LaunchOptions (see
    # _shortcut_launch_options).
    return f'"{os.path.abspath(sys.executable)}"'


def _shortcut_startdir():
    """Working directory for the shortcut — the exe's own directory."""
    if getattr(sys, "frozen", False):
        return f'"{_exe_dir()}"'
    return f'"{_exe_dir()}"'


def _shortcut_launch_options():
    """LaunchOptions for the shortcut: in source runs Steam must start the
    tray package via the interpreter; frozen builds need nothing."""
    if getattr(sys, "frozen", False):
        return ""
    return "-m tray"


def state_path():
    """steam_shortcut.json lives in the per-user appdata dir (same place as
    settings.json / dualtouch.log), so the app needs no writable install
    folder. One-time migration copies an old exe-dir state file over."""
    new_path = os.path.join(user_data_dir(), _STATE_FILENAME)
    if not os.path.isfile(new_path):
        old_path = os.path.join(_exe_dir(), _STATE_FILENAME)
        if os.path.isfile(old_path):
            with suppress(OSError):
                shutil.copy2(old_path, new_path)
    return new_path


def _log(msg):
    """Append a line to dualtouch.log (windowed exe has no stdout), via the
    applog gate so logging-off never touches the log path. Never raises."""
    log_line("steam_shortcut", msg)


# ---------------------------------------------------------------------------
# Steam discovery
# ---------------------------------------------------------------------------


def find_steam_path():
    """Locate the Steam install from HKCU\\Software\\Valve\\Steam -> SteamPath.
    Returns the path (as a str) or None."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STEAM_REG_KEY) as key:
            path, _ = winreg.QueryValueEx(key, _STEAM_REG_VALUE)
        if path:
            return path.strip().replace("/", os.sep)
    except OSError:
        pass
    return None


def find_active_userdata_dir(steam_path):
    """Return the userdata directory of the most recently logged-in Steam
    user, or None. Reads Steam/config/loginusers.vdf and picks the entry
    flagged "mostrecent" (falling back to the most recently written dir)."""
    userdata = os.path.join(steam_path, "userdata")
    if not os.path.isdir(userdata):
        return None

    most_recent_id = None
    loginusers = os.path.join(steam_path, "config", "loginusers.vdf")
    if os.path.isfile(loginusers):
        try:
            with open(loginusers, encoding="utf-8", errors="replace") as f:
                text = f.read()
            m = re.search(r'"mostrecent"\s+"1"', text)
            if m:
                head = text[: m.start()]
                idm = list(re.finditer(r'"(\d+)"\s*\{', head))
                if idm:
                    most_recent_id = idm[-1].group(1)
        except (OSError, AttributeError):
            pass

    if most_recent_id is not None:
        d = os.path.join(userdata, most_recent_id)
        if os.path.isdir(d):
            return d

    # Fallback: the most recently modified user dir (Steam writes to the
    # active user's dir at exit).
    best, best_t = None, -1.0
    try:
        for name in os.listdir(userdata):
            d = os.path.join(userdata, name)
            if os.path.isdir(d) and name.isdigit():
                t = os.path.getmtime(d)
                if t > best_t:
                    best, best_t = d, t
    except OSError:
        pass
    return best


def shortcuts_vdf_path(steam_path):
    ud = find_active_userdata_dir(steam_path)
    if not ud:
        return None
    return os.path.join(ud, "config", "shortcuts.vdf")


# ---------------------------------------------------------------------------
# shortcuts.vdf read/write (binary VDF via the `vdf` package)
# ---------------------------------------------------------------------------


def _file_mark(path):
    """(size, mtime_ns) snapshot of a file — the marker used to detect that
    Steam rewrote the file between our read and our write. Returns None if
    the file can't be stat'd."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _parse_shortcuts_file(path):
    """Parse a shortcuts.vdf (binary VDF) into a dict. May raise (the
    caller reports the error)."""
    import vdf

    with open(path, "rb") as f:
        data = vdf.binary_loads(f.read())
    if not isinstance(data, dict) or "shortcuts" not in data:
        data = {"shortcuts": {}}
    return data


def _read_shortcuts(steam_path):
    """Parse shortcuts.vdf. Returns (data_dict, path, error_str_or_None,
    file_mark_or_None). error_str is None on success; data_dict is a dict
    with a "shortcuts" map keyed by "0", "1", ... (empty dict when the file
    is missing). file_mark is the (size, mtime_ns) snapshot taken as the
    file was read — `_write_shortcuts_atomic` uses it to detect a concurrent
    Steam rewrite; None when the file was missing/unreadable."""
    if importlib.util.find_spec("vdf") is None:
        return None, None, "vdf package missing", None
    path = shortcuts_vdf_path(steam_path)
    if path is None:
        return {}, None, "no userdata dir", None
    if not os.path.isfile(path):
        return {"shortcuts": {}}, path, None, None
    try:
        data = _parse_shortcuts_file(path)
    except Exception as e:
        return None, path, f"unreadable: {e!r}", None
    return data, path, None, _file_mark(path)


def _apply_entry(data):
    """Ensure the DualTouch Keyboard Layer entry is present in `data`,
    inserting it at the next free shortcut index. Returns `data` (mutated).
    Shared by the initial write and the race retry (when Steam rewrote
    shortcuts.vdf between our read and our write, our change is re-applied
    on top of the newer copy instead of clobbering it)."""
    entry = _find_entry(data)
    if entry is not None:
        # Refresh our own target fields so an entry created by an older
        # build (pointing at the dummy cmd.exe) is migrated to the real exe
        # on the next verify. Never touches Steam-owned fields.
        entry["Exe"] = _shortcut_exe()
        entry["StartDir"] = _shortcut_startdir()
        entry["LaunchOptions"] = _shortcut_launch_options()
        return data
    index = 0
    for key in data.get("shortcuts") or {}:
        try:
            index = max(index, int(key) + 1)
        except (TypeError, ValueError):
            continue
    data.setdefault("shortcuts", {})[str(index)] = {
        "appid": _placeholder_appid(),
        "appname": SHORTCUT_APPNAME,
        "Exe": _shortcut_exe(),
        "StartDir": _shortcut_startdir(),
        "icon": "",
        "ShortcutPath": "",
        "LaunchOptions": _shortcut_launch_options(),
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "sortas": "",
        "tags": {},
    }
    return data


def _shortcuts_blob_valid(blob):
    """Structural guard BEFORE the blob replaces Steam's shortcuts.vdf: it
    must re-parse as a binary VDF and contain the expected shortcut entry. A
    malformed blob (encoding bug, partial merge) must never clobber Steam's
    good file — abort instead of overwrite."""
    try:
        import vdf

        parsed = vdf.binary_loads(blob)
    except Exception:
        return False
    if not isinstance(parsed, dict) or "shortcuts" not in parsed:
        return False
    entry = _find_entry(parsed)
    return entry is not None and entry.get("appid") is not None


def _shortcuts_blob_valid_without_entry(blob):
    """Structural guard for REMOVING the DualTouch shortcut: the blob must
    re-parse as a binary VDF with a shortcuts dict, and the DualTouch entry
    must be ABSENT (it was just removed). Never clobber Steam's file with a
    malformed blob."""
    try:
        import vdf

        parsed = vdf.binary_loads(blob)
    except Exception:
        return False
    if not isinstance(parsed, dict) or "shortcuts" not in parsed:
        return False
    return _find_entry(parsed) is None


def _cap_backups(path, keep=3):
    """Remove timestamped backups of `path` beyond the newest `keep`. Only
    touches files matching our exact .bak- prefix in the same directory, and
    only after a successful write (so a failed write never deletes the
    recovery copy)."""
    try:
        names = sorted(glob.glob(path + ".bak-*"))
        for stale in names[:-keep]:
            with suppress(OSError):
                os.unlink(stale)
    except Exception:
        pass


def _write_shortcuts_atomic(
    path, data, expected_mark=None, merge=None, validate=_shortcuts_blob_valid
):
    """Back up the file, then write via temp file + atomic rename. Returns
    the backup path, or None on failure.

    Race guard (MEDIUM-1): Steam hot-reloads and rewrites shortcuts.vdf
    concurrently. If the file's (size, mtime) changed since we read it
    (`expected_mark`), the file is re-read and `merge` re-applies our change
    on top of the newer copy before the replace (best-effort, one retry). The
    replacement blob is validated before os.replace, and old .bak-* backups
    are capped after a successful write."""
    bak = None
    try:
        current = _file_mark(path)
        if (
            expected_mark is not None
            and current is not None
            and current != expected_mark
            and merge is not None
        ):
            try:
                data = merge(_parse_shortcuts_file(path))
            except Exception:
                _log(
                    "write shortcuts.vdf: file changed mid-write; re-merge "
                    "failed — retrying with the stale copy"
                )
        if os.path.isfile(path):
            bak = path + f".bak-{datetime.now():%Y%m%d-%H%M%S-%f}"
            shutil.copy2(path, bak)
        import vdf

        blob = vdf.binary_dumps(data)
        if not validate(blob):
            raise ValueError(
                "shortcuts.vdf validation failed — refusing to "
                "clobber Steam's file"
            )
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)
        except Exception:
            with suppress(OSError):
                os.unlink(tmp)
            raise
        _cap_backups(path)
        return bak
    except Exception as e:
        _log(f"write shortcuts.vdf failed: {e!r}")
        return None


# ---------------------------------------------------------------------------
# State file (cached AppID, next to the exe — portable, like settings.json)
# ---------------------------------------------------------------------------

_DEFAULT_STATE = {
    "appid": None,
    "pending": False,
    "config_hint_shown": False,
    "written_at": 0.0,
    "chord_blocked": None,
    "chord_original": None,
    "chord_bak": None,
    "chord_sha": None,
}

_SIGNED32_MIN, _SIGNED32_MAX = -(2**31), 2**31 - 1


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as f:
            data = json.load(f)
        state = dict(_DEFAULT_STATE)
        state.update({k: v for k, v in data.items() if k in _DEFAULT_STATE})
    except (OSError, json.JSONDecodeError):
        state = dict(_DEFAULT_STATE)
    # The cached appid is later used to dispatch steam://forceinputappid —
    # never trust a value that isn't a well-formed signed int32. A stale or
    # forged appid would pin Steam Input to a dead config until alt-tab
    # (LOW-2). chord_blocked is only ever a face-button name; anything else
    # must not reach a _CHORD_SLOTS lookup (a forged value would raise).
    appid = state.get("appid")
    if (
        appid is None
        or isinstance(appid, bool)
        or not isinstance(appid, int)
        or not (_SIGNED32_MIN <= appid <= _SIGNED32_MAX)
    ):
        state["appid"] = None
    if state.get("chord_blocked") not in _CHORD_SLOTS:
        state["chord_blocked"] = None
    return state


def save_state(state):
    try:
        with open(state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        _log(f"state save failed: {e!r}")


def cached_appid():
    """The verified cached AppID, or None. Returns None while the entry
    is a freshly written placeholder still inside its adoption grace
    period — a placeholder that Steam may yet reassign must never be forced
    via forceinputappid."""
    state = load_state()
    if state.get("pending"):
        return None
    return state.get("appid")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _placeholder_appid():
    """AppID for the initial write. Modern Steam does NOT match the legacy
    CRC32 formula (verified against this machine's real entries), so this is
    a placeholder: Steam may adopt it as-is (then it becomes the effective
    AppID after the grace period) or reassign its own (then the file changes
    and the new AppID is read back, see verify_or_register). Stored as
    signed int32 (the binary VDF 'i' format), matching Steam's own entries,
    which all have the 0x80000000 flag set and thus read as negative."""
    key = f"{_shortcut_exe()}{SHORTCUT_APPNAME}"
    crc = (zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF) | 0x80000000
    return crc - 2**32


def _find_entry(data):
    """The registered shortcut entry (dict) or None, by appname match."""
    for entry in (data.get("shortcuts") or {}).values():
        if entry.get("appname") == SHORTCUT_APPNAME:
            return entry
    return None


def verify_or_register(steam_path=None):
    """Re-verify the registered shortcut (reads back Steam's real AppID if
    pending) and, if absent, register it. Returns a status dict:

        {"status": "ready"|"pending"|"written"|"steam_not_found"
                  |"no_userdata"|"unreadable"|"write_failed"|"disabled",
         "appid": int|None, "needs_steam_restart": bool}

    "pending" = the entry exists but still carries our freshly written
    placeholder AppID (younger than ADOPT_AFTER_SECONDS). The caller should
    keep polling; once the placeholder has survived unchanged for the grace
    period it is treated as adopted (Steam accepted it as-is — no restart),
    and if Steam rewrites the file with its own AppID that is read back
    directly.
    """
    if steam_path is None:
        steam_path = find_steam_path()
    if not steam_path or not os.path.isdir(steam_path):
        return {
            "status": "steam_not_found",
            "appid": None,
            "needs_steam_restart": False,
        }

    data, path, err, mark = _read_shortcuts(steam_path)
    if err:
        return {
            "status": "unreadable" if data is None else "no_userdata",
            "appid": None,
            "needs_steam_restart": False,
        }
    if path is None:
        return {
            "status": "no_userdata",
            "appid": None,
            "needs_steam_restart": False,
        }

    entry = _find_entry(data)
    if entry is not None and entry.get("appid") is not None:
        # Known entry — cache its current AppID. Steam hot-reloads
        # shortcuts.vdf (observed: the entry becomes visible live, no
        # restart), so the AppID can converge either way:
        #   * Steam adopts our placeholder as-is → after a short grace
        #     period the placeholder is treated as the real AppID.
        #   * Steam reassigns and rewrites the file → the new AppID is
        #     read back here directly.
        appid = int(entry["appid"])
        state = load_state()
        state["appid"] = appid
        if appid == _placeholder_appid():
            written_at = state.get("written_at") or 0.0
            if time.time() - written_at < _ADOPT_AFTER_SECONDS:
                state["pending"] = True
                save_state(state)
                return {
                    "status": "pending",
                    "appid": appid,
                    "needs_steam_restart": False,
                }
        state["pending"] = False
        save_state(state)
        # Shortcut is known — ensure the perfected controller config is in
        # place for first-run users (no-op when already configured), and the
        # library artwork for the (adopted) appid is installed.
        install_default_controller_config(steam_path)
        install_default_artwork(steam_path, appid)
        return {
            "status": "ready",
            "appid": appid,
            "needs_steam_restart": False,
        }

    if (
        _write_shortcuts_atomic(
            path, _apply_entry(data), expected_mark=mark, merge=_apply_entry
        )
        is None
    ):
        return {
            "status": "write_failed",
            "appid": None,
            "needs_steam_restart": False,
        }
    state = load_state()
    state["appid"] = None
    state["pending"] = True
    # Timestamp for the placeholder-adoption grace period: if the file still
    # carries our placeholder this long after the write, Steam accepted it
    # as-is and it IS the effective AppID (no restart involved).
    state["written_at"] = time.time()
    save_state(state)
    # Fresh registration: install the perfected controller config so first-run
    # users don't have to configure the controller in the Steam UI.
    install_default_controller_config(steam_path)
    return {"status": "written", "appid": None, "needs_steam_restart": False}


def remove_shortcut(steam_path=None):
    """Remove the DualTouch Keyboard Layer entry from shortcuts.vdf. Returns
    True when the entry is gone, False on failure. Uses the same backup +
    validate + atomic-replace discipline as registration, with the removal-
    specific guard (blob valid, entry absent). Call with Steam CLOSED."""
    if steam_path is None:
        steam_path = find_steam_path()
    if not steam_path or not os.path.isdir(steam_path):
        return False
    data, path, err, mark = _read_shortcuts(steam_path)
    if err or data is None or path is None:
        return False
    if _find_entry(data) is None:
        return True  # already absent

    def _drop_entry(d):
        kept = [
            e
            for e in (d.get("shortcuts") or {}).values()
            if e.get("appname") != SHORTCUT_APPNAME
        ]
        return {"shortcuts": {str(i): e for i, e in enumerate(kept)}}

    return (
        _write_shortcuts_atomic(
            path,
            _drop_entry(data),
            expected_mark=mark,
            merge=_drop_entry,
            validate=_shortcuts_blob_valid_without_entry,
        )
        is not None
    )


def default_controller_config_target(steam_path):
    """Absolute path where Steam reads the DualTouch Keyboard Layer shortcut's
    Steam Controller config, or None. Mirrors the chord-config layout:
    `Steam Controller Configs/<userid>/config/<appname-lower>/...`."""
    ud = find_active_userdata_dir(steam_path)
    if not ud:
        return None
    userid = os.path.basename(ud)
    return os.path.join(
        steam_path,
        "steamapps",
        "common",
        "Steam Controller Configs",
        userid,
        "config",
        SHORTCUT_APPNAME.lower(),
        _DEFAULT_CONFIG_FILENAME,
    )


def _read_bundled_default_config():
    """The bundled perfected Steam Controller config text, or None if the
    bundle is missing/unreadable. Resolved via _bundle_dir()/data so it works
    from source and from the PyInstaller bundle without a triton import."""
    try:
        from applog import _bundle_dir

        base = os.path.join(_bundle_dir(), "data")
        with open(
            os.path.join(base, _DEFAULT_CONFIG_BUNDLE), encoding="utf-8"
        ) as f:
            return f.read()
    except Exception as e:
        _log(f"read bundled default config failed: {e!r}")
        return None


def install_default_controller_config(steam_path=None):
    """Install the bundled perfected Steam Controller config for the DualTouch
    Keyboard Layer shortcut, ONCE — only when the target file does not exist.
    An existing config (the user's own Steam-UI edits, or a cloud-synced copy)
    is never overwritten. Returns True when the file is (now) present, False
    on failure/unknown-user, None when the bundle is missing."""
    if steam_path is None:
        steam_path = find_steam_path()
    if not steam_path or not os.path.isdir(steam_path):
        return False
    target = default_controller_config_target(steam_path)
    if target is None:
        return False
    if os.path.isfile(target):
        return True  # already configured (user's own or previously installed)
    text = _read_bundled_default_config()
    if text is None:
        return None
    # The bundle's url references the ORIGINAL user's autosave path; rewrite
    # it to this user's path so the controller-UI metadata is correct (the
    # load path itself ignores url, so this is cosmetic but cheap).
    ud = find_active_userdata_dir(steam_path)
    if ud:
        userid = os.path.basename(ud)
        pattern = re.compile(r'"url"\s+"autosave://[^"]*"')
        new_url = '"url"\t\t"autosave://{}"'.format(
            os.path.join(
                steam_path,
                "steamapps",
                "common",
                "Steam Controller Configs",
                userid,
                "config",
                SHORTCUT_APPNAME.lower(),
                _DEFAULT_CONFIG_FILENAME,
            )
        )
        # Replacement via a function: the path contains backslashes that
        # re.sub would otherwise parse as escape sequences (\u, \p, ...).
        text = pattern.sub(lambda _m: new_url, text, count=1)
    if not _braces_balanced(text):
        _log("install default config: unbalanced bundle — refusing to write")
        return False
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, target)
        except Exception:
            with suppress(OSError):
                os.unlink(tmp)
            raise
        _log(f"installed default controller config -> {target}")
    except Exception as e:
        _log(f"install default controller config failed: {e!r}")
        return False
    return True


def _grid_dir(steam_path):
    """Absolute path to the active user's Steam library-art grid folder, or
    None. Non-Steam shortcut artwork lives here as `<unsigned_appid>.png`,
    `<unsigned_appid>p.png` (portrait), `<unsigned_appid>_hero.png`."""
    ud = find_active_userdata_dir(steam_path)
    if not ud:
        return None
    return os.path.join(ud, "config", "grid")


def install_default_artwork(steam_path=None, appid=None):
    """Install the bundled Steam library artwork for the DualTouch Keyboard
    Layer non-Steam shortcut, ONCE — only files Steam doesn't already have are
    written, so a user's own grid art is never overwritten. The mapping goes
    by IMAGE DIMENSIONS (the source filenames are the author's labels, not the
    grid slots):

        icon.png        500x500   -> <appid>_icon.png
        capsule.png     600x900   -> <appid>p.png      (portrait)
        wide_capsule.png 920x430  -> <appid>.png       (base landscape tile)
        hero.png        3840x1240 -> <appid>_hero.png  (hero banner)

    Grid filenames use the UNSIGNED 32-bit appid. Returns True when all four
    targets are present afterwards, False on failure/unknown-user, None when a
    bundled source is missing."""
    if steam_path is None:
        steam_path = find_steam_path()
    if not steam_path or not os.path.isdir(steam_path):
        return False
    grid = _grid_dir(steam_path)
    if grid is None:
        return False
    if appid is None:
        appid = load_state().get("appid")
    if appid is None:
        return False
    unsigned = int(appid) & 0xFFFFFFFF

    # (bundled filename, grid filename, expected-ish size for logging)
    mapping = [
        ("icon.png", f"{unsigned}_icon.png"),
        ("capsule.png", f"{unsigned}p.png"),
        ("wide_capsule.png", f"{unsigned}.png"),
        ("hero.png", f"{unsigned}_hero.png"),
    ]
    try:
        from applog import _bundle_dir

        base = os.path.join(_bundle_dir(), "data", "images")
        os.makedirs(grid, exist_ok=True)
        for src_name, dst_name in mapping:
            target = os.path.join(grid, dst_name)
            if os.path.isfile(target):
                continue  # user's own art (or previously installed) — keep
            src = os.path.join(base, src_name)
            if not os.path.isfile(src):
                return None  # bundle missing one asset — don't half-install
            fd, tmp = tempfile.mkstemp(dir=grid, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f, open(src, "rb") as s:
                    f.write(s.read())
                os.replace(tmp, target)
            except Exception:
                with suppress(OSError):
                    os.unlink(tmp)
                raise
            _log(f"installed library artwork -> {target}")
        # True when every slot is now present (all four, or some already
        # existed from the user — either way the library is complete).
        return all(os.path.isfile(os.path.join(grid, d)) for _, d in mapping)
    except Exception as e:
        _log(f"install default artwork failed: {e!r}")
        return False


# ---------------------------------------------------------------------------
# Default Steam Controller config for the DualTouch Keyboard Layer shortcut
# ---------------------------------------------------------------------------
# The shortcut's controller config directory is keyed by the LOWERCASED
# shortcut appname (Steam reads `Steam Controller Configs/<userid>/config/
# <appname>/controller_neptune.vdf` for non-Steam shortcuts — the appid is
# NOT used for the folder name). The bundled `data/cfg/controller_neptune.vdf`
# is the perfected Steam Controller config (buttons/trackpads arranged for the
# OSK), installed on first run so every user gets it without configuring the
# controller in the Steam UI. Install-once: an existing (possibly user-edited)
# config is never overwritten.
_DEFAULT_CONFIG_BUNDLE = os.path.join("cfg", "controller_neptune.vdf")
_DEFAULT_CONFIG_FILENAME = "controller_neptune.vdf"


# ---------------------------------------------------------------------------
# Guide Button Chord blocker
# ---------------------------------------------------------------------------

# Steam's "Guide Button Chord" layout (the Steam-button + button chords that
# run while the OSK is closed) lives in a per-user config file. When a chord
# button is unbound there, Steam treats the Guide press as "open the Steam
# menu" ("Guide button sent to JS" in controller_ui.txt). When the button has
# ANY binding, Steam consumes the press ("Guide button skipped due to
# chording") and does NOT open the menu.
#
# DualTouch opens the OSK on its own Steam+<chord> (read from the raw HID,
# works elevated). To stop Steam from ALSO opening its menu on that same
# press, we write a dead binding ("controller_action empty_binding" — Steam's
# own "consume but do nothing" action, seen in the shipped chord base
# template) into the selected chord button's slot. The original activators
# are saved in the state file and restored when the user switches chords, so
# a real Steam binding the user makes later is preserved.
#
# The file is TEXT VDF (not binary like shortcuts.vdf) and MUST NOT be
# round-tripped through the vdf package: repeated "group" keys collapse on
# load, which would corrupt Steam's config. We therefore patch the one button
# block with exact string surgery, backing up the whole file first.
_CHORD_CONFIG_SUBDIR = os.path.join(
    "steamapps", "common", "Steam Controller Configs"
)
_CHORD_APPID_DIR = "443510"  # Steam's Guide Button Chord config appid
_CHORD_FILENAME = "controller_triton.vdf"
# Steam's shipped chord template, used to create the per-user config on PCs
# where Steam never autosaved one (a fresh install falls back to this file).
_CHORD_BASE_TEMPLATE = os.path.join(
    "controller_base", "chord_triton.vdf"
)

# Guide-chord button slot name -> Steam input key in the four_buttons group.
_CHORD_SLOTS = {
    "X": "button_x",
    "Y": "button_y",
    "A": "button_a",
    "B": "button_b",
}

# The dead binding text written into the chord button's activators. Matches
# the surrounding indentation of the existing file (tabs). Ends WITHOUT a
# trailing newline: the file's own newline after the closing brace follows.
_DEAD_ACTIVATORS = (
    '"activators"\n'
    "\t\t\t\t{\n"
    '\t\t\t\t\t"Full_Press"\n'
    "\t\t\t\t\t{\n"
    '\t\t\t\t\t\t"bindings"\n'
    "\t\t\t\t\t\t{\n"
    '\t\t\t\t\t\t\t"binding"\t\t"controller_action empty_binding"\n'
    "\t\t\t\t\t\t}\n"
    "\t\t\t\t\t}\n"
    "\t\t\t\t}"
)


def chord_config_path(steam_path):
    """Absolute path to Steam's Guide Button Chord config for the active
    user, or None. Mirrors the per-user layout under
    steamapps/common/Steam Controller Configs/<userid>/config/."""
    ud = find_active_userdata_dir(steam_path)
    if not ud:
        return None
    userid = os.path.basename(ud)
    return os.path.join(
        steam_path,
        _CHORD_CONFIG_SUBDIR,
        userid,
        "config",
        _CHORD_APPID_DIR,
        _CHORD_FILENAME,
    )


def _chord_backup_path(path):
    """Timestamped .bak for the chord config, like shortcuts.vdf backups.
    Includes microsecond precision so two writes within the same second
    (block + an immediate restore) can't collide and overwrite the stored
    `chord_bak` reference with the wrong content."""
    return path + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _braces_balanced(text):
    """True if `text` has balanced braces — the structural sanity guard
    before any hand-rolled chord-config edit is written to a file Steam
    owns. A malformed edit must never clobber Steam's good file."""
    depth = 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _activators_text_plausible(text):
    """Sanity check for a chord-slot activators block coming from the
    user-writable state file (MEDIUM-2): must look like Steam's "activators"
    dict — balanced, starts with the key, and contains a "bindings" sub-dict
    with a "binding". Refuses arbitrary injected text."""
    if not isinstance(text, str) or not text.startswith('"activators"'):
        return False
    if not _braces_balanced(text):
        return False
    return '"bindings"' in text and '"binding"' in text


def _slot_activators_range(text, slot):
    """Locate a chord-button slot's "activators" dict in a chord-config text
    with exact brace matching. Returns (act_start, act_end, block_start,
    block_end) as absolute offsets into `text`, or None if the slot key, its
    block, or its activators dict can't be found (or braces never balance —
    the file is not well-formed and must not be edited)."""
    key = f'"{slot}"'
    start = text.find(key)
    if start < 0:
        return None
    brace = text.find("{", start)
    if brace < 0:
        return None
    block_start = start
    depth = 0
    i = brace
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                block_end = i + 1
                break
        i += 1
    else:
        return None
    block = text[block_start:block_end]
    act_start = block.find('"activators"')
    if act_start < 0:
        return None
    act_brace = block.find("{", act_start)
    if act_brace < 0:
        return None
    depth = 0
    i = act_brace
    while i < len(block):
        if block[i] == "{":
            depth += 1
        elif block[i] == "}":
            depth -= 1
            if depth == 0:
                act_end = i + 1
                return (
                    block_start + act_start,
                    block_start + act_end,
                    block_start,
                    block_end,
                )
        i += 1
    return None


def _write_chord_blocked(path, slot, backup=None):
    """Patch ONE chord-button slot in Steam's guide-chord config with the
    dead binding, preserving every other byte. Returns True on success, False
    on any failure (missing file, unknown slot, unbalanced edited result,
    write error). `backup` (optional) is a backup path the caller already
    prepared; a fresh timestamped one is created when not given."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False

    rng = _slot_activators_range(text, slot)
    if rng is None:
        return False
    act_start, act_end, block_start, block_end = rng
    block = text[block_start:block_end]
    rel_act_start = act_start - block_start
    rel_act_end = act_end - block_start

    # Rebuild the activators dict with the dead binding, keeping the slot's
    # surrounding indentation (tabs before "activators").
    indent = block[:rel_act_start].rsplit("\n", 1)[-1]
    dead = _DEAD_ACTIVATORS.replace("				", indent)
    new_block = block[:rel_act_start] + dead + block[rel_act_end:]
    new_text = text[:block_start] + new_block + text[block_end:]

    # Parse-guard: never write an edited result that isn't balanced — a
    # botched edit must not clobber Steam's file (MEDIUM-1).
    if not _braces_balanced(new_text):
        _log("write chord blocker: unbalanced result — refusing to write")
        return False

    # Backup then write atomically; cap old backups after a successful write.
    try:
        if backup is None:
            backup = _chord_backup_path(path)
        shutil.copy2(path, backup)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
            os.replace(tmp, path)
        except Exception:
            with suppress(OSError):
                os.unlink(tmp)
            raise
        _cap_backups(path)
    except Exception as e:
        _log(f"write chord blocker failed: {e!r}")
        return False
    return True


def _trusted_chord_activators(path, slot, backup, original, expected_sha):
    """Resolve the restoration text for a chord slot from TRUSTED sources
    only (MEDIUM-2). Returns the activators block, or None when no trusted
    source yields one (the caller must abort the restore without writing):

      1. `backup` — the .bak the blocker wrote inside Steam's own config
         dir (attacker-immutable; the slot's activators are extracted from
         it, matching what Steam wrote).
      2. `original` — the state-file round-trip, used ONLY if it passes a
         balance/structure sanity check AND matches `expected_sha` (the
         SHA-256 recorded at block time)."""
    if backup and os.path.isfile(backup):
        try:
            if os.path.dirname(os.path.abspath(backup)) != os.path.dirname(
                os.path.abspath(path)
            ):
                raise ValueError("backup outside chord config dir")
            with open(backup, encoding="utf-8", errors="replace") as f:
                bak_text = f.read()
            rng = _slot_activators_range(bak_text, slot)
            if rng is not None:
                block = bak_text[rng[2] : rng[3]]
                act = bak_text[rng[0] : rng[1]]
                if _braces_balanced(block) and _activators_text_plausible(act):
                    return act
        except (OSError, ValueError) as e:
            _log(
                f"restore chord slot: backup unusable ({e!r}) — "
                "falling back to hash-verified original"
            )
    if original is None:
        return None
    if not _activators_text_plausible(original):
        return None
    if (
        expected_sha is not None
        and hashlib.sha256(original.encode("utf-8")).hexdigest()
        != expected_sha
    ):
        return None
    return original


def _restore_chord_slot(path, slot, original, backup=None, expected_sha=None):
    """Restore a chord slot's activators to the pre-block text. Only touches
    that one slot.

    The restoration text round-trips through the user-writable state file,
    so it is NEVER used verbatim: the preferred source is the .bak the
    blocker wrote inside Steam's config dir; `original` is accepted only when
    it passes a balance/structure sanity check AND matches the SHA-256
    recorded at block time. Anything else aborts the restore without writing.
    Returns True on success, False on refusal/failure."""
    if original is None and backup is None:
        return True
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    rng = _slot_activators_range(text, slot)
    if rng is None:
        # The slot is absent from the current config — there is no blocked
        # binding left to undo (Steam's base template has no button_a). The
        # previous block is already gone; treat the restore as done.
        return True
    act_start, act_end, block_start, block_end = rng

    restored = _trusted_chord_activators(
        path, slot, backup, original, expected_sha
    )
    if restored is None:
        _log(
            "restore chord slot: refusing untrusted restoration text "
            f"for slot {slot}"
        )
        return False

    block = text[block_start:block_end]
    rel_act_start = act_start - block_start
    rel_act_end = act_end - block_start
    indent = block[:rel_act_start].rsplit("\n", 1)[-1]
    filled = restored.replace("				", indent)
    new_block = block[:rel_act_start] + filled + block[rel_act_end:]
    new_text = text[:block_start] + new_block + text[block_end:]

    # Parse-guard on the edited result before it replaces Steam's file.
    if not _braces_balanced(new_text):
        _log("restore chord slot: unbalanced result — refusing to write")
        return False

    try:
        shutil.copy2(path, _chord_backup_path(path))
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
            os.replace(tmp, path)
        except Exception:
            with suppress(OSError):
                os.unlink(tmp)
            raise
        _cap_backups(path)
    except Exception as e:
        _log(f"restore chord slot failed: {e!r}")
        return False
    return True


def _insert_chord_slot(text, slot):
    """Insert a face-button input block carrying the dead binding into a chord
    config that has no such slot at all (Steam's base template lacks button_a).
    The block is anchored on an existing face-button slot to find the
    four_buttons group's `inputs` dict. Returns the new text, or None when the
    anchor or the inputs brace can't be located."""
    if f'"{slot}"' in text:
        return text
    for anchor in _CHORD_SLOTS.values():
        rng = _slot_activators_range(text, anchor)
        if rng is None:
            continue
        block_start = rng[2]
        inp = text.rfind('"inputs"', 0, block_start)
        if inp < 0:
            return None
        brace = text.find("{", inp)
        if brace < 0 or brace > block_start:
            return None
        at = brace + 1
        if text[at : at + 1] != "\n":
            return None
        block = (
            "\n"
            '\t\t\t"{slot}"\n'
            "\t\t\t{{\n"
            '\t\t\t\t"activators"\n'
            "\t\t\t\t{{\n"
            '\t\t\t\t\t"Full_Press"\n'
            "\t\t\t\t\t{{\n"
            '\t\t\t\t\t\t"bindings"\n'
            "\t\t\t\t\t\t{{\n"
            '\t\t\t\t\t\t\t"binding"\t\t"controller_action empty_binding"\n'
            "\t\t\t\t\t\t}}\n"
            "\t\t\t\t\t}}\n"
            "\t\t\t\t}}\n"
            "\t\t\t}}".format(slot=slot)
        )
        return text[:at] + block + text[at:]
    return None


def _ensure_chord_config(steam_path, path, slot):
    """Make sure Steam's per-user guide-chord config exists AND has a patchable
    slot for `slot`. Steam does not always create the per-user file: a fresh
    PC falls back to the shipped base template, which left `block_open_chord`
    with nothing to patch (silently failing to suppress the Steam menu).

    When the file is missing, this creates it from Steam's base template with
    the autosave header (progenitor/url) Steam itself writes for per-user
    configs. When the config (created or existing) has no slot for `slot` —
    the base template has no button_a — a dead-binding slot is inserted, with
    a backup taken first when the file is Steam's own. Returns True when the
    file is ready to patch, False on any failure."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        created = False
        modified = False
    except OSError:
        base = os.path.join(steam_path, _CHORD_BASE_TEMPLATE)
        try:
            with open(base, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            _log(f"guide chord: base template unreadable ({e!r})")
            return False
        if not _braces_balanced(text):
            _log("guide chord: base template unbalanced — refusing to copy")
            return False
        # The header Steam writes for autosaved per-user configs. The load
        # path ignores url/progenitor (Steam reads the file by path), but the
        # controller UI displays them.
        marker = '"creator"'
        idx = text.find(marker)
        if idx < 0:
            _log("guide chord: base template malformed (no creator line)")
            return False
        nl = text.find("\n", idx)
        header = (
            "\t\"progenitor\"\t\t\"local://controller_base/chord_triton.vdf\"\n"
            "\t\"url\"\t\t\"autosave://" + path + "\"\n"
        )
        text = text[: nl + 1] + header + text[nl + 1 :]
        created = True
        modified = True

    # A slot must exist for _slot_activators_range/_write_chord_blocked to
    # find it. The base template has no button_a — insert one with the dead
    # binding, backing up first when the file is Steam's own (not ours).
    if _slot_activators_range(text, slot) is None:
        if not created:
            try:
                shutil.copy2(path, _chord_backup_path(path))
            except OSError as e:
                _log(f"guide chord: backup failed before slot insert ({e!r})")
                return False
        inserted = _insert_chord_slot(text, slot)
        if inserted is None:
            _log(f"guide chord: cannot add missing slot {slot}")
            return False
        text = inserted
        modified = True

    if not _braces_balanced(text):
        _log("guide chord: unbalanced config — refusing to write")
        return False

    if modified:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                os.replace(tmp, path)
            except Exception:
                with suppress(OSError):
                    os.unlink(tmp)
                raise
            if created:
                _log(
                    f"guide chord: created per-user config from base template -> {path}"
                )
            else:
                _log(f"guide chord: inserted missing slot {slot} -> {path}")
        except Exception as e:
            _log(f"guide chord: create per-user config failed: {e!r}")
            return False
    return True


def block_open_chord(steam_path=None, chord="X"):
    """Ensure Steam consumes the Guide Button Chord for `chord` (the DualTouch
    OSK-open chord) so it does NOT open Steam's menu on the same press. Writes
    a dead binding into Steam's guide-chord config (backed up first) and
    remembers the original activators in the state file; switching chords
    restores the previous slot first. Idempotent and safe to call on every
    app start. Returns True when the config is (or was already) blocked."""
    if chord not in _CHORD_SLOTS:
        return True  # nothing to block for non-face buttons
    if steam_path is None:
        steam_path = find_steam_path()
    if not steam_path or not os.path.isdir(steam_path):
        return True  # Steam not found — nothing to patch, app won't open OSK anyway
    path = chord_config_path(steam_path)
    if path is None:
        return True  # unknown user — nothing to patch (no-op)
    slot = _CHORD_SLOTS[chord]

    # Steam does not always create the per-user chord config: on a fresh PC
    # it falls back to the shipped base template, which used to leave nothing
    # to patch here (the Steam menu kept opening on the chord). Ensure the
    # file exists — creating it from the base template when missing — so the
    # dead binding can be written.
    if not _ensure_chord_config(steam_path, path, slot):
        return True  # unreadable/uncreatable — nothing we can block (no-op)

    state = load_state()
    prev = state.get("chord_blocked")
    prev_orig = state.get("chord_original")

    # Restore the previously-blocked slot before blocking the new one, so
    # switching chords doesn't leave the old button silently dead. The
    # restore only trusts the block-time backup or the hash-verified
    # original (never the state file verbatim).
    if prev and prev != chord and prev_orig is not None:
        if not _restore_chord_slot(
            path,
            _CHORD_SLOTS[prev],
            prev_orig,
            backup=state.get("chord_bak"),
            expected_sha=state.get("chord_sha"),
        ):
            return False
        state["chord_blocked"] = None
        state["chord_original"] = None
        state["chord_bak"] = None
        state["chord_sha"] = None

    # Capture the current activators of the target slot as the new original,
    # then write the dead binding.
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    rng = _slot_activators_range(text, slot)
    if rng is None:
        return False
    original = text[rng[0] : rng[1]]

    # Backup the file inside Steam's dir BEFORE the block: this .bak is what
    # a later restore prefers (attacker-immutable, matches what Steam wrote).
    bak = None
    try:
        bak = _chord_backup_path(path)
        shutil.copy2(path, bak)
    except OSError:
        bak = None

    if not _write_chord_blocked(path, slot, backup=bak):
        return False

    state["chord_blocked"] = chord
    state["chord_original"] = original
    state["chord_bak"] = bak
    state["chord_sha"] = hashlib.sha256(original.encode("utf-8")).hexdigest()
    save_state(state)
    _log(f"guide chord blocked: Steam+{chord} consumed (no Steam menu)")
    return True


# ---------------------------------------------------------------------------
# Runtime switching
# ---------------------------------------------------------------------------


def force_appid(appid):
    """Dispatch steam://forceinputappid/<appid> (0 restores auto-switching).
    The tray exe runs elevated, so the URL is dispatched through the
    existing (non-elevated) explorer.exe shell instance — launching the
    steam:// handler from an elevated process would otherwise hand the URL
    to an elevated Steam. Returns True if the dispatch was launched. The
    exact URL is logged either way so a failed switch is confirmable in
    dualtouch.log.

    The URL carries the UNSIGNED 32-bit AppID: Steam addresses non-Steam
    shortcuts by their unsigned value (controller_ui.log shows e.g. appid
    2838447526 for shortcut appid -1456519770), and a signed negative URL
    is silently ignored (verified on this machine: "URL Forcing" appears
    in controller_ui.log for positive appids only)."""
    url = f"steam://forceinputappid/{int(appid) & 0xFFFFFFFF}"
    try:
        subprocess.Popen(
            ["explorer.exe", url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _log(f"dispatched {url}")
        return True
    except OSError:
        try:
            os.startfile(url)
            _log(f"dispatched {url} (startfile fallback)")
            return True
        except OSError as e:
            _log(f"steam url dispatch failed ({url}): {e!r}")
            return False
