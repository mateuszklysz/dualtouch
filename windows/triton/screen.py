import ctypes
import sys
import time

import sdl3w as S
from PIL import Image as PILImage
from PIL import ImageChops as PILImageChops

from triton import diacritics, resources, skins, state, utils
from triton.color import Color
from triton.fonts import (
    _FONT_CANDIDATES_LINUX,
    _FONT_CANDIDATES_WIN,
    _SYM_CANDIDATES_LINUX,
    _SYM_CANDIDATES_WIN,
    _first_existing,
    _Font,
)

# CoordFraction is re-exported here: triton.pad/controller/triton import it
# from triton.screen (the class lives in triton.geometry).
from triton.geometry import CoordFraction as CoordFraction
from triton.geometry import set_dims
from triton.size import _BASE_HEIGHT, _compute_size, _compute_split_size

width = 1286
height = 369

_active_osk_size = "medium"


# Parking spot for the always-visible OSK window while it is "closed". Far off
# the virtual screen so it's invisible and can't capture mouse input, but the
# window itself stays WS_VISIBLE — "opening" only moves it onto the display.
# This is the focus-flash fix (settings "focus_fix_open": "always-visible"): a
# window created VISIBLE never needs ShowWindow, so the transiently-NULL
# foreground that dims/brightens the focused app never happens.
_OFFSCREEN_X = -32000
_OFFSCREEN_Y = -32000


def set_osk_size(name):
    """Select the OSK window size ("small"/"medium"/"full") used by the NEXT
    Screen() construction. "medium" is the base 1286x369 size scaled to the
    display resolution (1080p reference); "small" scales it down; "full"
    stretches the width to span the primary display's usable bounds
    edge-to-edge (height stays the scaled default, recomputed at construction
    time, so it tracks the current display)."""
    global _active_osk_size
    _active_osk_size = (
        name if name in ("small", "medium", "full") else "medium"
    )


def get_osk_size():
    return _active_osk_size


def resize_for_layout():
    """Recompute width/height for the CURRENT split-layout setting + size
    submenu, updating the module dims (read by CoordFraction and vkb's key
    layout). Called at Screen construction and when the tray toggles split
    layout live (the main loop then resizes the SDL window). Returns (w, h)."""
    global width, height
    if state.is_split_layout_enabled():
        width, height = _compute_split_size(_active_osk_size)
    else:
        width, height = _compute_size(_active_osk_size)
    set_dims(width, height)  # keep geometry.py in sync (CoordFraction reads it)
    return width, height


class Screen:
    # Built-in default palette (kept when Steam's theme can't be resolved):
    # pure-black OLED backgrounds, cream labels, dark-grey cursor.
    bg_color = Color(0x00, 0x00, 0x00)

    key_color = {
        state.InputState.INACTIVE: Color(0x14, 0x14, 0x14),
        state.InputState.HOVER: Color(0x2A, 0x2A, 0x2A),  # cursor dark grey
        state.InputState.CLICK: Color(0x3F, 0x3F, 0x3F),  # press flash grey
    }
    # Modifier keys (Tab, Caps, Shift, Enter, Backspace) use a slightly
    # lighter dark grey when idle.
    modifier_idle_color = Color(0x1A, 0x1A, 0x1A)
    # Text stays cream on every state — the cursor/press fills are dark greys,
    # so the label never vanishes against them. The CLICK entry is the
    # held/pressed (toggle-on) highlight; it must contrast key_color[CLICK].
    text_color = {
        state.InputState.INACTIVE: Color(0xEB, 0xDB, 0xB2),
        state.InputState.HOVER: Color(0xEB, 0xDB, 0xB2),
        state.InputState.CLICK: Color(0xEB, 0xDB, 0xB2),
    }
    # Idle text/glyph color on modifier keys (Steam's --key-meta-color); some
    # skins differ from the normal --key-color here (e.g. TotallyTubular).
    modifier_text_color = Color(0xEB, 0xDB, 0xB2)
    # Subtle top-edge highlight to fake a 3D bevel on each key.
    key_highlight_color = Color(0x22, 0x22, 0x22)
    # Color for the small "shadow" label that previews a key's shifted form
    # (also the Move-key label and the arrow keys' ▲▼ previews, via skin:shadow).
    # A tint of the stock text color blended toward the background; _apply_skin
    # recomputes the same tint from the active skin's text color.
    shadow_label_color = Color(0x87, 0x80, 0x70)
    # Theme accent color (toggle-on / highlight) — used to tint the touchpad
    # cursor disc and glow so the pointer carries the keyboard's accent hue.
    # Stock default: the Big-Picture blue accent.
    accent_color = Color(0x1A, 0x9F, 0xFF)

    # Seconds for the Shift slide/fade transition on dual-state keys. Driven by
    # wall-clock time in render_vkb so it's frame-rate independent.
    _SHIFT_ANIM_DUR = 0.074

    # Transparent-mode text outline defaults (per-key overridable via the YAML
    # `outline_opacity` / `outline_px`). Opacity 0..1 (lower = finer); px is the
    # sub-pixel offset of the outline ring (larger = thicker).
    _OUTLINE_ALPHA = 0.40
    _OUTLINE_PX = 0.3

    # Standardized OSK font sizes (design-time px, scaled by _font_scale). One
    # size per key class — no per-key overrides anywhere.
    _FONT_SIZE = 26  # letter keys (font_manager)
    _FONT_SIZE_MOD = (
        20  # modifier / function / nav / inline keys (font_manager_small)
    )
    _FONT_SIZE_SYM = 20  # symbol (arrow) keys
    _FONT_SIZE_DUAL = 20  # dual-state active label (numbers/punctuation)
    _SHIFTED_RATIO = 0.85  # grayed shifted-preview text vs the active label
    _INLINE_GAP = 14  # label↔icon spacing on inline label+icon keys
    _INLINE_ICON_RATIO = 1.0  # inline icon height vs the label line height

    def __init__(self):
        # Apply the tray-selected OSK size ("Keyboard Skin -> Size"). Updates
        # the module-level width/height (read by CoordFraction and vkb's key
        # layout) for THIS window. `_font_scale` scales font/glyph sizes by
        # the same ratio so "Small"/"Full Screen" stay proportional to the
        # original 1286x369 "Default" look instead of just changing the grid.
        global width, height
        width, height = resize_for_layout()
        # Mark the layout dirty: this construction may have changed the module
        # dims from whatever the VirtualKeyboard was built against (e.g. the
        # tray pre-warm failed and main() built a fresh Screen at a non-default
        # OSK size). The window is non-resizable, so no SDL resize event will
        # fire to trigger update_dimensions — without this flag the first loop
        # iteration would skip the recompute and keys would render/input at
        # stale geometry for the whole session.
        self._resize_dirty = True
        self._font_scale = height / _BASE_HEIGHT

        # Apply the user-selected Steam OSK skin (overrides the built-in
        # palette class attributes with per-instance ones). Done first so the
        # opening clear() and every render use the skin colors.
        self._apply_skin(skins.get_active_skin())
        self._skin_generation = skins.get_generation()
        # Make sure SDL never auto-activates the window when it gets shown,
        # so opening the OSK doesn't steal focus from the user's target app
        # (e.g. a browser address bar / YouTube search field). "0" = do not
        # activate on show (the SDL3 successor to the old NO_ACTIVATION hint).
        S.SDL_SetHint(S.SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN, b"0")
        # Touchscreen support: let SDL synthesize mouse events from finger
        # input (SDL3 defaults to touch-only events), so a touchscreen can
        # point/type at the OSK exactly like a mouse.
        S.SDL_SetHint(S.SDL_HINT_TOUCH_MOUSE_EVENTS, b"1")

        # Anchor the window to the bottom-center of the primary display's
        # usable area (i.e. above the taskbar).
        bounds = S.SDL_Rect()
        disp = S.SDL_GetPrimaryDisplay()
        if disp and S.SDL_GetDisplayUsableBounds(disp, ctypes.byref(bounds)):
            win_x = bounds.x + max(0, (bounds.w - width) // 2)
            win_y = bounds.y + max(0, bounds.h - height)
        else:
            win_x = S.SDL_WINDOWPOS_CENTERED
            win_y = S.SDL_WINDOWPOS_CENTERED
        # SDL_WINDOW_HIDDEN normally lets us apply the WS_EX_NOACTIVATE bit
        # BEFORE the window is ever visible; otherwise the very first paint can
        # steal focus regardless of subsequent style changes. When the
        # always-visible focus-flash fix is active we instead create the window
        # VISIBLE but parked far off-screen (see _OFFSCREEN_*), so the first
        # paint is invisible, NOACTIVATE is applied before it ever shows on a
        # display, and no ShowWindow is ever needed later. SDL3's
        # SDL_CreateWindow drops the x/y args, so the position is set
        # separately right after creation.
        # Create transparency-capable (layered/composited) so the tray's
        # "Transparent" toggle can switch the OSK to a see-through background +
        # translucent keys live, without recreating the window. When the toggle
        # is off we just clear to the opaque background, so it looks identical to
        # an ordinary window. Fall back to an opaque window if the platform
        # rejects the transparent flag (transparency then simply unavailable).
        _always_visible = state.is_osk_always_visible()
        _base_flags = S.SDL_WINDOW_BORDERLESS
        if not _always_visible:
            _base_flags |= S.SDL_WINDOW_HIDDEN
        # SDL_WINDOW_UTILITY (Win32 WS_EX_TOOLWINDOW): the OSK must NEVER
        # appear in Alt+Tab — not even while "closed" (the always-visible
        # mode parks it off-screen, but a WS_VISIBLE top-level window still
        # shows in the Alt+Tab list unless it's a tool window). Creating the
        # window WITH the utility flag makes SDL own that ex-style bit, so
        # it survives SDL re-applying its own window styles (a manually-set
        # bit, applied after creation by _make_window_non_activating, can be
        # dropped when SDL restyles — observed as the parked keyboard
        # showing in Alt+Tab).
        _base_flags |= S.SDL_WINDOW_UTILITY
        self.window = S.SDL_CreateWindow(
            b"", width, height, _base_flags | S.SDL_WINDOW_TRANSPARENT
        )
        if not self.window:
            self.window = S.SDL_CreateWindow(b"", width, height, _base_flags)
        if not self.window:
            raise RuntimeError("SDL_CreateWindow failed: " + S.get_error())
        if _always_visible:
            # Park off-screen BEFORE the first present: the tray pre-warms this
            # Screen at startup, so construction must never show anything.
            S.SDL_SetWindowPosition(self.window, _OFFSCREEN_X, _OFFSCREEN_Y)
        else:
            S.SDL_SetWindowPosition(self.window, win_x, win_y)
        self.renderer = S.SDL_CreateRenderer(self.window, None)
        if not self.renderer:
            raise RuntimeError("SDL_CreateRenderer failed: " + S.get_error())
        # Blend so the alpha in glyph/text textures composites over the keys.
        S.SDL_SetRenderDrawBlendMode(self.renderer, S.SDL_BLENDMODE_BLEND)

        # Use Windows' Segoe UI Semibold (Steam Big Picture's keyboard font)
        # when available; otherwise try common Linux system fonts; fall back
        # to the bundled DejaVu as a last resort.
        win_candidates = (
            _FONT_CANDIDATES_WIN if sys.platform == "win32" else []
        )
        linux_candidates = (
            _FONT_CANDIDATES_LINUX if sys.platform != "win32" else []
        )
        # Use the BUNDLED Selawik Semibold — Microsoft's SIL-OFL, metric-compatible
        # Segoe UI substitute (so it looks like Steam's keyboard) — so the OSK font
        # is identical on every platform AND freely redistributable (Segoe UI and
        # Steam's Motiva Sans are proprietary and can't be shipped). System
        # Segoe / Linux fonts and bundled DejaVu stay as fallbacks only.
        font_path = resources.find_data_resource(
            "fonts/Selawik-Semibold.ttf"
        ) or _first_existing(win_candidates + linux_candidates)
        if font_path is None:
            font_name = "fonts/DejaVuSansCondensed-Bold.ttf"
            font_path = resources.find_data_resource(font_name)
            assert font_path is not None, (
                f"Could not find font file `{font_name}`!"
            )
        print(f"Found font file at `{font_path}`")
        self.font_manager = _Font(font_path, self._scaled(self._FONT_SIZE))
        # Modifier keys (Tab, Caps, Shift, Enter, Backspace) wear a smaller label.
        self.font_manager_small = _Font(
            font_path, self._scaled(self._FONT_SIZE_MOD)
        )

        # Symbol glyphs (⌫ ␣ ◀ ▶ ▲ ▼): Selawik is a UI font and doesn't include
        # them, so use the bundled Noto Sans Symbols 2 (OFL, verified to cover
        # them all, identical on every platform). DejaVu Sans stays as the
        # fallback for older builds that may not have the Noto font yet.
        sym_win = _SYM_CANDIDATES_WIN if sys.platform == "win32" else []
        sym_linux = _SYM_CANDIDATES_LINUX if sys.platform != "win32" else []
        sym_path = (
            resources.find_data_resource("fonts/NotoSansSymbols2-Regular.ttf")
            or resources.find_data_resource(
                "fonts/DejaVuSansCondensed-Bold.ttf"
            )
            or _first_existing(sym_win + sym_linux)
        )
        if sym_path is None:
            sym_path = font_path
        self.font_manager_symbol = _Font(
            sym_path, self._scaled(self._FONT_SIZE)
        )
        self.font_manager_symbol_small = _Font(
            sym_path, self._scaled(self._FONT_SIZE_SYM)
        )
        # Shadow labels (small grey previews of a key's shifted variant).
        self.font_manager_shadow = _Font(
            font_path,
            self._scaled(int(self._FONT_SIZE_DUAL * self._SHIFTED_RATIO)),
        )
        self.font_manager_shadow_symbol = _Font(
            sym_path,
            self._scaled(int(self._FONT_SIZE_DUAL * self._SHIFTED_RATIO)),
        )

        # Cache for one-off custom-size fonts keyed by (font_name, size).
        self._font_paths = {"default": font_path, "symbol": sym_path}
        self._font_cache = {}

        # Cache of glyph PNGs (button hint icons) keyed by filename.
        self._glyph_textures = {}
        # Cache of rendered key-label textures keyed by (font object, text,
        # color). The old path re-rasterized every label via FreeType EVERY
        # FRAME — ~90 keys x 1-2 labels x 120 fps = tens of thousands of
        # rasterizations + texture create/destroy churn per second while a
        # game runs; that was the in-game "unresponsive keyboard". Colors
        # change with the skin (new cache keys), the font scale with the
        # window size (new Screen), so nothing needs explicit invalidation.
        # Freed in destroy_textures (renderer teardown).
        self._text_cache = {}
        # Dirty-frame gate: the render loop skips render+present while the
        # content signature is unchanged, so an idle keyboard costs ~0 CPU/GPU
        # inside a game. None forces the first frame (main loop resets it on
        # show so a re-open always paints once).
        self._last_sig = None
        self._resize_dirty = False
        # Caps-Lock read cached per render frame (see content_changed): the
        # Win32 GetKeyState call is made once per frame instead of twice (the
        # dirty-gate signature + render_vkb). None = not computed this frame —
        # render_vkb falls back to a fresh read (open/close-anim frames).
        self._caps_on = None
        # NOTE: no SDL3_image — every glyph/skin PNG loads through Pillow and
        # uploads via SDL_CreateSurfaceFrom (see _load_glyph_texture).
        # Glyph cache resolution, scaled with the OSK size so "Full Screen"
        # icons stay crisp and "Small" ones aren't oversampled.
        self._glyph_cache_px = max(32, self._scaled(self._GLYPH_CACHE_PX))

        # Shift slide/fade animation: eased progress 0 (unshifted) → 1 (shifted),
        # advanced each frame in render_vkb. `None` timestamp = snap to the live
        # shift state on the first frame (no animation when the OSK first opens).
        self._shift_anim = 0.0
        self._shift_anim_t = None

        # Per-key press "pop": when a key enters the CLICK state it quickly
        # scales down to _PRESS_SCALE, then springs back to 1.0 on release
        # (the iOS press feel). Maps (row, col) -> [phase, start_time] where
        # phase is 1 (pressing down) or 0 (springing back). Advanced each frame
        # in render_vkb; empty when idle so it never gates the dirty check.
        self._press_anim = {}
        # Quick press-in duration (s) and the press scale target. iOS shrinks
        # ~2-3% on press; 0.93 opened a ~3px black ring around the key (the
        # board showing through the gap) that read as a glitch. 0.97 leaves a
        # sub-perceptual ~1px sink edge.
        self._PRESS_SCALE = 0.97
        self._PRESS_IN_DUR = 0.08
        # Release spring: damping ratio / natural freq (see utils.spring_p).
        self._PRESS_ZETA = 0.6
        self._PRESS_OMEGA0 = 28.0

        # Offscreen render target for the OSK OPEN animation (lazily created in
        # render_open_anim). The keyboard is drawn into it at full opacity, then
        # blitted to the window faded (alpha-mod) + clipped (reveal). None until
        # first used / if the GPU can't make a target texture (then no animation).
        self._anim_target = None

        # Transparency (tray "Keyboard Skin → Transparent" submenu). Cached here
        # and refreshed in maybe_reload_skin so an open keyboard switches live.
        # `_tscale` is the level's global opacity multiplier folded into every
        # transparent-mode alpha (text/icons/fills/outlines) so the dialed-in
        # ratios stay fixed and only the overall level changes.
        self._transparent = skins.is_transparent()
        self._tscale = skins.get_transparency_scale()
        # The font (readable text) is ALWAYS full opacity — the transparency
        # level never scales it; only fills, icons and outlines scale.
        self._text_alpha = 255
        self._icon_alpha = (
            min(255, int(round(204 * self._tscale)))
            if self._transparent
            else 255
        )

        self.clear()
        # NOTE: with the always-visible fix the window is VISIBLE but parked
        # far off-screen; otherwise it stays SDL_WINDOW_HIDDEN. In both cases
        # triton.main() applies the WS_EX_NOACTIVATE bit, and "showing" moves it
        # on-screen without a ShowWindow (always-visible) or uses the Win32
        # no-activate path (hidden). Showing it here would activate it.

    def _apply_skin(self, name):
        """Override the built-in color palette with the selected skin's colors.
        Sets instance attributes that shadow the Screen class defaults, so a
        missing/unparseable skin simply leaves the stock palette in place."""
        pal = skins.load_palette(name)
        if not pal:
            return
        self.bg_color = pal["bg"]
        self.key_color = {
            state.InputState.INACTIVE: pal["key_inactive"],
            state.InputState.HOVER: pal["key_hover"],
            state.InputState.CLICK: pal["key_click"],
        }
        self.modifier_idle_color = pal["modifier"]
        self.text_color = {
            state.InputState.INACTIVE: pal["text_inactive"],
            state.InputState.HOVER: pal["text_hover"],
            # CLICK = held/pressed highlight: the toggle-on text color, chosen
            # to contrast the toggle-on fill (key_color[CLICK]).
            state.InputState.CLICK: pal["text_click"],
        }
        self.modifier_text_color = pal["text_modifier"]
        self.key_highlight_color = pal["highlight"]
        # Theme accent (toggle-on fill) tints the touchpad cursor disc + glow.
        self.accent_color = pal["key_click"]
        # The shifted-preview ("gray") label is a TINT of the theme's text
        # color, blended toward the background — a translucent-looking version
        # that keeps the theme's hue on every skin instead of a flat grey.
        self.shadow_label_color = self._lerp_color(
            self.text_color[state.InputState.INACTIVE], self.bg_color, 0.5
        )

    def _resolve_skin_color(self, c):
        """Resolve a per-key color override (see vkb._decode_key_color): a
        Color passes through unchanged; a 'skin:ROLE' marker maps to the
        active skin's palette so the key follows skin changes instead of a
        frozen literal (e.g. the Move key uses 'skin:key' / 'skin:shadow')."""
        if not isinstance(c, str):
            return c
        role = c[5:] if c.startswith("skin:") else c
        if role == "bg":
            return self.bg_color
        if role == "accent":
            return self.key_color[state.InputState.CLICK]
        if role == "text":
            return self.text_color[state.InputState.INACTIVE]
        if role == "modifier":
            return self.modifier_idle_color
        if role == "shadow":
            return self.shadow_label_color
        # "key" and any unknown role fall back to the normal idle key color.
        return self.key_color[state.InputState.INACTIVE]

    def content_changed(self, virtual_kb, pointers):
        """True if the next frame would draw something different from the last
        presented one. The render loop skips render+present entirely while
        False, so an idle keyboard burns ~0 CPU/GPU inside a game — the old
        code full-redrew at 120 fps no matter what. The signature must cover
        every pixel-affecting input render_vkb/render_ptr read (keep in sync
        if new reads appear)."""
        # One Win32 GetKeyState per frame, shared with render_vkb (see
        # self._caps_on).
        self._caps_on = state.is_caps_on()
        # The press-anim elapsed signature only matters while keys are in
        # flight; idle (empty) it is a constant (), so skip building it.
        press_sig = (
            tuple(
                sorted(
                    (k, int((time.monotonic() - t0) * 120.0))
                    for k, (_phase, t0) in self._press_anim.items()
                )
            )
            if self._press_anim
            else ()
        )
        sig = (
            state.get_cursor(),
            pointers[0].state,
            pointers[0].coord_frac.x_fraction,
            pointers[0].coord_frac.y_fraction,
            pointers[1].state,
            pointers[1].coord_frac.x_fraction,
            pointers[1].coord_frac.y_fraction,
            state.get_mouse_press_cell(),
            state.get_highlighted(),
            state.is_shift_held(),
            self._caps_on,
            state.is_lpad_touched(),
            state.is_rpad_touched(),
            skins.get_generation(),
            self._transparent,
            self._tscale,
            int(self._shift_anim * 32),  # in-flight shift slide (eased 0..1)
            press_sig,
            self._resize_dirty,
            # Feature-B variant row: the whole session (base, candidates,
            # highlighted index, strip rect, source) changes whenever the
            # highlight moves, so an open row repaints on every selection
            # change (and closes paint it away).
            state.get_diacritic(),
        )
        if sig != self._last_sig:
            self._last_sig = sig
            return True
        return False

    def destroy_textures(self):
        """Free the cached label textures — called before renderer teardown
        (the tray's cached-screen path reuses them across opens). Also resets
        the dirty-gate so the next open paints its first frame."""
        for tex, _w, _h in self._text_cache.values():
            if tex:
                S.SDL_DestroyTexture(tex)
        self._text_cache.clear()
        self._last_sig = None

    def _scaled(self, px):
        """Scale a design-time pixel size (font point size, glyph cache
        resolution, ...) by `_font_scale` so "Small"/"Full Screen" keep the
        same proportions as the 1286x369 "Default" layout."""
        return max(1, int(round(px * self._font_scale)))

    def maybe_reload_skin(self):
        """Re-apply the palette if the tray changed the active skin since the
        last frame, so an open keyboard switches skins live. Runs on the render
        thread (called from triton.main's loop), so the color swap never races
        the renderer. Cheap no-op otherwise — one int compare per frame."""
        gen = skins.get_generation()
        if gen != self._skin_generation:
            self._skin_generation = gen
            self._apply_skin(skins.get_active_skin())
            self._transparent = skins.is_transparent()
            self._tscale = skins.get_transparency_scale()
            self._text_alpha = 255  # font opacity never scales (always 100%)
            self._icon_alpha = (
                min(255, int(round(204 * self._tscale)))
                if self._transparent
                else 255
            )

    def clear(self):
        if self._transparent:
            # Erase to alpha 0 so the desktop shows through — the background
            # solid is removed entirely. RenderClear writes the draw color
            # (including alpha) directly, ignoring the blend mode.
            S.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 0)
        else:
            c = self.bg_color
            S.SDL_SetRenderDrawColor(self.renderer, c.r, c.g, c.b, 255)
        S.SDL_RenderClear(self.renderer)

    def _render_split_background(self, virtual_kb):
        """Split layout, opaque mode: erase everything to alpha 0 and repaint
        ONLY the two keyboard halves — the middle gap stays fully transparent
        so the desktop shows through the band (the "no black in between" split
        look) instead of the window's opaque fill. Transparent mode already
        erases everywhere, so this is a no-op there (the halves keep their
        translucent key fills)."""
        band = virtual_kb.split_gap_band()
        if band is None:
            return
        left_x, right_x = band
        c = self.bg_color
        S.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 0)
        S.SDL_RenderClear(self.renderer)
        S.SDL_SetRenderDrawColor(self.renderer, c.r, c.g, c.b, 255)
        S.SDL_RenderFillRect(
            self.renderer,
            ctypes.byref(
                S.SDL_FRect(0.0, 0.0, float(left_x), float(height))
            ),
        )
        S.SDL_RenderFillRect(
            self.renderer,
            ctypes.byref(
                S.SDL_FRect(
                    float(right_x), 0.0, float(width - right_x), float(height)
                )
            ),
        )

    # Glyph cache target size. Source PNGs are 128–240 px but get drawn at
    # ~30–50 px on screen — an 8× one-step GPU downscale aliases hard even
    # with linear filtering. Pre-resampling to ~2× the typical draw size
    # with PIL/LANCZOS, then letting the GPU do the final tiny rescale,
    # matches the quality Steam's own keyboard renders at.
    _GLYPH_CACHE_PX = 96

    def _get_glyph(self, name):
        """Load (and cache) a controller-button glyph PNG by basename: from
        the Steam install (steam_assets) first, then the bundled copy for
        original assets (touch-circle/glow, smiley) Steam doesn't ship."""
        if name in self._glyph_textures:
            return self._glyph_textures[name]
        tex = None
        size = (0, 0)
        try:
            from steam_assets import find_glyph_path

            path = find_glyph_path(name)
        except Exception:
            path = None
        if path is None:
            path = resources.find_data_resource("images/glyphs/" + name)
        if path is not None:
            tex, size = self._load_glyph_texture(path)
        else:
            print(f"Glyph not found: images/glyphs/{name}")
        entry = (tex, size)
        self._glyph_textures[name] = entry
        return entry

    @staticmethod
    def _normalize_glyph(pil):
        """Glyphs are tinted with a multiply (SetTextureColorMod), which can
        only darken. A glyph that bakes in dark detail (e.g. sc_r2_md's black
        "R2" on a light button) therefore breaks on hover: the body darkens to
        match the tint and the already-black detail merges into it and vanishes.

        Fix such glyphs by flattening them to a white silhouette whose alpha
        tracks luminance — the dark detail drops to zero alpha and becomes a
        transparent cut-out that shows the key background through it, so it
        inverts correctly in every state (dark-on-light idle, light-on-dark
        hover). Pure white-on-transparent glyphs have no dark opaque pixels and
        are returned unchanged."""
        r, g, b, a = pil.split()
        lum = PILImage.merge("RGB", (r, g, b)).convert("L")

        def _white_if_dark(v: int) -> int:
            return 255 if v < 64 else 0

        def _white_if_opaque(v: int) -> int:
            return 255 if v > 128 else 0

        dark = lum.point(_white_if_dark)
        opaque = a.point(_white_if_opaque)
        dark_opaque = PILImageChops.multiply(dark, opaque).histogram()[255]
        if dark_opaque < 50:
            return pil
        # alpha' = alpha * luminance/255 → light body stays, dark detail cut out.
        new_alpha = PILImageChops.multiply(a, lum)
        white = PILImage.new("RGB", pil.size, (255, 255, 255))
        white.putalpha(new_alpha)
        return white

    def _load_glyph_texture(self, path):
        """Open a glyph PNG and upload it as an SDL texture: flatten to a
        white silhouette, LANCZOS-downsample to the cache target, then upload
        with linear filtering for the final on-screen blit."""
        try:
            pil = PILImage.open(path).convert("RGBA")
        except OSError:
            return None, (0, 0)
        pil = self._normalize_glyph(pil)
        if max(pil.size) > self._glyph_cache_px:
            pil.thumbnail(
                (self._glyph_cache_px, self._glyph_cache_px),
                PILImage.Resampling.LANCZOS,
            )
        w, h = pil.size
        data = pil.tobytes()
        # Keep `buf` alive until CreateTextureFromSurface copies the pixels —
        # SDL_CreateSurfaceFrom references the buffer, it does not own it.
        buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        # SDL_PIXELFORMAT_ABGR8888 matches PIL's RGBA byte order on
        # little-endian (red byte first in memory).
        surf = S.SDL_CreateSurfaceFrom(
            w,
            h,
            S.SDL_PIXELFORMAT_ABGR8888,
            ctypes.cast(buf, ctypes.c_void_p),
            w * 4,
        )
        if not surf:
            return None, (0, 0)
        tex = S.SDL_CreateTextureFromSurface(self.renderer, surf)
        S.SDL_DestroySurface(surf)
        if tex:
            S.SDL_SetTextureScaleMode(tex, S.SDL_SCALEMODE_LINEAR)
            S.SDL_SetTextureBlendMode(tex, S.SDL_BLENDMODE_BLEND)
        return tex, (w, h)

    def _get_sized_font(self, font_name, size):
        size = self._scaled(size)
        key = (font_name, size)
        fm = self._font_cache.get(key)
        if fm is None:
            path = self._font_paths.get(font_name, self._font_paths["default"])
            fm = _Font(path, size)
            self._font_cache[key] = fm
        return fm

    def _make_text_texture(self, font_obj, text, color):
        """Return a cached texture for `text` rasterized with `font_obj` in
        `color`, as (texture, w, h) — or (None, 0, 0) for empty text. The
        caller draws it and MUST NOT destroy it: textures are cached and
        reused across frames (the label set is small and static per open), so
        FreeType runs once per label instead of every frame. Font objects are
        themselves cached for the Screen's lifetime, so they're safe dict
        keys. Draw sites set their own alpha/color mod before blitting, so a
        shared texture is safe to draw at different opacities."""
        key = (font_obj, text, color.r, color.g, color.b)
        hit = self._text_cache.get(key)
        if hit is not None:
            return hit
        surf = font_obj.render_surface(text, color)
        if not surf:
            return None, 0, 0
        w = surf.contents.w
        h = surf.contents.h
        tex = S.SDL_CreateTextureFromSurface(self.renderer, surf)
        S.SDL_DestroySurface(surf)
        if not tex:
            return None, 0, 0
        S.SDL_SetTextureBlendMode(tex, S.SDL_BLENDMODE_BLEND)
        entry = (tex, w, h)
        self._text_cache[key] = entry
        return entry

    def render_key(
        self,
        txt,
        key,
        key_state,
        modifier=False,
        align="center",
        valign="center",
        glyph=None,
        font="default",
        text_color_override=None,
        bg_color_override=None,
        shadow_label=None,
        font_size=None,
        text_dx=0,
        shadow_font_size=None,
        dual_anim=None,
        outline_px=None,
        outline_opacity=None,
    ):
        if (
            bg_color_override is not None
            and key_state == state.InputState.INACTIVE
        ):
            fill = self._resolve_skin_color(bg_color_override)
        elif modifier and key_state == state.InputState.INACTIVE:
            fill = self.modifier_idle_color
        else:
            fill = self.key_color[key_state]
        # Effective label + glyph color for this key & state, used for BOTH the
        # text label and any glyph icon so they always match. Modifier keys wear
        # their own idle (meta) text color; held/clicked keys (CLICK) use the
        # toggle-on text color so the label/glyph stays legible on the highlight.
        if (
            text_color_override is not None
            and key_state == state.InputState.INACTIVE
        ):
            label_color = self._resolve_skin_color(text_color_override)
        elif key_state == state.InputState.INACTIVE and modifier:
            label_color = self.modifier_text_color
        else:
            label_color = self.text_color[key_state]
        # Glyph (button-hint icon) tint: same as the label EXCEPT a text-color
        # override never applies to the icon. The Move key's text is forced to
        # the grey shadow color, but its keyboard icon must stay white/meta.
        if key_state == state.InputState.INACTIVE and modifier:
            glyph_color = self.modifier_text_color
        else:
            glyph_color = self.text_color[key_state]
        # Flat fill, then (opaque mode only) a 1-px top highlight rim for a
        # subtle raised look. Transparent mode: the idle key fill is 45% opacity
        # and the hover/click highlight 90%, and the bevel rim is omitted.
        if self._transparent:
            base_a = 115 if key_state == state.InputState.INACTIVE else 230
            fill_a = min(255, int(round(base_a * self._tscale)))
        else:
            fill_a = 255
        S.SDL_SetRenderDrawColor(self.renderer, fill.r, fill.g, fill.b, fill_a)
        S.SDL_RenderFillRect(
            self.renderer,
            ctypes.byref(S.SDL_FRect(key.x, key.y, key.w, key.h)),
        )
        if not self._transparent:
            hi = self.key_highlight_color
            S.SDL_SetRenderDrawColor(self.renderer, hi.r, hi.g, hi.b, 255)
            S.SDL_RenderLine(
                self.renderer, key.x, key.y, key.x + key.w - 1, key.y
            )

        # Inline label + icon: a key with BOTH a text label and a shortcut
        # glyph ("Backspace X", "Caps L3", "Space Y") draws them side by side,
        # centered as one unit, instead of the icon perched at the bottom edge
        # opposite the label. Dual-anim and shadow keys never reach here (they
        # are handled below).
        if glyph and txt and dual_anim is None and not shadow_label:
            self._render_inline_label(
                key,
                txt,
                glyph,
                label_color,
                glyph_color,
                font,
                font_size,
                outline_px,
                outline_opacity,
            )
            return

        # Glyph icon (controller button hint), tinted to match the label.
        if glyph:
            self._draw_glyph(key, glyph, align, txt, glyph_color)

        # Dual-state key mid-Shift-transition: slide both forms downward and
        # cross-fade instead of the static shadow-above-main stack. Replaces the
        # shadow_label + main-label path below (see render_vkb / _shift_anim).
        if dual_anim is not None:
            self._render_dual_anim(
                key, dual_anim, label_color, glyph_color, font, align, text_dx
            )
            return

        # Shadow label: small grey preview of the shifted variant, centered
        # above the main label. Forces the main label to bottom alignment so
        # the two stack cleanly.
        if shadow_label:
            # Shadow size: explicit shadow_font_size wins (lets a key shrink its
            # preview independently — e.g. the arrows' ▲▼); else the key's
            # font_size; else the default shadow font.
            sh_size = (
                shadow_font_size if shadow_font_size is not None else font_size
            )
            if sh_size is not None:
                shadow_font_obj = self._get_sized_font(font, sh_size)
            else:
                shadow_font_obj = (
                    self.font_manager_shadow_symbol
                    if font == "symbol"
                    else self.font_manager_shadow
                )
            sh_tex, sh_tw, sh_th = self._make_text_texture(
                shadow_font_obj, shadow_label, self.shadow_label_color
            )
            if sh_tex:
                # Cached texture: pin full opacity (it's only drawn here).
                S.SDL_SetTextureAlphaMod(sh_tex, 255)
                sh_x = key.x + (key.w - sh_tw) // 2
                sh_y = key.y + 3
                sh_dst = S.SDL_FRect(sh_x, sh_y, sh_tw, sh_th)
                S.SDL_RenderTexture(
                    self.renderer, sh_tex, None, ctypes.byref(sh_dst)
                )

        # We don't need to continue rendering text if there's nothing to render!
        if txt == "":
            return

        if font_size is not None:
            font_obj = self._get_sized_font(font, font_size)
        elif font == "symbol":
            font_obj = (
                self.font_manager_symbol_small
                if modifier
                else self.font_manager_symbol
            )
        else:
            font_obj = (
                self.font_manager_small if modifier else self.font_manager
            )
        tex, tw, th = self._make_text_texture(font_obj, txt, label_color)
        if not tex:
            return

        # Text labels are always centered in the button, regardless of the
        # per-key align/valign edge overrides (those now only steer the glyph
        # icon placement, not the label).
        text_x = key.x + (key.w - tw) // 2 + text_dx
        text_y = key.y + (key.h - th) // 2

        self._draw_text_outline(
            font_obj,
            txt,
            label_color,
            text_x,
            text_y,
            255,
            outline_px,
            outline_opacity,
        )
        S.SDL_SetTextureAlphaMod(tex, self._text_alpha)
        dst = S.SDL_FRect(text_x, text_y, tw, th)
        S.SDL_RenderTexture(self.renderer, tex, None, ctypes.byref(dst))

    def _render_inline_label(
        self,
        key,
        txt,
        glyph,
        label_color,
        glyph_color,
        font,
        font_size,
        outline_px,
        outline_opacity,
    ):
        """Draw a key's text label and its controller-button icon on ONE line,
        centered as a unit ("Backspace X", "Caps L3", "Space Y"). The glyph is
        sized to sit beside the label's cap height and vertically centered with
        it. Used by keys whose label and shortcut hint belong together. Inline
        keys are word-label keys, so they always use the modifier-size standard
        (unless a font_size override is set)."""
        if font_size is not None:
            font_obj = self._get_sized_font(font, font_size)
        elif font == "symbol":
            font_obj = self.font_manager_symbol_small
        else:
            font_obj = self.font_manager_small
        tex, tw, th = self._make_text_texture(font_obj, txt, label_color)
        if not tex:
            return
        gtex, (gw, gh) = self._get_glyph(glyph)
        if gtex is None:
            return
        # Glyph height tracks the label's line height; width keeps aspect.
        gh_draw = int(th * self._INLINE_ICON_RATIO)
        gw_draw = int(gw * (gh_draw / gh)) if gh else gh_draw
        gap = self._INLINE_GAP
        total_w = tw + gap + gw_draw
        x0 = key.x + (key.w - total_w) // 2
        # Vertically center the text + icon as ONE unit inside the key, and
        # center each element within that unit so they sit on the same line.
        unit_h = max(th, gh_draw)
        unit_top = key.y + (key.h - unit_h) // 2
        text_y = unit_top + (unit_h - th) // 2
        glyph_y = unit_top + (unit_h - gh_draw) // 2
        # Icon tint + opacity, matching the label's color.
        S.SDL_SetTextureColorMod(
            gtex, glyph_color.r, glyph_color.g, glyph_color.b
        )
        S.SDL_SetTextureAlphaMod(gtex, self._icon_alpha)
        gdst = S.SDL_FRect(x0 + tw + gap, glyph_y, gw_draw, gh_draw)
        S.SDL_RenderTexture(self.renderer, gtex, None, ctypes.byref(gdst))
        # Label text, with outline + opacity like the normal text path.
        self._draw_text_outline(
            font_obj,
            txt,
            label_color,
            x0,
            text_y,
            255,
            outline_px,
            outline_opacity,
        )
        S.SDL_SetTextureAlphaMod(tex, self._text_alpha)
        dst = S.SDL_FRect(x0, text_y, tw, th)
        S.SDL_RenderTexture(self.renderer, tex, None, ctypes.byref(dst))

    def _draw_glyph(
        self, key, glyph, align, txt, glyph_color, dy=0.0, alpha=255
    ):
        """Draw a controller-button glyph icon on `key`, tinted to `glyph_color`.
        For keys with no text label the glyph is the centerpiece (big, centered);
        otherwise it sits at the bottom on the opposite side from the label. `dy`
        shifts it vertically and `alpha` scales its opacity — used by the Shift
        animation to slide the Move key's keyboard icon down and fade it out."""
        gtex, (gw, gh) = self._get_glyph(glyph)
        if gtex is None:
            return
        # Centerpiece: glyph-only key with no specific edge alignment — the glyph
        # IS the button's content, so it goes big & centered.
        is_centerpiece = txt == "" and align == "center"
        gh_draw = int(key.h * (0.54 if is_centerpiece else 0.46))
        gw_draw = int(gw * (gh_draw / gh)) if gh else gh_draw
        # Horizontal placement: for glyph-only keys (empty label) the `align`
        # value picks the side directly; for keys with text the glyph sits on
        # the opposite side from the label.
        edge_pad = 12
        if txt == "":
            if align == "left":
                gx = key.x + edge_pad
            elif align == "right":
                gx = key.x + key.w - gw_draw - edge_pad
            else:
                gx = key.x + (key.w - gw_draw) // 2
        elif align == "left":
            gx = key.x + key.w - gw_draw - edge_pad
        elif align == "right":
            gx = key.x + edge_pad
        else:
            gx = key.x + (key.w - gw_draw) // 2
        if is_centerpiece:
            gy = key.y + (key.h - gh_draw) // 2
        else:
            gy = key.y + key.h - gh_draw - 6
        # The glyph PNGs are white-on-transparent; tint to the label color so
        # they track the skin AND stay legible in every state. Alpha is always
        # set (default 255) so a faded Move icon never leaks onto reused glyphs.
        S.SDL_SetTextureColorMod(
            gtex, glyph_color.r, glyph_color.g, glyph_color.b
        )
        # Fold in the icon opacity (80% in transparent mode) on top of any
        # animation alpha (the Move icon's fade).
        S.SDL_SetTextureAlphaMod(gtex, alpha * self._icon_alpha // 255)
        dst = S.SDL_FRect(gx, gy + dy, gw_draw, gh_draw)
        S.SDL_RenderTexture(self.renderer, gtex, None, ctypes.byref(dst))

    def _draw_text_outline(
        self, font_obj, txt, color, x, y, alpha, px=None, opacity=None
    ):
        """Transparent mode only: draw a hairline outline behind a text label in
        the inverse of the font color, by blitting the text at the 8 neighbour
        offsets. `px`/`opacity` override the per-key defaults (thicker for the
        arrows, fainter for the modifier/bracket keys). No-op when opaque."""
        if not self._transparent or not txt:
            return
        opacity = self._OUTLINE_ALPHA if opacity is None else opacity
        o = self._OUTLINE_PX if px is None else px
        inv = Color(255 - color.r, 255 - color.g, 255 - color.b)
        otex, ow, oh = self._make_text_texture(font_obj, txt, inv)
        if not otex:
            return
        # Semi-transparent outline reads as a finer, softer edge than a solid
        # one; scaled by the label's own alpha so it fades with an animating
        # label. Sub-pixel offsets keep it a hairline (text is linearly filtered).
        S.SDL_SetTextureAlphaMod(
            otex, min(255, int(alpha * opacity * self._tscale))
        )
        for ox, oy in (
            (-o, -o),
            (0, -o),
            (o, -o),
            (-o, 0),
            (o, 0),
            (-o, o),
            (0, o),
            (o, o),
        ):
            d = S.SDL_FRect(x + ox, y + oy, ow, oh)
            S.SDL_RenderTexture(self.renderer, otex, None, ctypes.byref(d))

    @staticmethod
    def _lerp_color(c0, c1, t):
        return Color(
            int(round(c0.r + (c1.r - c0.r) * t)),
            int(round(c0.g + (c1.g - c0.g) * t)),
            int(round(c0.b + (c1.b - c0.b) * t)),
        )

    def _render_dual_anim(
        self, key, spec, label_color, glyph_color, font, align, text_dx
    ):
        """Draw a dual-state key mid-Shift-transition.

        `spec["progress"]` runs 0 (unshifted) → 1 (shifted). The "upper" form
        (`spec["shifted"]`) slides from its grey top-perch down into the center
        while its color fades up from grey to the full label color; the "lower"
        form slides down by the same distance and fades out. The lower form is
        either text (`spec["unshifted"]`, the number/punctuation keys) or a glyph
        (`spec["glyph"]`, the Move key's keyboard icon). At the 0/1 extremes this
        matches the old static layout. (Modifier dual keys — Paste/Copy, Move —
        rest in a slight tint of the modifier text color and brighten to it on
        Shift, like the number keys' "!" preview.)"""
        p = spec["progress"]
        size = spec.get("font_size") or self._FONT_SIZE_DUAL
        font_obj = self._get_sized_font(font, size)
        # The shifted (upper) form is a hint while unshifted — small and grey.
        # As Shift engages it GROWS up to the key's normal size and takes its
        # full color, so a held-shift label is exactly the same size as the
        # key's unshifted label. Uniform for every dual-state key (numbers,
        # punctuation, Paste/Copy, Move).
        sh_size = max(8, int(round(size * self._SHIFTED_RATIO)))
        edge_pad = 14

        def x_for(tw):
            if align == "left":
                x = key.x + edge_pad
            elif align == "right":
                x = key.x + key.w - tw - edge_pad
            else:
                x = key.x + (key.w - tw) // 2
            return x + text_dx

        # Upper (shifted) form: top → center, grey → full color. Dual keys fade
        # to the label color; the Move key fades to its meta/glyph color (white
        # when idle) so "Move" brightens grey→white as it centers.
        # `slide` (its downward travel in px) is reused below so the lower form
        # moves the same distance at the same speed.
        slide = 0.0
        # The upper form brightens from grey to its full color as it centers on
        # shift. Dual keys → label color; the Move key → its meta/glyph color —
        # the same active text color the rest of the keys take in shift state, in
        # BOTH opaque and transparent modes (so "Move" doesn't stay grey).
        top_target = glyph_color if spec.get("glyph") else label_color
        # MODIFIER dual keys (Paste/Copy, Move) rest in a semi-transparent tint
        # of the modifier text color — the same faded-preview treatment the
        # number keys give their shifted "!" — but tinted toward the background
        # only slightly so they stay legible on the modifier button (the
        # accent-derived shadow was too dark to read there). The slide animation
        # still runs, and holding Shift brightens them to the full color.
        rest_color = (
            self._lerp_color(self.modifier_text_color, self.bg_color, 0.3)
            if spec.get("modifier")
            else self.shadow_label_color
        )
        sh_color = self._lerp_color(rest_color, top_target, p)
        # Resolve the shifted font at its current interpolated size — re-rastered
        # per frame while the size animates from the small hint to the normal
        # label size.
        cur_sh_size = max(8, int(round(sh_size + (size - sh_size) * p)))
        sh_font_obj = self._get_sized_font(font, cur_sh_size)
        sh_tex, sh_tw, sh_th = self._make_text_texture(
            sh_font_obj, spec["shifted"], sh_color
        )
        if sh_tex:
            # Upper-label rest perch. The dialed-in per-key nudges (closer-
            # together pair, legacy positions, per-key top_dy) apply ONLY to the
            # transparent skins; opaque skins keep the original key.y+3 perch —
            # only the Shift animation itself carries over to opaque. center_y
            # (the p=1 target) is unchanged either way, so the endpoint matches.
            if not self._transparent or spec.get("glyph"):
                top_y = key.y + 3
            else:
                top_y = key.y + (4 if spec.get("legacy_pos") else 2)
            if self._transparent:
                top_y += spec.get("top_dy", 0)  # per-key fine offset (YAML)
            center_y = key.y + (key.h - sh_th) // 2
            slide = (center_y - top_y) * p
            ux, uy = x_for(sh_tw), top_y + slide
            # Outline on the upper form fades IN with the shift progress: the
            # unshifted preview stays outline-free and the centered/shifted char
            # picks up the outline like any primary label. The Move "Move" text
            # uses this same shifted-text formatting — so it has NO outline in its
            # unshifted rest state (per user pref), only as it centers on shift.
            out_a = int(255 * p)
            if out_a > 0:
                self._draw_text_outline(
                    sh_font_obj,
                    spec["shifted"],
                    sh_color,
                    ux,
                    uy,
                    out_a,
                    spec.get("outline_px"),
                    spec.get("outline_opacity"),
                )
            S.SDL_SetTextureAlphaMod(sh_tex, self._text_alpha)
            dst = S.SDL_FRect(ux, uy, sh_tw, sh_th)
            S.SDL_RenderTexture(self.renderer, sh_tex, None, ctypes.byref(dst))

        # Lower form: bottom -> slide down by the same `slide`, fading out.
        # Skipped once fully transparent (fully shifted).
        alpha = int(round(255 * (1.0 - p)))
        if alpha <= 0:
            return
        lower_glyph = spec.get("glyph")
        if lower_glyph:
            # Move key: the emoji smiley drops away (passing the label text so
            # _draw_glyph keeps the same bottom placement, not centerpiece).
            self._draw_glyph(
                key,
                lower_glyph,
                align,
                spec["shifted"],
                glyph_color,
                dy=slide,
                alpha=alpha,
            )
        else:
            un_tex, un_tw, un_th = self._make_text_texture(
                font_obj, spec["unshifted"], label_color
            )
            if un_tex:
                # Lower-label rest: the dialed-in nudges (8-px pad nudged / 5-px
                # legacy / per-key bottom_dy) apply ONLY to transparent skins;
                # opaque skins keep the original 4-px pad. p=1 is fully faded
                # either way, so the animation endpoint is unaffected.
                if self._transparent:
                    bottom_y = (
                        key.y
                        + key.h
                        - un_th
                        - (5 if spec.get("legacy_pos") else 8)
                        + spec.get("bottom_dy", 0)
                    )
                else:
                    bottom_y = key.y + key.h - un_th - 4
                lx, ly = x_for(un_tw), bottom_y + slide
                # Outline tracks the label's fade alpha so it dissolves together.
                self._draw_text_outline(
                    font_obj,
                    spec["unshifted"],
                    label_color,
                    lx,
                    ly,
                    alpha,
                    spec.get("outline_px"),
                    spec.get("outline_opacity"),
                )
                S.SDL_SetTextureAlphaMod(
                    un_tex, alpha
                )  # animation fade only; font opacity not scaled
                dst = S.SDL_FRect(lx, ly, un_tw, un_th)
                S.SDL_RenderTexture(
                    self.renderer, un_tex, None, ctypes.byref(dst)
                )

    def render_ptr(self, ptr):
        # User-supplied touchcircle.png drawn at the pointer position with
        # 50% opacity. Same artwork for both pads. Rendered as a two-tone
        # cursor (see below) so it's visible on every theme.
        ptr_x, ptr_y = ptr.coord_frac.to_absolute()
        tex, (tw, th) = self._get_glyph("touchcircle.png")
        if tex is None:
            return
        cx = utils.round_to_int(ptr_x)
        cy = utils.round_to_int(ptr_y)
        # Two-tone cursor (the Windows/macOS technique for guaranteed
        # visibility on ANY background): an accent-tinted core with a dark halo
        # ring. The dark ring reads on light fills, the accent glow/disc on
        # dark ones, so the cursor never vanishes — and it carries the theme's
        # accent hue. The glow texture is a pure radial gradient (fully
        # transparent corners), so every blit is a smooth circle.
        glow, (gw, gh) = self._get_glyph("touchglow.png")
        if glow is not None:
            # Dark outer ring: larger, in near-black, so it reads as a crisp
            # rim against light fills while staying subtle on dark ones.
            dark = Color(0x00, 0x00, 0x00)
            S.SDL_SetTextureColorMod(glow, dark.r, dark.g, dark.b)
            S.SDL_SetTextureAlphaMod(glow, 170)
            gs = max(tw, th)
            dst = S.SDL_FRect(
                cx - int(gs * 0.95),
                cy - int(gs * 0.95),
                int(gs * 1.9),
                int(gs * 1.9),
            )
            S.SDL_RenderTexture(self.renderer, glow, None, ctypes.byref(dst))
            # Accent glow: slightly smaller, accent tint LIGHTENED toward white
            # (so even a dark accent — NightShift's pink, Gruvbox's grey —
            # still reads on the dark key fills). The dark ring outlines it on
            # light fills.
            acc = self.accent_color
            glow_c = self._lerp_color(acc, Color(0xFF, 0xFF, 0xFF), 0.55)
            S.SDL_SetTextureColorMod(glow, glow_c.r, glow_c.g, glow_c.b)
            S.SDL_SetTextureAlphaMod(glow, 130)
            dst = S.SDL_FRect(
                cx - int(gs * 0.8),
                cy - int(gs * 0.8),
                int(gs * 1.6),
                int(gs * 1.6),
            )
            S.SDL_RenderTexture(self.renderer, glow, None, ctypes.byref(dst))
        # Disc core: accent-tinted, so the pointer center carries the theme's
        # accent color and stays legible on dark fills.
        acc = self.accent_color
        S.SDL_SetTextureColorMod(tex, acc.r, acc.g, acc.b)
        S.SDL_SetTextureAlphaMod(tex, 190)
        dst = S.SDL_FRect(cx - tw // 2, cy - th // 2, tw, th)
        S.SDL_RenderTexture(self.renderer, tex, None, ctypes.byref(dst))

    def _press_scale(self, key_id):
        """Current per-key press scale for `key_id` — a (row, col) tuple, or a
        KeyLayout with .row/.col (1.0 when idle). Advancing the press animation
        is done in render_vkb; this only reads the stored phase and computes
        the eased/spring scale from wall-clock time, so it stays cheap and pure
        for the dirty-gate signature."""
        if hasattr(key_id, "row"):
            key_id = (key_id.row, key_id.col)
        entry = self._press_anim.get(key_id)
        if entry is None:
            return 1.0
        phase, t0 = entry
        now = time.monotonic()
        if phase == 1:  # pressing down: quick scale-in
            t = min(1.0, (now - t0) / self._PRESS_IN_DUR)
            # fast ease-in (fast start, settles at the pressed scale)
            return 1.0 - (1.0 - self._PRESS_SCALE) * (t * t * t)
        # phase == 0: release — spring back to 1.0 from the pressed scale.
        p = utils.spring_p(now - t0, self._PRESS_ZETA, self._PRESS_OMEGA0)
        return self._PRESS_SCALE + (1.0 - self._PRESS_SCALE) * p

    def render_vkb(self, virtual_kb, pointers):
        shift_held = state.is_shift_held()
        # Reuse the per-frame caps read from content_changed when available
        # (the open/close-anim paths reach here without one, so fall back to a
        # fresh read then).
        caps_on = (
            self._caps_on if self._caps_on is not None else state.is_caps_on()
        )
        highlighted = state.get_highlighted()
        lpad_touched = state.is_lpad_touched()
        rpad_touched = state.is_rpad_touched()
        cursor_row, cursor_col = state.get_cursor()
        mouse_press_cell = state.get_mouse_press_cell()
        # Modifiers latched via the on-screen toggle — rendered as a stable
        # "held" highlight, never a press animation (see the press-anim block).
        latched_mod_keys = state.get_latched_modifier_keys()

        # Advance the Shift slide/fade animation toward the live shift state.
        # Dual-state keys (numbers/punctuation) use `shift_anim` (eased 0→1) to
        # slide their two labels and cross-fade instead of snapping. Wall-clock
        # driven so it's independent of the render frame rate.
        now_t = time.monotonic()
        target = 1.0 if shift_held else 0.0
        if self._shift_anim_t is None:
            self._shift_anim = target  # snap on the first frame after open
        elif self._SHIFT_ANIM_DUR > 0:
            # Clamp dt so a single slow frame (e.g. the first one after the
            # render loop ramps back up from its idle FPS) can't make the
            # animation jump — it just starts a touch slower instead.
            dt = min(now_t - self._shift_anim_t, 0.02)
            step = dt / self._SHIFT_ANIM_DUR
            if self._shift_anim < target:
                self._shift_anim = min(target, self._shift_anim + step)
            elif self._shift_anim > target:
                self._shift_anim = max(target, self._shift_anim - step)
        else:
            self._shift_anim = target
        self._shift_anim_t = now_t
        # Back-ease the linear progress: the shift label slides and
        # slightly overshoots then settles (iOS feel) instead of the
        # old smoothstep. Fixed-duration (0..1 input), subtle ~6%
        # overshoot (c1=1.3).
        _ap = self._shift_anim
        shift_anim = utils.ease_out_back(_ap, c1=1.3)

        # While the diacritic variant row is open, the finger must move up into
        # the strip to pick a candidate — which puts the pointer physically
        # over the REAL keys in the row above (e.g. w/e under a strip raised
        # from s). Those keys must NOT flash CLICK/HOVER just because the
        # pointer sits on top of them; only the strip's own candidates light
        # up. So when a key's rect intersects the strip, the pointers cannot
        # drive its state (button/cursor/mouse-press highlights still apply).
        strip_rect = state.get_diacritic_rect()

        def _key_under_strip(kx, ky, kw, kh):
            if strip_rect is None:
                return False
            rx, ry, rw, rh = strip_rect
            return not (
                kx + kw <= rx
                or rx + rw <= kx
                or ky + kh <= ry
                or ry + rh <= ky
            )

        for key in virtual_kb.gen_key_layouts():
            input_state = state.InputState.INACTIVE
            pointer_gated = _key_under_strip(key.x, key.y, key.w, key.h)
            if not pointer_gated and pointers[0].in_box(
                key.x, key.y, key.w, key.h
            ):
                input_state = max(pointers[0].state, input_state)
            if not pointer_gated and pointers[1].in_box(
                key.x, key.y, key.w, key.h
            ):
                input_state = max(pointers[1].state, input_state)
            kb_key = virtual_kb.keys[key.row][key.col]
            # Controller-button highlight: paint the on-screen key as if it
            # were being clicked while its bound button is physically held.
            if kb_key.keycode and kb_key.keycode in highlighted:
                input_state = max(state.InputState.CLICK, input_state)
            # DPAD/stick cursor: paint the selected key in HOVER so the user can
            # see where the A button will land. Only when keyboard-stick
            # navigation is enabled (the left stick is what moves this cursor)
            # AND neither touchpad is touched — the pointer is the focus then,
            # not the cursor. Without the stick-nav gate, a leftover default
            # cursor (e.g. row 2, col 5 = "G") would highlight a key for touch-
            # pad-only users the moment both fingers lift, which reads as a bug.
            if (
                state.is_sc_kbd_stick_nav_enabled()
                and state.is_cursor_used()
                and key.row == cursor_row
                and key.col == cursor_col
                and not lpad_touched
                and not rpad_touched
            ):
                input_state = max(state.InputState.HOVER, input_state)
            # Mouse press: paint the key held under the left button blue, so a
            # mouse click flashes the same CLICK highlight as a real press.
            if (
                mouse_press_cell is not None
                and key.row == mouse_press_cell[0]
                and key.col == mouse_press_cell[1]
            ):
                input_state = max(state.InputState.CLICK, input_state)

            # Per-key press animation: on the CLICK rising edge start the
            # quick scale-down; on the release (falling) edge spring back.
            # LATCHED modifiers (Shift/Ctrl/Alt toggled on-screen) are a held
            # state, not a click — they keep the CLICK "held" color but never
            # run the press-pop animation.
            pressed_now = input_state == state.InputState.CLICK
            key_id = (key.row, key.col)
            entry = self._press_anim.get(key_id)
            if kb_key.keycode in latched_mod_keys:
                self._press_anim.pop(key_id, None)
            elif pressed_now and entry is None:
                self._press_anim[key_id] = [
                    1,
                    time.monotonic(),
                ]  # phase=1: down
            elif not pressed_now and entry is not None:
                if entry[0] == 1:
                    # Release edge: seed the spring-back with the current scale
                    # so a mid-press release snaps back instead of jumping.
                    self._press_anim[key_id] = [0, time.monotonic()]
                else:
                    # Release spring done: drop the entry so idle keys do not
                    # gate the dirty check.
                    self._press_anim.pop(key_id, None)

            # Single-alpha letter keys just swap case on shift/caps — they
            # don't warrant a permanent "shadow" preview. Non-letter dual-state
            # keys (numbers, punctuation) show the shifted form as a small
            # grey shadow above the main label while shift is *not* held; with
            # shift held, only the shifted form is shown, vertically centered.
            is_letter = len(kb_key.str) == 1 and kb_key.str.isalpha()
            dual_eligible = (
                kb_key.shifted and not kb_key.swap_on_shift and not is_letter
            )
            # Move key: animates on shift like the Paste/Copy dual-state key -
            # its label slides top->center (grey->white) while the emoji smiley
            # glyph slides down and fades out. The is_move flag (behavior: move)
            # identifies it.
            is_move_key = kb_key.is_move
            shadow = None
            valign = kb_key.valign
            dual_anim = None
            if dual_eligible:
                # Slide/fade between the unshifted (bottom) and shifted (center)
                # forms as Shift engages; handled in render_key via
                # _render_dual_anim (replaces the static shadow + main label).
                label = ""
                dual_anim = {
                    "shifted": kb_key.shifted,
                    "unshifted": kb_key.str,
                    "progress": shift_anim,
                    "modifier": kb_key.modifier,
                    "font_size": kb_key.font_size or self._FONT_SIZE_DUAL,
                    "legacy_pos": kb_key.legacy_label_pos,
                    "top_dy": kb_key.dual_top_dy,
                    "bottom_dy": kb_key.dual_bottom_dy,
                    "outline_px": kb_key.outline_px,
                    "outline_opacity": kb_key.outline_opacity,
                }
            elif is_move_key:
                # "Move" text is the upper form (slides top->center on shift,
                # grey at both ends so only the slide shows); the smiley glyph
                # is the lower form (slides down + fades out). render_key draws
                # no static glyph (forced None below) - the animated path draws it.
                label = ""
                dual_anim = {
                    "shifted": kb_key.str,
                    "unshifted": None,
                    "glyph": kb_key.glyph,
                    "progress": shift_anim,
                    "modifier": kb_key.modifier,
                    "font_size": kb_key.font_size or self._FONT_SIZE_MOD,
                    "top_dy": kb_key.dual_top_dy,
                }

            else:
                label = kb_key.display_label(shift_held, caps_on)

            glyph = kb_key.glyph
            if shift_held:
                if kb_key.shift_glyph is not None:
                    glyph = kb_key.shift_glyph or None
                if kb_key.shift_valign is not None:
                    valign = kb_key.shift_valign
            # Hide the L2/R2 hint glyphs: the SC's triggers are disabled
            # (trackpad click is the insert, see controller.py), so the hint
            # would point at a button that does nothing.
            if glyph == "glyph_l2.png":
                glyph = None
            if glyph == "sc_r2_md.png":
                glyph = None

            # Dual-label keys size their main label to match the shadow
            # preview, unless the YAML explicitly overrides font_size.
            key_font_size = kb_key.font_size
            if dual_eligible and key_font_size is None:
                key_font_size = self._FONT_SIZE_DUAL

            # The Move key's smiley glyph is drawn (slid + faded) by the
            # animated path, so suppress render_key's static glyph draw.
            if is_move_key:
                glyph = None

                # Per-key press pop: scale the whole key (fill + label + glyph)
            # about its center. The clip rect below stays at the ORIGINAL
            # key bounds so a scaled-down key shows the desktop around it
            # (the shrink is visible), while a spring overshoot past 1.0
            # cannot bleed into neighbors.
            pscale = self._press_scale(key)
            rkey = key
            if pscale != 1.0:
                nw = max(1, int(round(key.w * pscale)))
                nh = max(1, int(round(key.h * pscale)))
                rkey = key._replace(
                    x=key.x + (key.w - nw) // 2,
                    y=key.y + (key.h - nh) // 2,
                    w=nw,
                    h=nh,
                )

            # Clip each key's content to its own rect so a label sliding past
            # the key edge during the Shift animation can't show outside the key
            # (now visible in transparent mode, which has no opaque background to
            # hide it). Expanded out by 1px so the fill edges aren't trimmed;
            # adjacent keys' clips overlap harmlessly at the shared boundary.
            cx, cy = int(key.x), int(key.y)
            clip = S.SDL_Rect(
                cx,
                cy,
                int(key.x + key.w) - cx + 1,
                int(key.y + key.h) - cy + 1,
            )
            S.SDL_SetRenderClipRect(self.renderer, ctypes.byref(clip))
            self.render_key(
                label,
                rkey,
                input_state,
                modifier=kb_key.modifier,
                align=kb_key.align,
                valign=valign,
                glyph=glyph,
                font=kb_key.font,
                text_color_override=kb_key.text_color,
                bg_color_override=kb_key.bg_color,
                shadow_label=shadow,
                font_size=key_font_size,
                text_dx=kb_key.text_dx,
                shadow_font_size=kb_key.shadow_font_size,
                dual_anim=dual_anim,
                outline_px=kb_key.outline_px,
                outline_opacity=kb_key.outline_opacity,
            )

        # Drop the per-key clip so the pointer circles (and the next frame's
        # clear) aren't restricted to the last key's rect.
        S.SDL_SetRenderClipRect(self.renderer, None)

    def _render_diacritic_row(self):
        """Overlay pass for the Feature-B variant row: a small strip of
        candidate keys drawn just above the held letter key. The selected
        candidate is painted with the pressed (CLICK) colors; a base
        selection (index -1) highlights none. Drawn after the keys so the
        strip covers whatever is behind it."""
        sess = state.get_diacritic()
        if sess is None:
            return
        _char, variants, index, rect, _source = sess
        if not variants or rect is None:
            return
        x, y, w, h = rect
        # The strip is drawn ABOVE the held key, which puts it ON TOP of the
        # keys in the row above (e.g. w/e sit under a strip raised from s).
        # It must read as a solid floating POPUP, not as those keys lighting
        # up: a fully opaque panel, a hard 1-px outline, and a small drop
        # shadow, so the underlying keys are cleanly hidden and the strip is
        # clearly a separate element. (Candidate fills below are also forced
        # opaque for the same reason.)
        panel = self.modifier_idle_color
        rim = self.shadow_label_color
        # Drop shadow first: a dark rect offset 2 px down+right, drawn before
        # the panel so the panel covers all but the offset sliver. Semi-
        # transparent so it reads as a shadow, not a second panel.
        sh = self.shadow_label_color
        S.SDL_SetRenderDrawColor(self.renderer, sh.r, sh.g, sh.b, 110)
        S.SDL_RenderFillRect(
            self.renderer, ctypes.byref(S.SDL_FRect(x + 2, y + 2, w, h))
        )
        # Opaque panel.
        S.SDL_SetRenderDrawColor(self.renderer, panel.r, panel.g, panel.b, 255)
        S.SDL_RenderFillRect(
            self.renderer, ctypes.byref(S.SDL_FRect(x, y, w, h))
        )
        # 1-px outline all the way around (not just a bottom rim), so the
        # popup has a hard edge against the keyboard.
        S.SDL_SetRenderDrawColor(self.renderer, rim.r, rim.g, rim.b, 255)
        S.SDL_RenderLine(self.renderer, x, y, x + w - 1, y)
        S.SDL_RenderLine(self.renderer, x, y + h - 1, x + w - 1, y + h - 1)
        S.SDL_RenderLine(self.renderer, x, y, x, y + h - 1)
        S.SDL_RenderLine(self.renderer, x + w - 1, y, x + w - 1, y + h - 1)
        # Candidate keys, sized to the shared diacritics geometry so the
        # input paths (which map pointer x -> candidate from the same
        # constants) and the renderer always agree on where each one sits.
        pad_y = 4
        cw = diacritics.CANDIDATE_W
        step = cw + diacritics.CANDIDATE_GAP
        cand_h = h - 2 * pad_y
        for i, ch in enumerate(variants):
            # Candidate i spans [x + i*step, x + i*step + cw] — the SAME x
            # layout variant_index_at_point assumes, so the rendered box and
            # the input highlight always agree (no horizontal inset).
            cx = x + i * step
            cy = y + pad_y
            if i == index:
                fill = self.key_color[state.InputState.CLICK]
                label_color = self.text_color[state.InputState.CLICK]
            else:
                fill = self.key_color[state.InputState.INACTIVE]
                label_color = self.text_color[state.InputState.INACTIVE]
            S.SDL_SetRenderDrawColor(
                self.renderer, fill.r, fill.g, fill.b, 255
            )
            S.SDL_RenderFillRect(
                self.renderer, ctypes.byref(S.SDL_FRect(cx, cy, cw, cand_h))
            )
            tex, tw, th = self._make_text_texture(
                self.font_manager, ch, label_color
            )
            if not tex:
                continue
            S.SDL_SetTextureAlphaMod(tex, self._text_alpha)
            dst = S.SDL_FRect(
                cx + (cw - tw) // 2, cy + (cand_h - th) // 2, tw, th
            )
            S.SDL_RenderTexture(self.renderer, tex, None, ctypes.byref(dst))

    def render(self, virtual_kb, pointers):
        self.clear()
        if not self._transparent:
            # Split layout: repaint only the two halves so the middle gap stays
            # transparent (opaque mode already filled the whole window).
            self._render_split_background(virtual_kb)
        self.render_vkb(virtual_kb, pointers)
        self._render_diacritic_row()
        # Only show the finger circles while the trackpad is actually being
        # touched; otherwise the screen would have two big idle pointers.
        if pointers[0].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[0])
        if pointers[1].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[1])
        S.SDL_RenderPresent(self.renderer)
        self._resize_dirty = False  # resize forced this frame; next can gate

    def _ensure_anim_target(self):
        """Lazily create (once) the offscreen render-target texture the open
        animation draws into. Returns it, or None if the renderer can't provide
        a target — in which case the caller simply skips the animation."""
        if self._anim_target is None:
            tex = S.SDL_CreateTexture(
                self.renderer,
                S.SDL_PIXELFORMAT_ABGR8888,
                S.SDL_TEXTUREACCESS_TARGET,
                width,
                height,
            )
            if tex:
                # Rendering onto this texture (cleared to (0,0,0,0)) with the
                # renderer's normal BLEND mode leaves it holding PREMULTIPLIED
                # RGB (SDL's render-to-transparent-texture quirk: a draw of
                # color C @ alpha A onto a zeroed target yields stored
                # (C*A/255, A)). Compositing it back with plain BLEND would
                # multiply that already-premultiplied RGB by alpha AGAIN,
                # darkening translucent fills (visible as a "pop" to the
                # correct color when the animation ends and the normal
                # single-multiply render takes over). PREMULTIPLIED composites
                # it correctly and keeps the fade a pure alpha ramp.
                S.SDL_SetTextureBlendMode(
                    tex, S.SDL_BLENDMODE_BLEND_PREMULTIPLIED
                )
            self._anim_target = tex
        return self._anim_target

    def render_open_anim(self, virtual_kb, pointers, fade, cut_px):
        """Render ONE frame of the OSK OPEN animation.

        `fade` (0..1) is the whole-keyboard opacity; `cut_px` pixels are hidden
        off the BOTTOM (the reveal shrinks this to 0). The keyboard is drawn into
        an offscreen texture at FULL opacity — so the font/text alpha is never
        scaled (the standing 100%-font rule holds) — then composited onto the
        window: cleared fully transparent, the keyboard blitted with a uniform
        alpha-mod (the fade) and clipped to the un-cut top region (the reveal).
        The cut strip and the fade's see-through both come from the window's
        per-pixel alpha (it is a TRANSPARENT/composited window). The downward
        settle is done by the CALLER repositioning the window. Returns False if
        no offscreen target is available (caller falls back to a normal render)."""
        tex = self._ensure_anim_target()
        if not tex:
            return False
        # 1) Keyboard -> offscreen texture, full opacity, normal appearance.
        S.SDL_SetRenderTarget(self.renderer, tex)
        self.clear()
        if not self._transparent:
            self._render_split_background(virtual_kb)
        # The animation runs without a content_changed this frame, so the caps
        # cache may be stale — force render_vkb to read it fresh.
        self._caps_on = None
        self.render_vkb(virtual_kb, pointers)
        self._render_diacritic_row()
        if pointers[0].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[0])
        if pointers[1].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[1])
        # 2) Composite onto the window: clear fully transparent, then blit the
        #    faded keyboard clipped to the still-revealed (un-cut) top region.
        S.SDL_SetRenderTarget(self.renderer, None)
        S.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 0)
        S.SDL_RenderClear(self.renderer)
        vis_h = height - max(0, int(round(cut_px)))
        if vis_h > 0:
            clip = S.SDL_Rect(0, 0, width, vis_h)
            S.SDL_SetRenderClipRect(self.renderer, ctypes.byref(clip))
            alpha = (
                0
                if fade <= 0.0
                else 255
                if fade >= 1.0
                else int(round(fade * 255))
            )
            S.SDL_SetTextureAlphaMod(tex, alpha)
            # Alpha-mod only scales the ALPHA channel; the texture RGB stays
            # full-brightness, so a premultiplied composite at low fade adds the
            # whole keyboard onto the desktop -> a white flash at the fade
            # boundaries (visible on dark themes). Scale RGB by the same factor
            # to restore the premultiplied invariant: a clean opacity fade.
            S.SDL_SetTextureColorMod(tex, alpha, alpha, alpha)
            dst = S.SDL_FRect(0.0, 0.0, float(width), float(height))
            S.SDL_RenderTexture(self.renderer, tex, None, ctypes.byref(dst))
            S.SDL_SetRenderClipRect(self.renderer, None)
        S.SDL_RenderPresent(self.renderer)
        return True

    def render_close_anim(self, virtual_kb, pointers, fade, scale=1.0):
        """Render ONE frame of the OSK CLOSE animation — the reverse of the
        open: the whole keyboard fades out (fade 1->0) while scaling down
        slightly about its center (a quick "recede into the plate" settle).
        Same offscreen-target composite as render_open_anim, so the font alpha
        stays full and only the uniform window alpha + scale change. Returns
        False if no offscreen target is available (caller falls back to an
        instant hide)."""
        tex = self._ensure_anim_target()
        if not tex:
            return False
        S.SDL_SetRenderTarget(self.renderer, tex)
        self.clear()
        if not self._transparent:
            self._render_split_background(virtual_kb)
        # Same as render_open_anim: no content_changed this frame, so force a
        # fresh caps read inside render_vkb.
        self._caps_on = None
        self.render_vkb(virtual_kb, pointers)
        self._render_diacritic_row()
        if pointers[0].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[0])
        if pointers[1].state != state.InputState.INACTIVE:
            self.render_ptr(pointers[1])
        S.SDL_SetRenderTarget(self.renderer, None)
        S.SDL_SetRenderDrawColor(self.renderer, 0, 0, 0, 0)
        S.SDL_RenderClear(self.renderer)
        if fade > 0.0:
            alpha = (
                0
                if fade <= 0.0
                else 255
                if fade >= 1.0
                else int(round(fade * 255))
            )
            S.SDL_SetTextureAlphaMod(tex, alpha)
            # Same premultiplied-RGB fix as render_open_anim: without scaling
            # RGB by fade, the closing keyboard would wash the desktop white.
            S.SDL_SetTextureColorMod(tex, alpha, alpha, alpha)
            sw = max(1, int(round(width * scale)))
            sh = max(1, int(round(height * scale)))
            dst = S.SDL_FRect(
                (width - sw) / 2.0, (height - sh) / 2.0, float(sw), float(sh)
            )
            S.SDL_RenderTexture(self.renderer, tex, None, ctypes.byref(dst))
        S.SDL_RenderPresent(self.renderer)
        return True
