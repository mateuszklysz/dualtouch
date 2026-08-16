"""settings.json defaults, persistence, and Steam Controller tuning maps.

Every setting key is defined in DEFAULT_SETTINGS with a typed default; the
tray menu edits them and they persist next to the exe. The sc_* keys are
Steam Controller OSK tuning values, hand-editable in settings.json.
"""

import json
import os
import re

from applog import _exe_dir, user_data_dir
from steamcontroller import SCButtons
from triton import diacritics
from triton.applook import OSK_SIZES

SETTINGS_FILENAME = "settings.json"
STEAM_PROC_NAME = "steam.exe"

DEFAULT_SETTINGS = {
    # Default OFF: a freshly downloaded, unsigned binary that silently adds
    # itself to autostart on first launch is exactly the "drive-by persistence"
    # pattern Defender's ML flags. Users opt in via the tray "Start with
    # Windows" toggle, which is a deliberate, user-initiated action.
    "start_with_windows": False,
    # Write dualtouch.log (tray "Startup -> Enable Logging"). Off by default:
    # the log is for diagnosing problems, not for everyday runs.
    "logging_enabled": False,
    # Steam keyboard-layer switching: when enabled (default), the OSK asks
    # Steam Input to switch to the "DualTouch Keyboard Layer" shortcut's
    # controller config (LB/RB muted, everything else default) while the
    # keyboard is open, via steam://forceinputappid/<appid> (and back to
    # auto with /0 on close). The shortcut is registered into Steam's
    # shortcuts.vdf automatically; see steam_shortcut.py.
    "steam_kbd_layer": True,
    # Global SC haptics switch: gates the on-screen-keyboard click feedback AND
    # the desktop rumble (volume-chord ticks, L2/R2 pull buzzes). No tray UI —
    # hand-edit in settings.json.
    "rumble_enabled_sc": True,
    # Steam-keyboard click sound on each OSK key press (plays
    # steamui/sounds/deck_ui_typing.wav from the Steam install — the same
    # sound Steam's own on-screen keyboard uses). No tray UI — hand-edit in
    # settings.json.
    "key_sound_enabled_sc": True,
    # "Block SteamInput Steam Controller grab": open the physical Steam Controller
    # HID exclusively so Steam can't read it (no Steam Input / forced lizard while
    # we hold it). Must be enabled before Steam opens the controller to win the
    # grab.
    "block_sc_hid": False,
    # Name of the selected on-screen-keyboard skin: Steam's OSK themes resolve
    # from the Steam install at runtime; "Gruvbox" is the bundled original.
    # Unlike the others this is a string, not a bool — see the type-aware
    # coercion in _load_settings. Applied when the OSK next opens.
    "skin": "Gruvbox",
    # OSK transparency level (tray "Keyboard Skin → Transparent" submenu): one of
    # "off"/"low"/"medium"/"high". Renders the keyboard with no background and
    # translucent keys/text over the desktop, at three global-opacity levels.
    "osk_transparency": "off",
    # OSK window size (tray "Keyboard Skin → Size" submenu): "small" /
    # "medium" (the original 1286x369 size, default) / "full" (fills the
    # primary display's usable bounds edge-to-edge - good for touchscreens
    # like the Steam Deck). Applied on the next OSK open after the setting
    # changes (see App._rebuild_cached_screen).
    "osk_size": "medium",
    # Steam Controller-only OSK settings (tray "Steam Controller" submenu, shown
    # only while an SC is connected). "Sticks Control Keyboard" on/off (key kept
    # as sc_left_stick_nav; OFF = OSK goes click-through, sticks/mouse drive the
    # desktop, L2/R2 = mouse buttons); and the L2/R2 OSK actuation point:
    # "default" (firmware full pull) / "low".
    "sc_left_stick_nav": True,
    "sc_osk_trigger_actuation": "default",
    # Steam Controller trackpad press-force calibration (raw OpenPuck force,
    # hand-edit these in settings.json to retune — no tray UI). Measured on
    # hardware 2026-08-09: the pad's physical click/vibration engages at
    # ~2500 (an earlier 3000 made the key insert a hair late, after the
    # vibration was already felt).
    #   engage: force that fires the pad-click key insert.
    #   release: press must fall below this to unlatch / unlock the cursor.
    #   hold: force that freezes the cursor on the selected key's center.
    #   lock_glide_alpha: lowpass for the freeze glide (normal smoothing 0.15).
    "sc_pad_click_engage": 2500,
    "sc_pad_click_release": 1000,
    "sc_pad_press_hold": 2000,
    "sc_pad_lock_glide_alpha": 0.35,
    # Steam Controller key-insert paths (settings.json, hand-edited — no tray
    # UI): the physical trackpad press ("touchpad click") inserts the key under
    # the pointer only while sc_pad_click_enter is ON (default OFF — the click
    # button below is the primary insert), and the click button that inserts
    # the key like a same-side pad click is selectable per side:
    #   "L1/R1" = bumpers (the original behavior, default) / "L2/R2" = triggers.
    "sc_pad_click_enter": False,
    "sc_click_button": "L1/R1",
    # Click-button focus (the lock-on-key that follows the click button):
    # when the click button is L2/R2, the trigger's analog pull (0..32767) at
    # which the pointer freezes on the key center (default = half pull); the
    # click itself still fires on the full-pull digital bit. L1/R1 (digital)
    # always focus on the press itself — this key only tunes the triggers.
    "sc_trigger_focus_pull": 16384,
    # Controller chord that opens the OSK while the keyboard is closed:
    # Steam/Guide held + this button opens it. Read from the raw HID by the
    # watcher (works elevated, unlike Steam Input's injected key chords).
    # Tray "Startup → Open Keyboard Chord". Default "X" (Steam+X).
    "sc_osk_open_chord": "X",
    # Split keyboard layout (tray "Steam Controller -> Split Keyboard"):
    # split the keyboard into left/right halves with a middle gap, each
    # touchpad covering its own half (better ergonomics — no cross-body
    # reach). Applied on the next OSK open (or live while open, like the
    # other SC toggles).
    "osk_split_layout": False,
    # Per-foreground-app remembered OSK "Move" position: {exe name (lowercase):
    # position index 0-5}. Remembered per app so the keyboard reopens where the
    # user left it in each app; apps without an entry keep the current spot
    # (down-mid on a fresh program start) — the fallback rule. Written by
    # triton when the user moves the keyboard while it is open.
    "window_position_per_app": {},
    # Per-foreground-app remembered OSK size and skin: {exe name (lowercase):
    # value}. The per-app look, mirroring window_position_per_app — each app
    # reopens with the size/skin the user last picked while it was focused;
    # apps without an entry fall back to the global osk_size / skin. Written by
    # the tray when a size/skin is selected while an app is foreground.
    "osk_size_per_app": {},
    "skin_per_app": {},
    # Diacritic variants (Feature B): hold a letter key to pick its accented
    # variants (iOS hold-letter style). "diacritics_enabled" is the master
    # on/off; "diacritic_locale" is the active variant-map locale — "auto"
    # resolves from the Windows keyboard input layout at startup — and
    # "diacritic_variants" is the per-locale letter -> [variants] map. The
    # default is the built-in fallback map (see triton/diacritics.py); a user
    # map in settings.json overrides it per (locale, letter) via the tray's
    # merge at startup. All default characters are covered by the bundled
    # fonts (Selawik + DejaVu — verified), so none render as tofu.
    "diacritics_enabled": True,
    "diacritic_locale": "auto",
    # Fresh copy, not an alias of the module-level built-in map: the loaded
    # settings dict must never share structure with DIACRITIC_VARIANTS, or an
    # in-place edit of a loaded settings map would corrupt the shared default.
    "diacritic_variants": diacritics.merge_diacritic_maps(
        diacritics.DIACRITIC_VARIANTS
    ),
    # Focus-flash experiments (see windows/flashing_issue.md). A/B toggles so
    # each fix can be tested and reverted without rebuilding behavior.
    #   focus_fix_open: "always-visible" (default) keeps the OSK window always
    #     visible but parked off-screen, so opening never runs ShowWindow on a
    #     hidden window — that show transition is what makes the foreground
    #     transiently NULL and dims/brightens the focused app. "showwindow"
    #     restores the old create-hidden-then-show behavior.
    #   steam_input_nudge: how the close path restores the Steam Input
    #     config that was active before the OSK.
    #     The PREFERRED path is capture-and-force-back + /0:
    #       (1) at open, the appid Steam Input used before the layer is
    #           captured from controller_ui.txt;
    #       (2) on close, force that appid back — a specific-appid force
    #           is applied INSTANTLY (no activation change needed, no
    #           helper-hop, no flash);
    #       (3) then dispatch /0 to return Steam Input to AUTO-switching,
    #           so alt-tab to a game / Big Picture picks the right config.
    #     This setting only picks the FALLBACK (used when the capture
    #     failed): "hop" (helper-window foreground hop, one visible
    #     dim->brighten), "hop-fast" (~2ms hold, DWM likely never presents
    #     the inactive frame), "hop-noactivate" (helper is WS_EX_NOACTIVATE
    #     so the app never dims), or "event" (synthetic WinEvents — known
    #     NOT to make Steam re-evaluate; kept for A/B).
    #   steam_input_nudge_delay: seconds to wait after Steam confirms /0
    #     before the FIRST hop. The nudge now VERIFIES the restore against
    #     Steam's controller_ui.txt (OnFocusWindowChanged -> Desktop marker)
    #     and retries with backoff if Steam hasn't re-applied the auto
    #     config — so a too-short delay no longer leaves the appid forced;
    #     it just costs one extra hop. Tune down to speed up the common case
    #     (0.3 is usually enough; the retry covers the slow-force-removal
    #     case).
    "focus_fix_open": "always-visible",
    "steam_input_nudge": "hop",
    "steam_input_nudge_delay": 1.0,
}


# --- Settings persistence ---------------------------------------------------


def _settings_path():
    """settings.json lives in the per-user appdata dir (see applog.user_data_dir),
    so the app no longer needs a writable install folder. Kept separate so the
    old-exe-dir migration below can target the same path."""
    return os.path.join(user_data_dir(), SETTINGS_FILENAME)


def _migrate_old_settings():
    """One-time migration: if an old settings.json sits next to the exe/script
    (the pre-appdata layout) and the appdata file doesn't exist yet, copy the
    values across. Returns True if it migrated (or the appdata file already
    existed)."""
    new_path = _settings_path()
    if os.path.isfile(new_path):
        return True
    old_path = os.path.join(_exe_dir(), SETTINGS_FILENAME)
    if os.path.isfile(old_path):
        try:
            import shutil

            shutil.copy2(old_path, new_path)
        except OSError:
            return False
    return True


# Windows absolute-path prefix (drive letter), e.g. "C:" or "c:\...". A skin
# name is matched against Steam theme names — it must never carry a path.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _coerce_bool(val, default):
    """Strict bool coercion: real bools pass, legacy 0/1 ints fold (old
    settings.json files stored them that way), and EVERYTHING else — notably
    strings like "false"/"0", which bool() happily turns into True — falls
    back to the key's default so a malformed/hostile file can't flip
    behavior."""
    if isinstance(val, bool):
        return val
    if isinstance(val, int) and val in (0, 1):
        return bool(val)
    return default


def _coerce_number(val, default):
    """Strict numeric coercion for numeric keys (pad press-force calibration,
    trigger focus pull): the value must be a real number — a numeric string
    would propagate a malformed value into the input thread. bool is an int
    subclass and is rejected explicitly. Invalid values fall back to the
    default."""
    if isinstance(val, bool):
        return default
    if not isinstance(val, (int, float)):
        return default
    try:
        return type(default)(val)
    except (TypeError, ValueError, OverflowError):
        return default


def _valid_skin_name(name):
    """Return `name` unchanged if it is a safe skin name — a plain identifier
    (Steam theme class name) — else None. Rejects non-strings,
    blank/whitespace-padded names, path separators, ".." traversal,
    rooted/absolute paths (leading / or \\, or a drive letter), so a hostile
    settings.json can never smuggle a path into the Steam-theme lookup."""
    if not isinstance(name, str):
        return None
    if not name.strip() or name != name.strip():
        return None
    if ("/" in name) or ("\\" in name) or (".." in name):
        return None
    if _WINDOWS_DRIVE_RE.match(name):
        return None
    return name


def _valid_position_per_app(val):
    """Filter a malformed/hostile `window_position_per_app` map: keep only
    {exe name: int 0-5} entries. Values that aren't a valid 0..5 int and keys
    that are empty or non-string are dropped; a non-dict value collapses to
    the empty default. Never raises."""
    if not isinstance(val, dict):
        return {}
    out = {}
    for name, index in val.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        if 0 <= index <= 5:
            out[name] = index
    return out


def _valid_osk_size_per_app(val):
    """Filter a malformed/hostile `osk_size_per_app` map: keep only
    {exe name: "small"|"medium"|"full"} entries. Values that aren't a known
    size name and keys that are empty or non-string are dropped; a non-dict
    value collapses to the empty default. Never raises."""
    if not isinstance(val, dict):
        return {}
    out = {}
    for name, size in val.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if size in OSK_SIZES:
            out[name] = size
    return out


def _valid_skin_per_app(val):
    """Filter a malformed/hostile `skin_per_app` map: keep only {exe name:
    safe skin name} entries, each skin checked by _valid_skin_name (a plain
    identifier matching a Steam theme name). Keys that are empty or non-string
    are dropped; a non-dict value collapses to the empty default. Never
    raises."""
    if not isinstance(val, dict):
        return {}
    out = {}
    for name, skin in val.items():
        if not isinstance(name, str) or not name.strip():
            continue
        skin_name = _valid_skin_name(skin)
        if skin_name is not None:
            out[name] = skin_name
    return out


def _load_settings():
    _migrate_old_settings()
    path = _settings_path()
    try:
        # utf-8-sig strips a leading UTF-8 BOM: hand-edited settings.json
        # written by Notepad or PowerShell (Set-Content -Encoding UTF8) can
        # carry one, and json.load under plain "utf-8" raises on the BOM —
        # which _load_settings used to swallow and fall back to defaults.
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)
    merged = dict(DEFAULT_SETTINGS)
    # Coerce each known key strictly to the type of its default, so a
    # malformed/hostile settings.json can't flip behavior: bools must really
    # be bools (bool("false") is True), numeric keys must be real numbers
    # (not strings), and the skin name must never smuggle path separators
    # (it feeds the Steam-theme lookup). One bad value falls back to that
    # key's default — never crashes the load. Unknown keys pass through
    # unvalidated for forward compat.
    for k, val in data.items():
        if k not in DEFAULT_SETTINGS:
            continue
        default = DEFAULT_SETTINGS[k]
        if isinstance(default, bool):
            merged[k] = _coerce_bool(val, default)
        elif isinstance(default, (int, float)):
            merged[k] = _coerce_number(val, default)
        elif k == "skin":
            merged[k] = _valid_skin_name(val) or default
        elif k == "window_position_per_app":
            merged[k] = _valid_position_per_app(val)
        elif k == "osk_size_per_app":
            merged[k] = _valid_osk_size_per_app(val)
        elif k == "skin_per_app":
            merged[k] = _valid_skin_per_app(val)
        else:
            merged[k] = val
    # Migrate old exclusive_access key to block_sc_hid.
    if "exclusive_access" in data:
        merged["block_sc_hid"] = _coerce_bool(
            data["exclusive_access"], merged["block_sc_hid"]
        )
    # The single global "rumble_enabled" predated the per-controller toggles —
    # seed the SC switch from the old value so a saved preference carries over.
    if "rumble_enabled" in data:
        merged["rumble_enabled_sc"] = _coerce_bool(
            data["rumble_enabled"], merged["rumble_enabled_sc"]
        )
    # The two-level "low"(6000)/"lower"(3000) actuation collapsed to a single
    # "low" using the lighter 3000 pull — fold a saved "lower" into "low".
    if merged.get("sc_osk_trigger_actuation") == "lower":
        merged["sc_osk_trigger_actuation"] = "low"
    return merged


def _save_settings(settings):
    path = _settings_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except OSError as e:
        print(f"settings save failed: {e}")


_SC_ACTUATION_THRESHOLDS = {"default": None, "low": 3000}


# Steam Controller "click button" (sc_click_button, settings.json) → the
# (left, right) button bits that insert the key like a same-side pad click.
_SC_CLICK_BUTTONS = {
    "L1/R1": (SCButtons.LB, SCButtons.RB),  # bumpers — original behavior
    "L2/R2": (SCButtons.LT, SCButtons.RT),  # triggers
}

# OSK-open chord (sc_osk_open_chord, settings.json) → the button bit that
# opens the OSK when held with Steam (Guide). The watcher reads the raw HID,
# so the chord works even though the app runs elevated (Steam Input's injected
# key chords can't reach an elevated process — UIPI).
# Face buttons only: the guide-chord config's four_buttons group maps them
# directly to button_x/y/a/b slots (reliable to patch). Bumpers/triggers live
# in differently-structured groups (switches/trigger), so they are not offered.
_SC_OSK_OPEN_CHORDS = {
    "X": SCButtons.X,
    "Y": SCButtons.Y,
    "A": SCButtons.A,
    "B": SCButtons.B,
}
