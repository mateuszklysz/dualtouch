import ctypes

import sdl3w as S

# Base ("Default") OSK dimensions for the tray "Keyboard Skin -> Size" submenu.
# `width`/`height` above hold the ACTIVE size — Screen.__init__ recomputes them
# from `_active_osk_size` on every construction, so "small"/"full" are derived
# from these base values (and, for "full", the current display).
_BASE_WIDTH = 1554
_BASE_HEIGHT = 369
# "Small" scales both dimensions down uniformly, for users who don't want the
# OSK to cover much of the screen.
_SMALL_SCALE = 0.7
# The base size is resolution-independent at 1286x369, which on a 4K display
# shrinks to a sliver of the screen. The default is scaled against the 1080p
# reference it was designed on, so the keyboard keeps the same RELATIVE size
# (and font proportions — _font_scale derives from the window height) on any
# display: ~1.5x on 4K (capped below the full proportional 2x — 1.5x reads
# better on a big TV), unchanged on 1080p.
_REFERENCE_HEIGHT = 1080
_OSK_SCALE_MIN = 0.75
_OSK_SCALE_MAX = 1.5


def _display_scale():
    """Default-OSK scale factor vs the 1080p reference the base size was
    designed on (raw display height, not the usable bounds, so a taskbar
    doesn't shrink the default). Falls back to 1.0 when the primary display
    can't be queried."""
    bounds = S.SDL_Rect()
    disp = S.SDL_GetPrimaryDisplay()
    if (
        disp
        and S.SDL_GetDisplayBounds(disp, ctypes.byref(bounds))
        and bounds.h > 0
    ):
        return max(
            _OSK_SCALE_MIN, min(_OSK_SCALE_MAX, bounds.h / _REFERENCE_HEIGHT)
        )
    return 1.0


def _compute_size(name):
    scale = _display_scale()
    if name == "small":
        return (
            int(round(_BASE_WIDTH * _SMALL_SCALE * scale)),
            int(round(_BASE_HEIGHT * _SMALL_SCALE * scale)),
        )
    if name == "full":
        bounds = S.SDL_Rect()
        disp = S.SDL_GetPrimaryDisplay()
        if disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds)):
            return bounds.w, int(round(_BASE_HEIGHT * scale))
        return _BASE_WIDTH, _BASE_HEIGHT
    return (int(round(_BASE_WIDTH * scale)), int(round(_BASE_HEIGHT * scale)))


def _compute_split_size(name):
    """Split-layout OSK size: the window spans the primary display's usable
    width edge-to-edge so both halves sit at the screen corners (the middle
    gap is rendered transparent, showing the desktop). The height follows the
    size submenu ("small" scales it down; "full"/"medium" share the scaled
    default height) — only the width changes in split mode."""
    scale = _display_scale()
    if name == "small":
        height = int(round(_BASE_HEIGHT * _SMALL_SCALE * scale))
    else:
        height = int(round(_BASE_HEIGHT * scale))
    bounds = S.SDL_Rect()
    disp = S.SDL_GetPrimaryDisplay()
    if disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds)):
        return bounds.w, height
    return int(round(_BASE_WIDTH * scale)), height
