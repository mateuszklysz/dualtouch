import ctypes
from collections import deque
from contextlib import suppress
from enum import IntEnum
from threading import Lock

from triton import diacritics

should_exit = False
should_exit_lock = Lock()

_visible = True
_visible_lock = Lock()

_shift_held = False
_shift_lock = Lock()

# Latched Shift from the mouse/click path (clicking the on-screen Shift key or
# right-click). Kept separate from _shift_held because a connected controller
# rewrites _shift_held every input frame — the toggle must read its own latch
# to decide on/off, or it would never turn back off.
_shift_latched = False
_shift_latch_lock = Lock()

# Latched Ctrl from clicking the on-screen Ctrl key (mirror of the Shift
# latch: the controller has no Ctrl hold, so this flag is the single source
# of truth for whether real KEY_LEFTCTRL is held on the OS).
_ctrl_latched = False
_ctrl_latch_lock = Lock()
_alt_latched = False
_alt_latch_lock = Lock()

# Set of keycode strings (e.g. "KEY_BACKSPACE") that should render in the
# CLICK (blue) state, e.g. while their corresponding controller button is held.
_highlighted = set()
# Cached sorted tuple mirror of _highlighted (kept in sync by set_highlighted).
# The renderer's dirty-gate signature needs a sorted snapshot every frame; the
# sort is paid once per set instead of per content_changed call.
_highlighted_sorted = ()

# Select mode: the on-screen "Select" key (behavior: select) is being held,
# so horizontal pad/mouse drag sends Shift+Left/Right to select text (like
# iOS hold-space). Read by the pad/mouse input threads and the renderer (to
# highlight the Select key while it is held).
_select_active = False
_select_lock = Lock()
_highlight_lock = Lock()

# Touchpad capacitive-touch state. Renderer uses it to hide the L2/R2 hint
# glyphs on Shift/Enter while LT/RT's alternate "click under pad" role is
# active.
_lpad_touched = False
_rpad_touched = False
_touch_lock = Lock()

# DPAD cursor over the virtual keyboard's grid. The cursor key is painted
# as HOVER; the A button presses it.
_cursor = (2, 5)
_cursor_lock = Lock()
# True once the user has actually moved the cursor with the stick/DPAD. The
# cursor highlight must NOT appear from its default (2, 5) — a touchpad-only
# user lifting both fingers shouldn't see an arbitrary key ("G") lit up.
_cursor_used = False

# (row, col) of the key currently held down by the left mouse button, or None.
# Painted in the CLICK (blue) state so a mouse press flashes like a real key
# press. Kept separate from _highlighted (which a controller frame overwrites).
_mouse_press_cell = None
_mouse_press_lock = Lock()

# Queue of (row, col) cells whose KeyButton callback should fire on the
# main thread (the A button enqueues here on each rising edge).
_key_press_queue = deque()
_key_press_lock = Lock()

# DPAD direction events posted by the controller thread; consumed by the
# main loop, which has access to the keyboard layout for pixel-aware nav.
_dpad_queue = deque()
_dpad_lock = Lock()

# Shared empty drain result (see drain_*_queue): callers only iterate the
# drained items, never mutate them, so one canonical empty list is safe and
# skips the per-call list() allocation + clear() on an idle queue (~500 Hz).
_EMPTY = []

# Set by the Move-key (shift held) callback to ask the main thread to
# advance the keyboard window through its 6-position rotation.
_position_cycle_requested = False
_position_cycle_lock = Lock()

# Lock-screen launcher: ask the main thread to JUMP to a SPECIFIC position
# index (0-5, see triton._apply_window_position) instead of cycling — used to
# move the OSK out of the way of the LogonUI password box once its on-screen
# location is known. None = no pending request.
_window_position_request = None
_window_position_lock = Lock()

# Per-app remembered OSK positions (settings.json "window_position_per_app"):
# {foreground process exe name (lowercased): position index 0-5}. Remembered
# per foreground app so the OSK reopens where the user left it in each app;
# apps without an entry fall back to the global default (down-mid). Published
# by the tray at startup and updated by triton when the user moves the
# keyboard. Session-independent config - deliberately NOT reset by
# reset_session().
_position_per_app = {}
_position_per_app_lock = Lock()

# Per-app remembered OSK look (settings.json "osk_size_per_app" /
# "skin_per_app"): {foreground process exe name (lowercased): size name /
# skin name}. Same per-app mechanism as _position_per_app — each app reopens
# with the size/skin the user last picked while it was focused; apps without
# an entry fall back to the global osk_size / skin. Published by the tray at
# startup and updated when a size/skin is selected while an app is foreground.
# Session-independent config - deliberately NOT reset by reset_session().
_osk_size_per_app = {}
_osk_size_per_app_lock = Lock()
_skin_per_app = {}
_skin_per_app_lock = Lock()

# Tracks whether the OS emoji picker (opened by the on-screen emoji key) is
# currently open, so pressing the emoji key again closes it (sends Escape)
# instead of re-opening. Reset per OSK open so a fresh session starts closed.
_emoji_open = False
_emoji_lock = Lock()

# Monotonic time of the last frame where the controller was ACTIVELY used —
# any button other than a resting trackpad touch, or a stick pushed past the
# OSK's mouse deadzone (see controller.py). Published by the input thread,
# read by triton's mouse handling: while the user is driving the controller,
# mouse events landing on the OSK window are Steam Input's injected duplicates
# of the controller action (its desktop config emulates a mouse), and must be
# ignored so they can't chase the highlight around or press a random key.
# 0.0 = no activity recorded this session (gate never fires).
_last_controller_activity = 0.0
_controller_activity_lock = Lock()

# Monotonic time of the last mouse move the OSK INJECTED itself (the
# right-stick → system-cursor path in controller.py). triton's mouse-handling
# gate exempts these from the controller-activity suppression above — a
# movement we made deliberately must still highlight keys, or the right-stick
# pointer would be dead while typing. 0.0 = no injection this session.
_osk_mouse_inject_t = 0.0
_osk_mouse_inject_lock = Lock()

# HWND (int) of the window the user was typing in just before the OSK opened.
# triton.main restores focus to it after showing the OSK: a controller-open
# fires the firmware lizard's mouse-click, which can land off the target field
# and steal focus. The OSK window is NOACTIVATE so it never takes focus, so
# re-activating this window puts the caret back. None = nothing to restore.
_focus_restore_target = None
_focus_restore_lock = Lock()

# Whether Steam is currently running (published by the tray). The OSK's
# SteamHidSource skips its firmware lizard-restore on close while Steam is
# active: Steam Input owns the device's lizard state, and re-enabling firmware
# lizard on close makes Steam re-assert it — the ~1s "lizard mode" blip right
# after closing the keyboard. False = standalone use, which WANTS the restore.
_steam_running = False
_steam_running_lock = Lock()

# Windows focus-flash fix (settings.json "focus_fix_open" == "always-visible",
# published by the tray BEFORE the cached Screen is built). When True the OSK
# window is created VISIBLE but parked far off-screen, and "opening"/"closing"
# only move it between off-screen and its resting spot (SetWindowPos with
# SWP_NOACTIVATE) — the window is never shown via ShowWindow once it exists, so
# the transiently-NULL foreground that dims/brightens the focused app at open
# never happens. False = the old create-hidden-then-show path. Read by both
# screen.Screen (window creation flags) and triton (show/close/teardown).
_osk_always_visible = False
_osk_always_visible_lock = Lock()


def set_steam_running(running):
    global _steam_running
    with _steam_running_lock:
        _steam_running = bool(running)


def is_steam_running():
    with _steam_running_lock:
        return _steam_running


def set_osk_always_visible(enabled):
    """Publish whether the OSK window should be created visible+off-screen
    (the focus-flash fix) instead of hidden-then-shown. Read at Screen build
    time, so it must be set before the cached Screen is constructed."""
    global _osk_always_visible
    with _osk_always_visible_lock:
        _osk_always_visible = bool(enabled)


def is_osk_always_visible():
    with _osk_always_visible_lock:
        return _osk_always_visible


# Diacritic-variant hold-to-extend session (Feature B, see
# vkb.diacritic_variants_for_key): while a letter key is held past the hold
# delay its variant row is open. The session tuple is
# (base_char, tuple(variants), index, rect, source) where index is -1 =
# "base" (no variant chosen, release keeps the already-typed base letter) and
# >= 0 is the index into `variants`; rect = (x, y, w, h) px of the strip in
# window coords; source = "pad" / "mouse" / "a" (which input opened it, so
# the right input drives the highlight + commit). None = no row open.
_diacritic = None
_diacritic_lock = Lock()

# Diacritic config: the merged per-locale variant map (built-in fallback
# merged with the user's settings.json map by the tray; user wins per
# letter), the active locale (Windows keyboard layout resolved by the tray,
# or a user-picked one), and the master on/off switch. Session-independent:
# published by the tray and deliberately NOT reset by reset_session().
_diacritic_variants = dict(diacritics.DIACRITIC_VARIANTS)
_diacritic_variants_lock = Lock()
_active_locale = "en"
_active_locale_lock = Lock()
_diacritics_enabled = True
_diacritics_enabled_lock = Lock()


def open_diacritic(char, variants, rect, source):
    """Open the variant row for `char` (the base letter), with the candidate
    list `variants`, the strip rect (x, y, w, h) in window px, and the input
    `source` that opened it. Selection starts at -1 (base). Returns False
    (and leaves no row open) when `rect` is None — a candidate strip that
    can't be clamped into the window (see diacritics.variant_row_rect)."""
    if rect is None:
        return False
    global _diacritic
    with _diacritic_lock:
        _diacritic = (
            str(char),
            tuple(str(v) for v in variants),
            -1,
            tuple(int(v) for v in rect),
            str(source),
        )
    return True


def close_diacritic():
    global _diacritic
    with _diacritic_lock:
        _diacritic = None


def is_diacritic_open():
    with _diacritic_lock:
        return _diacritic is not None


def get_diacritic():
    with _diacritic_lock:
        return _diacritic


def set_diacritic_index(index):
    global _diacritic
    with _diacritic_lock:
        if _diacritic is None:
            return
        _diacritic = (
            _diacritic[0],
            _diacritic[1],
            int(index),
            _diacritic[3],
            _diacritic[4],
        )


def get_diacritic_index():
    with _diacritic_lock:
        return _diacritic[2] if _diacritic is not None else -1


def get_diacritic_rect():
    with _diacritic_lock:
        return _diacritic[3] if _diacritic is not None else None


def get_diacritic_source():
    with _diacritic_lock:
        return _diacritic[4] if _diacritic is not None else None


def get_diacritic_variants_list():
    with _diacritic_lock:
        return _diacritic[1] if _diacritic is not None else ()


def get_diacritic_variant_count():
    with _diacritic_lock:
        return len(_diacritic[1]) if _diacritic is not None else 0


def get_diacritic_selected_char():
    """The currently highlighted variant char, or None when the selection is
    the base (index -1) or no row is open."""
    with _diacritic_lock:
        if _diacritic is None or _diacritic[2] < 0:
            return None
        return _diacritic[1][_diacritic[2]]


def set_diacritic_variants(mapping):
    """Publish the merged per-locale letter->variants map (the tray merges
    the built-in fallback with the user's settings; user wins per letter)."""
    global _diacritic_variants
    with _diacritic_variants_lock:
        _diacritic_variants = {
            str(k).lower(): {
                str(a).lower(): list(b) for a, b in (v or {}).items()
            }
            for k, v in (mapping or {}).items()
        }


def get_diacritic_variants():
    with _diacritic_variants_lock:
        return {k: dict(v) for k, v in _diacritic_variants.items()}


def set_active_locale(locale):
    """Publish the active variant-map locale (the Windows keyboard layout
    resolved by the tray, or a user-picked one). Lookups fall back to "en"
    when the locale has no entry in the map."""
    global _active_locale
    with _active_locale_lock:
        _active_locale = str(locale or "en").lower()


def get_active_locale():
    with _active_locale_lock:
        return _active_locale


def set_diacritics_enabled(enabled):
    global _diacritics_enabled
    with _diacritics_enabled_lock:
        _diacritics_enabled = bool(enabled)


def is_diacritics_enabled():
    with _diacritics_enabled_lock:
        return _diacritics_enabled


# Optional haptic-feedback hook. The controller thread registers a callable
# (bound to the live SteamController) here; the main thread calls haptic_tick()
# on each key press for a trackpad "tick". None when no controller is open.
_haptic_tick = None
# Separate, stronger hook for the simulated physical pad-click (press/release)
# so only that feedback is deeper/more intense than the light UI tick.
_pad_click_haptic = None
# On/off for Steam Controller haptics (OSK UI ticks AND rumble). Driven by the
# rumble_enabled_sc settings.json key (no tray submenu anymore).
_rumble_enabled = True
_key_sound = None
_key_sound_open = None
_key_sound_close = None
_key_sound_enabled = True
_haptic_lock = Lock()

# Win32: ask the OS whether Caps Lock is currently toggled on. Lets the
# on-screen keyboard mirror the system caps state automatically — we don't
# need to track L3 ourselves because L3 just sends KEY_CAPSLOCK to the OS.
_VK_CAPITAL = 0x14
try:
    _user32 = ctypes.windll.user32
    _user32.GetKeyState.restype = ctypes.c_short
except Exception:
    _user32 = None


def close():
    global should_exit
    with should_exit_lock:
        should_exit = True


def reset_session():
    """Wipe per-session state so triton.main() can be invoked again from a
    long-lived launcher process (no subprocess startup cost)."""
    global \
        should_exit, \
        _visible, \
        _shift_held, \
        _shift_latched, \
        _ctrl_latched, \
        _alt_latched, \
        _highlighted
    global _highlighted_sorted
    global _select_active
    global _lpad_touched, _rpad_touched, _cursor
    global _position_cycle_requested, _mouse_press_cell, _focus_restore_target
    global _emoji_open, _window_position_request
    global _last_controller_activity, _osk_mouse_inject_t
    global _diacritic
    with should_exit_lock:
        should_exit = False
    with _emoji_lock:
        _emoji_open = False
    with _focus_restore_lock:
        _focus_restore_target = None
    with _visible_lock:
        _visible = True
    with _shift_lock:
        _shift_held = False
    with _shift_latch_lock:
        _shift_latched = False
    with _ctrl_latch_lock:
        _ctrl_latched = False
    with _alt_latch_lock:
        _alt_latched = False
    with _highlight_lock:
        _highlighted = set()
        _highlighted_sorted = ()
    with _select_lock:
        _select_active = False
    with _touch_lock:
        _lpad_touched = False
        _rpad_touched = False
    with _cursor_lock:
        _cursor = (2, 5)
        global _cursor_used
        _cursor_used = False
    with _mouse_press_lock:
        _mouse_press_cell = None
    with _key_press_lock:
        _key_press_queue.clear()
    with _dpad_lock:
        _dpad_queue.clear()
    with _position_cycle_lock:
        _position_cycle_requested = False
    with _window_position_lock:
        _window_position_request = None
    with _controller_activity_lock:
        _last_controller_activity = 0.0
    with _osk_mouse_inject_lock:
        _osk_mouse_inject_t = 0.0
    with _diacritic_lock:
        _diacritic = None


def set_last_controller_activity(t):
    """Record that the controller was actively used at monotonic time `t`."""
    global _last_controller_activity
    with _controller_activity_lock:
        _last_controller_activity = t


def get_last_controller_activity():
    with _controller_activity_lock:
        return _last_controller_activity


def set_osk_mouse_inject(t):
    """Record that the OSK itself moved the system cursor at monotonic time
    `t` (right-stick mouse). Exempts that movement from the injected-input
    suppression in triton's mouse handling."""
    global _osk_mouse_inject_t
    with _osk_mouse_inject_lock:
        _osk_mouse_inject_t = t


def get_osk_mouse_inject_t():
    with _osk_mouse_inject_lock:
        return _osk_mouse_inject_t


def set_focus_restore_target(hwnd):
    """Record the window (HWND int) to re-focus after the OSK opens, or None."""
    global _focus_restore_target
    with _focus_restore_lock:
        _focus_restore_target = hwnd


def get_focus_restore_target():
    with _focus_restore_lock:
        return _focus_restore_target


def should_close():
    global should_exit
    with should_exit_lock:
        ret = should_exit
    return ret


def is_visible():
    with _visible_lock:
        return _visible


def show():
    global _visible
    with _visible_lock:
        _visible = True


def is_shift_held():
    with _shift_lock:
        return _shift_held


def set_shift_held(value):
    global _shift_held
    with _shift_lock:
        _shift_held = bool(value)


def is_shift_latched():
    with _shift_latch_lock:
        return _shift_latched


def set_shift_latched(value):
    global _shift_latched
    with _shift_latch_lock:
        _shift_latched = bool(value)


def is_ctrl_latched():
    with _ctrl_latch_lock:
        return _ctrl_latched


def set_ctrl_latched(value):
    global _ctrl_latched
    with _ctrl_latch_lock:
        _ctrl_latched = bool(value)


def is_alt_latched():
    with _alt_latch_lock:
        return _alt_latched


def set_alt_latched(value):
    global _alt_latched
    with _alt_latch_lock:
        _alt_latched = bool(value)


def get_latched_modifier_keys():
    """Keycode strings (e.g. "KEY_LEFTSHIFT") of the modifiers currently
    LATCHED via the on-screen toggle. Used by the renderer to show them as a
    stable "held" highlight instead of a click (no press animation)."""
    keys = set()
    if is_shift_latched():
        keys.update({"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"})
    if is_ctrl_latched():
        keys.update({"KEY_LEFTCTRL", "KEY_RIGHTCTRL"})
    if is_alt_latched():
        keys.update({"KEY_LEFTALT", "KEY_RIGHTALT"})
    return keys


def is_caps_on():
    """True if the OS has Caps Lock currently toggled on."""
    if _user32 is None:
        return False
    return bool(_user32.GetKeyState(_VK_CAPITAL) & 0x0001)


def is_select_active():
    """True while the on-screen Select key is held (iOS-style text selection)."""
    with _select_lock:
        return _select_active


def set_select_active(value):
    global _select_active
    with _select_lock:
        _select_active = bool(value)


def set_highlighted(items):
    global _highlighted, _highlighted_sorted
    with _highlight_lock:
        _highlighted = set(items)
        _highlighted_sorted = tuple(sorted(_highlighted))


def get_highlighted():
    # Returns the cached sorted tuple (a stable snapshot, never mutated by
    # callers) — the renderer uses it both as a membership set (tiny) and as
    # the dirty-gate signature element, so no per-call set()/sort() copy.
    with _highlight_lock:
        return _highlighted_sorted


def is_lpad_touched():
    with _touch_lock:
        return _lpad_touched


def is_rpad_touched():
    with _touch_lock:
        return _rpad_touched


def set_pad_touched(left, right):
    global _lpad_touched, _rpad_touched
    with _touch_lock:
        _lpad_touched = bool(left)
        _rpad_touched = bool(right)


def get_cursor():
    with _cursor_lock:
        return _cursor


def mark_cursor_used():
    """Called when the DPAD/stick navigation moves the cursor — the ONLY input
    that should arm the persistent cursor highlight (see is_cursor_used)."""
    global _cursor_used
    with _cursor_lock:
        _cursor_used = True


def set_cursor(row, col):
    global _cursor
    with _cursor_lock:
        _cursor = (int(row), int(col))


def is_cursor_used():
    """True once the DPAD/stick cursor has been user-navigated (so its
    highlight only appears for keyboard-nav users, never from the default)."""
    with _cursor_lock:
        return _cursor_used


def get_mouse_press_cell():
    with _mouse_press_lock:
        return _mouse_press_cell


def set_mouse_press_cell(cell):
    global _mouse_press_cell
    with _mouse_press_lock:
        _mouse_press_cell = tuple(cell) if cell is not None else None


# The active Keyboard layout (vkb.Keyboard), published by the main loop so
# controller.py can resolve a pixel position to a key cell (the press-lock
# feature glides the cursor to the selected key's center). Stale-safe: only
# read while the OSK is open, and None is handled by the caller.
_virtual_kb = None


def set_virtual_kb(kb):
    global _virtual_kb
    _virtual_kb = kb


def get_virtual_kb():
    return _virtual_kb


def queue_key_press(row, col, repeat=False, silent=False):
    # repeat=True marks an auto-repeat hit (A held); the main thread only acts
    # on it over Backspace, so holding rubs out text without machine-gunning
    # ordinary keys. silent=True marks a DEFERRED release (a variant-capable
    # key's base typed on release after the press edge already clicked) so the
    # dispatch suppresses the second click sound.
    with _key_press_lock:
        _key_press_queue.append(
            (int(row), int(col), bool(repeat), bool(silent))
        )


def drain_key_press_queue():
    with _key_press_lock:
        if not _key_press_queue:
            return _EMPTY
        out = list(_key_press_queue)
        _key_press_queue.clear()
    return out


def queue_dpad(direction, haptic=False):
    with _dpad_lock:
        _dpad_queue.append((direction, bool(haptic)))


def drain_dpad_queue():
    with _dpad_lock:
        if not _dpad_queue:
            return _EMPTY
        out = list(_dpad_queue)
        _dpad_queue.clear()
    return out


def request_position_cycle():
    global _position_cycle_requested
    with _position_cycle_lock:
        _position_cycle_requested = True


def take_position_cycle_request():
    global _position_cycle_requested
    with _position_cycle_lock:
        v = _position_cycle_requested
        _position_cycle_requested = False
    return v


def take_window_position_request():
    global _window_position_request
    with _window_position_lock:
        v = _window_position_request
        _window_position_request = None
    return v


def set_window_position_per_app(mapping):
    """Publish the {exe name: position index} map remembered per foreground
    app (settings.json "window_position_per_app", or updated by triton after
    a Move). Replaces the map wholesale; snapshots the caller's dict."""
    global _position_per_app
    with _position_per_app_lock:
        _position_per_app = dict(mapping or {})


def get_window_position_per_app():
    with _position_per_app_lock:
        return dict(_position_per_app)


def set_osk_size_per_app(mapping):
    """Publish the {exe name: size name} map remembered per foreground app
    (settings.json "osk_size_per_app", or updated by the tray after a size
    selection). Replaces the map wholesale; snapshots the caller's dict."""
    global _osk_size_per_app
    with _osk_size_per_app_lock:
        _osk_size_per_app = dict(mapping or {})


def get_osk_size_per_app():
    with _osk_size_per_app_lock:
        return dict(_osk_size_per_app)


def set_skin_per_app(mapping):
    """Publish the {exe name: skin name} map remembered per foreground app
    (settings.json "skin_per_app", or updated by the tray after a skin
    selection). Replaces the map wholesale; snapshots the caller's dict."""
    global _skin_per_app
    with _skin_per_app_lock:
        _skin_per_app = dict(mapping or {})


def get_skin_per_app():
    with _skin_per_app_lock:
        return dict(_skin_per_app)


def is_emoji_open():
    with _emoji_lock:
        return _emoji_open


def set_emoji_open(value):
    global _emoji_open
    with _emoji_lock:
        _emoji_open = bool(value)


def set_haptic_tick(fn):
    global _haptic_tick
    with _haptic_lock:
        _haptic_tick = fn


def set_pad_click_haptic(fn):
    global _pad_click_haptic
    with _haptic_lock:
        _pad_click_haptic = fn


def set_rumble_enabled(value):
    """Enable/disable Steam Controller haptics."""
    global _rumble_enabled
    with _haptic_lock:
        _rumble_enabled = bool(value)


def is_rumble_enabled():
    """Whether Steam Controller haptics are on."""
    with _haptic_lock:
        return _rumble_enabled


def set_key_sound(fn):
    """Register the key-press sound hook (plays Steam's keyboard click).
    Gated only by its own enabled flag — unlike the haptic ticks, it is
    NOT silenced while Steam runs (the OSK is used precisely while Steam
    Input holds the controller)."""
    global _key_sound
    with _haptic_lock:
        _key_sound = fn


def set_key_sound_open(fn):
    """Register the OSK-open sound hook (Steam modal-show chime)."""
    global _key_sound_open
    with _haptic_lock:
        _key_sound_open = fn


def set_key_sound_close(fn):
    """Register the OSK-close sound hook (Steam modal-hide chime)."""
    global _key_sound_close
    with _haptic_lock:
        _key_sound_close = fn


def set_key_sound_enabled(enabled):
    """Enable/disable the key-press sound."""
    global _key_sound_enabled
    with _haptic_lock:
        _key_sound_enabled = bool(enabled)


def is_key_sound_enabled():
    """Whether the key-press sound is on."""
    with _haptic_lock:
        return _key_sound_enabled


# Steam Controller-only OSK settings (settings.json sc_* keys). Set on the tray
# thread, read on the input thread.
_sc_lock = Lock()
# Left-stick OSK cursor navigation. Off = the SC's left stick doesn't move the
# OSK cursor, so its firmware-lizard behavior (e.g. scrolling a page) passes
# through while the OSK is open. Default on.
_sc_kbd_stick_nav = True
# OSK L2/R2 (Shift/Enter) actuation: None = firmware full-pull digital bit only
# (default); an int 0..32767 also engages Shift/Enter at that lighter analog pull.
_sc_osk_trigger_threshold = None

# Trackpad press-force calibration (settings.json sc_pad_* keys, hand-edited;
# published by the tray at OSK open, read on the input thread): (click engage,
# click release, press hold, lock-glide alpha). See tray.py DEFAULT_SETTINGS
# for the measured rationale.
_sc_pad_click_engage = 2500
_sc_pad_click_release = 1000
_sc_pad_press_hold = 2000
_sc_pad_lock_glide_alpha = 0.35
# Physical trackpad press inserts the key under the pointer (settings.json
# sc_pad_click_enter, default OFF — the click button is the primary insert).
_sc_pad_click_enter = False
# (left, right) SCButtons bits of the button that inserts the key like a
# same-side pad click (settings.json sc_click_button: "L1/R1" bumpers default,
# "L2/R2" triggers). Published by the tray at OSK open, read per frame on the
# input thread.
_sc_click_button = None
# Trigger analog pull (0..32767) at which the click-button focus engages when
# the click button is L2/R2 (settings.json sc_trigger_focus_pull; default =
# half pull). Pulling past this freezes the pointer on the key center; the
# click itself still fires on the full-pull digital bit.
_sc_trigger_focus_pull = 16384


def set_sc_pad_press(engage, release, hold, glide_alpha):
    global \
        _sc_pad_click_engage, \
        _sc_pad_click_release, \
        _sc_pad_press_hold, \
        _sc_pad_lock_glide_alpha
    with _sc_lock:
        _sc_pad_click_engage = engage
        _sc_pad_click_release = release
        _sc_pad_press_hold = hold
        _sc_pad_lock_glide_alpha = glide_alpha


def get_sc_pad_press():
    with _sc_lock:
        return (
            _sc_pad_click_engage,
            _sc_pad_click_release,
            _sc_pad_press_hold,
            _sc_pad_lock_glide_alpha,
        )


def set_sc_pad_click_enter(enabled):
    global _sc_pad_click_enter
    with _sc_lock:
        _sc_pad_click_enter = bool(enabled)


def get_sc_pad_click_enter():
    with _sc_lock:
        return _sc_pad_click_enter


def set_sc_click_button(bits):
    global _sc_click_button
    with _sc_lock:
        _sc_click_button = bits


def get_sc_click_button():
    with _sc_lock:
        return _sc_click_button


def set_sc_trigger_focus_pull(pull):
    global _sc_trigger_focus_pull
    with _sc_lock:
        _sc_trigger_focus_pull = pull


def get_sc_trigger_focus_pull():
    with _sc_lock:
        return _sc_trigger_focus_pull


def set_sc_kbd_stick_nav(enabled):
    global _sc_kbd_stick_nav
    with _sc_lock:
        _sc_kbd_stick_nav = bool(enabled)


def is_sc_kbd_stick_nav_enabled():
    with _sc_lock:
        return _sc_kbd_stick_nav


def set_sc_osk_trigger_threshold(threshold):
    global _sc_osk_trigger_threshold
    with _sc_lock:
        _sc_osk_trigger_threshold = threshold


def get_sc_osk_trigger_threshold():
    with _sc_lock:
        return _sc_osk_trigger_threshold


# Split layout (settings.json "osk_split_layout", tray "Steam Controller ->
# Split Keyboard"): split the keyboard into left/right halves with a middle
# gap, each touchpad covering its own half. Published by the tray; read by
# vkb (geometry) and controller.py (pad X mapping). Session-independent
# config - deliberately NOT reset by reset_session().
_split_layout = False
_split_layout_lock = Lock()


def set_split_layout(enabled):
    global _split_layout
    with _split_layout_lock:
        _split_layout = bool(enabled)


def is_split_layout_enabled():
    with _split_layout_lock:
        return _split_layout


def key_sound_tick():
    """Fire the registered key-press sound hook on every dispatched key.
    Gated by the key-sound enabled flag; NOT silenced while Steam runs (see
    set_key_sound). Fire-and-forget so a playback error never breaks input."""
    with _haptic_lock:
        if not _key_sound_enabled:
            return
        fn = _key_sound
    if fn is not None:
        with suppress(Exception):
            fn()


def key_sound_open():
    """Fire the registered OSK-open sound hook (once per OSK open)."""
    with _haptic_lock:
        if not _key_sound_enabled:
            return
        fn = _key_sound_open
    if fn is not None:
        with suppress(Exception):
            fn()


def key_sound_close():
    """Fire the registered OSK-close sound hook (once per OSK close)."""
    with _haptic_lock:
        if not _key_sound_enabled:
            return
        fn = _key_sound_close
    if fn is not None:
        with suppress(Exception):
            fn()


def haptic_tick():
    """Fire the registered haptic-feedback hook, if any. Safe to call from the
    main thread; swallows errors so feedback never breaks key dispatch. Gated
    by the Steam Controller's Vibration setting. Silenced while Steam runs:
    Steam Input plays its own haptics for the same actions, and stacked ticks
    feel muddy."""
    if is_steam_running():
        return
    with _haptic_lock:
        if not _rumble_enabled:
            return
        fn = _haptic_tick
    if fn is not None:
        with suppress(Exception):
            fn()


def pad_click_haptic():
    """Fire the stronger physical-pad-click hook (press/release of the
    simulated trackpad click). Falls back to the normal tick if no dedicated
    hook is registered. Gated by the Steam Controller's Vibration setting like
    haptic_tick(), and silenced while Steam runs for the same reason."""
    if is_steam_running():
        return
    with _haptic_lock:
        if not _rumble_enabled:
            return
        fn = _pad_click_haptic or _haptic_tick
    if fn is not None:
        with suppress(Exception):
            fn()


class InputState(IntEnum):
    INACTIVE = 0
    HOVER = 1
    CLICK = 2
