"""Per-foreground-app remembered OSK "look" — size and skin.

Parallel to windowpos.py's per-app position: each foreground app remembers the
OSK size and skin the user last picked while it was focused, so the keyboard
reopens with that app's own look. Apps without an entry (and non-positionable
foregrounds, exe_name None) fall back to the global osk_size / skin settings —
the caller applies that fallback, never these helpers.

All functions are pure: they never touch the maps in place, never touch
settings.json or the SDL globals, and never raise on malformed input.
"""

# The OSK size names the tray "Keyboard Skin -> Size" submenu offers and the
# renderer accepts (mirrors screen.set_osk_size). Single source of truth so
# appsettings' settings.json validation and the runtime lookup agree.
OSK_SIZES = ("small", "medium", "full")


def _size_for_app(exe_name, size_per_app_map):
    """The OSK size name to use while `exe_name` is foreground: that app's
    stored size from `size_per_app_map` ({exe name: size name}), else None —
    None means "no per-app entry, fall back to the global osk_size". A stored
    value that isn't a known size is treated as absent (the global wins); None
    exe (non-positionable foreground) always returns None."""
    if exe_name is None:
        return None
    stored = size_per_app_map.get(exe_name)
    if stored in OSK_SIZES:
        return stored
    return None


def _save_size_for_app(exe_name, size, size_per_app_map):
    """Return a NEW {exe name: size name} map recording `size` for `exe_name`.
    Returns `size_per_app_map` unchanged when exe_name is None or `size` isn't
    a known size name (a hostile/malformed value is ignored rather than
    crashing the caller). Pure: the caller owns persisting the result."""
    if exe_name is None or size not in OSK_SIZES:
        return size_per_app_map
    out = dict(size_per_app_map)
    out[exe_name] = size
    return out


def _skin_for_app(exe_name, skin_per_app_map):
    """The OSK skin name to use while `exe_name` is foreground: that app's
    stored skin from `skin_per_app_map` ({exe name: skin name}), else None —
    None means "no per-app entry, fall back to the global skin". A stored
    value that isn't a non-blank string is treated as absent; None exe
    (non-positionable foreground) always returns None."""
    if exe_name is None:
        return None
    stored = skin_per_app_map.get(exe_name)
    if isinstance(stored, str) and stored.strip():
        return stored
    return None


def _save_skin_for_app(exe_name, skin, skin_per_app_map):
    """Return a NEW {exe name: skin name} map recording `skin` for `exe_name`.
    Returns `skin_per_app_map` unchanged when exe_name is None or `skin` isn't
    a non-blank string (a hostile/malformed value is ignored). Pure: the
    caller owns persisting the result."""
    if exe_name is None or not isinstance(skin, str) or not skin.strip():
        return skin_per_app_map
    out = dict(skin_per_app_map)
    out[exe_name] = skin
    return out
