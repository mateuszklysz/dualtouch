import ctypes

import sdl3w as S

from triton import screen, state

# Index into the 6-position window rotation, advanced by Shift+Move.
# 0 starts at down-mid (the default open location).
_position_index = [0]


# Index of the up-right spot in _apply_window_position's seq, used to force
# the OSK there on open (without disturbing _position_index) when the Windows
# Start menu is covering its usual spot.
_POS_UP_RIGHT = 4


def _apply_window_position(sdl_window, index=None, move=True):
    """Compute (and by default apply) the OSK spot for `index` (default: the
    CURRENT _position_index, 0 = down-mid). Used to restore the remembered
    position when the OSK (re)opens, after advancing the index by
    _cycle_window_position, or to force _POS_UP_RIGHT on open when the Start
    menu is covering the usual spot (see _is_start_menu_open).

    `move=False` only computes the resting (x, y) without touching the window.
    The always-visible open path uses it: the window is parked off-screen when
    closed, and _begin_open_anim renders the transparent first frame while it
    is STILL off-screen, then moves it to the raised spot — moving it on-screen
    first (with the stale keyboard frame still displayed) would flash."""
    bounds = S.SDL_Rect()
    disp = S.SDL_GetPrimaryDisplay()
    if not (disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds))):
        return None
    w = screen.width
    h = screen.height
    x_left = bounds.x
    x_mid = bounds.x + max(0, (bounds.w - w) // 2)
    x_right = bounds.x + max(0, bounds.w - w)
    y_top = bounds.y
    y_bot = bounds.y + max(0, bounds.h - h)
    # 0 down-mid (start) → 1 down-left → 2 up-left → 3 up-mid → 4 up-right → 5 down-right → 0.
    seq = [
        (x_mid, y_bot),
        (x_left, y_bot),
        (x_left, y_top),
        (x_mid, y_top),
        (x_right, y_top),
        (x_right, y_bot),
    ]
    x, y = seq[_position_index[0] if index is None else index]
    if move:
        S.SDL_SetWindowPosition(sdl_window, x, y)
    # The resting (x, y) — the open animation settles the window down INTO this.
    return (x, y)


def _cycle_window_position(sdl_window):
    if state.is_split_layout_enabled():
        # The split window spans the full display width, so the left/right/mid
        # spots collapse to the same x — the Move key alternates between DOWN
        # (indices 0/1/5) and UP (2/3/4) only.
        _position_index[0] = 0 if _position_index[0] not in (0, 1, 5) else 3
    else:
        _position_index[0] = (_position_index[0] + 1) % 6
    _apply_window_position(sdl_window)


def _position_for_app(exe_name, per_app_map):
    """The 6-position index to use while `exe_name` is foreground: that app's
    stored index from `per_app_map` ({exe name: index 0-5}), else the global
    default _position_index (0 = down-mid on a fresh program start). Pure --
    never mutates `per_app_map`. Stored values are coerced safely: an
    int()-coercible value is wrapped into the 0-5 cycle; a non-numeric value
    (which used to raise and stop the OSK from opening) falls back to the
    global default."""
    if exe_name is None:
        return _position_index[0]
    stored = per_app_map.get(exe_name)
    if stored is None:
        return _position_index[0]
    try:
        return int(stored) % 6
    except (TypeError, ValueError, OverflowError):
        return _position_index[0]


def _save_position_for_app(exe_name, index, per_app_map):
    """Return a NEW {exe name: index} map recording `index` (wrapped into
    0-5). Returns `per_app_map` unchanged when exe_name is None --
    non-positionable foregrounds (no app, our own window, shell/Steam) never
    get a remembered position -- or when `index` isn't int()-coercible (a
    hostile/malformed index is ignored rather than crashing the caller).
    Pure: the caller owns persisting the result."""
    if exe_name is None:
        return per_app_map
    try:
        idx = int(index) % 6
    except (TypeError, ValueError, OverflowError):
        return per_app_map
    out = dict(per_app_map)
    out[exe_name] = idx
    return out
