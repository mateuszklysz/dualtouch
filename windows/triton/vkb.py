from collections import namedtuple

import steamcontroller.uinput as sui

from triton import config, diacritics, screen, state, utils
from triton.color import Color
from triton.size import _compute_size

kb = sui.Keyboard()

# True while dispatch_key is being called for an AUTO-REPEAT (held Backspace /
# arrow / L2-R2 rub), so the key-press sound doesn't machine-gun.
_dispatch_is_repeat = False
# True while dispatching a DEFERRED key's release (the base letter of a
# variant-capable key typed on release after the press edge already clicked):
# suppress the key-press sound so a quick tap of a variant key doesn't click
# twice (once at press, once at release).
_dispatch_silent = False

# Hold-to-repeat cadence shared by EVERY "press a key" input path (controller
# X button, A button, trackpad-click on the SC / ZL-ZR on Switch Pro, and
# mouse left-click): the key fires once, then after KEY_REPEAT_DELAY repeats
# every KEY_REPEAT_INTERVAL seconds. Single source of truth so all input modes
# rub out / arrow-step at one speed.
KEY_REPEAT_DELAY = 0.4
KEY_REPEAT_INTERVAL = 0.137
# Horizontal MOUSE travel (px) that fires one Shift+Left/Right arrow while the
# Select key is held (iOS hold-space text selection). A few px per character is
# precise; the mouse moves in screen px so the step is far smaller than the
# pad step (raw units).
SELECT_MOUSE_DRAG_STEP = 48
# Only these keycodes auto-repeat when held: Backspace (whose shifted form is
# Delete), the four arrow directions, and the Home/End nav keys on the 75%
# board. Every other key fires once.
REPEATABLE_KEYS = frozenset(
    {
        sui.Keys.KEY_BACKSPACE,
        sui.Keys.KEY_LEFT,
        sui.Keys.KEY_RIGHT,
        sui.Keys.KEY_UP,
        sui.Keys.KEY_DOWN,
        sui.Keys.KEY_HOME,
        sui.Keys.KEY_END,
    }
)


def is_repeatable(key):
    """True if holding this key should auto-repeat. Checks both the base and
    the shift keycode so the ◀▶ arrow keys repeat whether or not Shift is
    swapping them to ▲▼."""
    if key is None:
        return False
    return key.keycode in REPEATABLE_KEYS or (
        key.shift_keycode is not None and key.shift_keycode in REPEATABLE_KEYS
    )


def _parse_hex_color(s):
    s = s.lstrip("#")
    return Color(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _decode_key_color(s):
    """A per-key color override from the layout: a literal '#rrggbb', or a
    'skin:ROLE' marker (e.g. 'skin:key', 'skin:shadow') resolved against the
    active skin at render time so the key tracks skin changes, or None."""
    if not s:
        return None
    if isinstance(s, str) and s.startswith("skin:"):
        return s
    return _parse_hex_color(s)


class VirtualKeyboard:
    padding_inner = 3
    # Gap from the window edge to the outermost keys = padding_outer +
    # padding_inner, so 2 + 3 = 5 px of background border around the grid.
    padding_outer = 2
    key_width = []
    key_height = 0

    KeyLayout = namedtuple("KeyLayout", "x y w h row col")

    def __init__(self, keys):
        self.keys = keys
        self.key_rows = len(keys)
        # Cache of the flattened key-layout list (see gen_key_layouts): the
        # layout only depends on key_width/key_height/screen dims, all of which
        # change exclusively via update_dimensions, so it is invalidated there.
        self._layouts_cache = None
        self.update_dimensions()

    def _uniform_key_width(self, row):
        unpadded_width = (
            screen.width
            - self.padding_outer * 2
            - self.split_gap_px()
            - (len(self.keys[row]) * self.padding_inner * 2)
        )
        weights_total = 0
        for key in self.keys[row]:
            weights_total += key.width_weight

        return unpadded_width / weights_total

    def split_gap_px(self):
        """Middle-gap width (px) when split layout is on: a fraction of the
        window so "full" displays get a proportionally larger gap."""
        if not state.is_split_layout_enabled():
            return 0
        return round(screen.width * self.SPLIT_GAP_FRACTION)

    def _split_index(self, i_row):
        """Column where `i_row` splits into left/right halves in split layout:
        the column whose width_weight sum leaves the two halves most evenly
        balanced. The halves occupy equal-width fixed regions (see split_x), so
        balancing the key WEIGHTS gives the most natural split. At least one
        key lands on each side."""
        row = self.keys[i_row]
        n = len(row)
        if n < 2:
            return n
        total = sum(k.width_weight for k in row)
        best_i, best_diff = 1, None
        left = row[0].width_weight
        for i in range(1, n):
            diff = abs(left - (total - left))
            if best_diff is None or diff < best_diff:
                best_i, best_diff = i, diff
            left += row[i].width_weight
        return best_i

    def _split_ref_width(self):
        """Non-split keyboard width (px) for the CURRENT size submenu — the
        reference each split half is sized against. Sizing the halves from the
        plain-mode width (instead of from half the display) keeps split keys at
        the same size the user picked; the display's extra width becomes
        transparent middle gap."""
        return _compute_size(screen.get_osk_size())[0]

    def _split_region(self):
        """Usable width (px) of each split half in split layout, 0 when off.
        The halves are anchored to the window edges but sized from the
        non-split keyboard width (see _split_ref_width), so keys keep their
        normal size and the middle of a wide display stays transparent."""
        if not state.is_split_layout_enabled():
            return 0
        ref_w = self._split_ref_width()
        gap = round(ref_w * self.SPLIT_GAP_FRACTION)
        return (ref_w - gap) / 2 - self.padding_outer

    def split_gap_band(self):
        """(left, right) x-bounds of the transparent middle band in split
        layout, or None when off. Used by the renderer to clear the gap to
        alpha 0 (the desktop shows through, "no black in between") while the
        two halves stay opaque, and by the controller to map each pad onto its
        own half's key span."""
        if not state.is_split_layout_enabled():
            return None
        region = self._split_region()
        return (
            self.padding_outer + region,
            screen.width - self.padding_outer - region,
        )

    def _half_key_widths(self, i_row):
        """(left, right) per-key base widths (px per width_weight unit) for
        `i_row` in split layout: each half is sized independently against its
        own fixed-width region (either side of the gap band), so rows with
        different key counts still keep both halves symmetric around the ONE
        middle band. Plain layout returns the shared row width twice."""
        if not state.is_split_layout_enabled():
            kw = self.key_width[i_row]
            return kw, kw
        split_idx = self._split_index(i_row)
        row = self.keys[i_row]
        left = row[:split_idx]
        right = row[split_idx:]
        region = self._split_region()
        left_unpadded = region - len(left) * self.padding_inner * 2
        right_unpadded = region - len(right) * self.padding_inner * 2
        return (
            left_unpadded / sum(k.width_weight for k in left),
            right_unpadded / sum(k.width_weight for k in right),
        )

    def _key_unit_width(self, i_row, col):
        """Per-key base width (px per width_weight unit) at (i_row, col): the
        shared row width in plain layout, the key's own HALF's width in split
        layout — so find_key, find_key_rc and the render iteration all agree
        with _row_key_positions."""
        if not state.is_split_layout_enabled():
            return self.key_width[i_row]
        if col < self._split_index(i_row):
            return self._left_key_width[i_row]
        return self._right_key_width[i_row]

    def _row_key_positions(self, i_row):
        """Yield (col, x_start) for every key of `i_row` in visual left-to-right
        order. Split layout splits the row into two halves around the middle
        gap: the left half runs from the left edge to split_x(), the right
        half from split_x() + gap to the right edge (each half sizes its own
        keys, so all rows share ONE gap x-position — see split_gap_band); the
        plain layout is one contiguous run. Single source of truth for the
        render iteration, find_key, and find_key_rc, so the two halves stay in
        sync."""
        row = self.keys[i_row]
        if state.is_split_layout_enabled():
            split_idx = self._split_index(i_row)
            x = self.padding_outer
            for i, key in enumerate(row[:split_idx]):
                yield i, x
                x += (
                    key.width_weight * self._left_key_width[i_row]
                    + self.padding_inner * 2
                )
            band = self.split_gap_band()
            assert band is not None  # split mode is on (guard above)
            x = band[1]
            for i, key in enumerate(row[split_idx:], start=split_idx):
                yield i, x
                x += (
                    key.width_weight * self._right_key_width[i_row]
                    + self.padding_inner * 2
                )
        else:
            x = self.padding_outer
            for i, key in enumerate(row):
                yield i, x
                x += (
                    key.width_weight * self.key_width[i_row]
                    + self.padding_inner * 2
                )

    def _uniform_key_height(self):
        return (
            screen.height
            - self.padding_outer * 2
            - (self.key_rows * self.padding_inner * 2)
        ) / self.key_rows

    def update_dimensions(self):
        self.key_height = self._uniform_key_height()
        self.key_width = []
        self._left_key_width = []
        self._right_key_width = []
        for i in range(0, self.key_rows):
            self.key_width.append(self._uniform_key_width(i))
            lw, rw = self._half_key_widths(i)
            self._left_key_width.append(lw)
            self._right_key_width.append(rw)
        self._layouts_cache = None

    def find_key_row(self, y_coord):
        return int(
            (y_coord - self.padding_outer)
            / (self.key_height + self.padding_inner * 2)
        )

    def find_key(self, x_coord, y_coord):
        i_row = self.find_key_row(y_coord)
        i_row = utils.clamp(i_row, 0, self.key_rows - 1)
        row = self.keys[i_row]
        for i, x_start in self._row_key_positions(i_row):
            x_end = (
                x_start
                + row[i].width_weight * self._key_unit_width(i_row, i)
                + self.padding_inner * 2
            )
            if x_coord < x_end:
                return row[i]
        return None

    def find_key_rc(self, x_coord, y_coord):
        """Like find_key but returns the (row, col) grid index — used by the
        mouse handler to drive the same cursor/press path as the DPAD. Clamps
        to the nearest in-bounds cell so an edge click never misses."""
        i_row = utils.clamp(self.find_key_row(y_coord), 0, self.key_rows - 1)
        row = self.keys[i_row]
        for i, x_start in self._row_key_positions(i_row):
            x_end = (
                x_start
                + row[i].width_weight * self._key_unit_width(i_row, i)
                + self.padding_inner * 2
            )
            if x_coord < x_end:
                return (i_row, i)
        return (i_row, len(row) - 1)

    # Split layout: fraction of the window width left empty between the left
    # and right keyboard halves. Sized relative to the window so "full" gets a
    # proportionally larger gap.
    SPLIT_GAP_FRACTION = 0.06

    # Hit-target expansion (px) added around every key when resolving a click
    # or hover position (see find_key_expanded). Keys sit ~6 px apart (the
    # padding_inner gaps) with a ~5 px outer gutter, so a fast finger that
    # misses a key by a hair lands on the wrong neighbor or on nothing at all.
    # Expanding each rect by this margin and snapping to the key whose edge is
    # nearest gives every slightly-off click a small "grab radius" without
    # changing how a well-inside click resolves (a point inside a key has
    # distance 0, so it always stays on that key).
    HIT_TARGET_EXPAND = 10

    def find_key_expanded(self, x_coord, y_coord, expand=HIT_TARGET_EXPAND):
        """The key a click/hover at (x_coord, y_coord) should hit, with a small
        grab radius. Among the keys whose rect (grown by `expand` px on every
        side) contains the point, returns the one whose UNEXPANDED rect is
        nearest — distance 0 for a point inside a key (same result as
        find_key), otherwise the key whose edge is closest. So a click a few px
        over a boundary, in a row/column gap, or into the outer gutter still
        hits the intended key instead of its neighbor or nothing, but a click
        far from every key still returns None. Compared to find_key's x-range
        bucket + row floor-division, this only differs near boundaries (the
        real fix: wide/narrow key pairs split at the visual edge, not at the
        bucket cut)."""
        best = self._find_key_expanded(x_coord, y_coord, expand, want_rc=False)
        return self.keys[best[0]][best[1]] if best is not None else None

    def find_key_expanded_rc(self, x_coord, y_coord, expand=HIT_TARGET_EXPAND):
        """(row, col) grid index variant of find_key_expanded (None when the
        point is far from every key). Used by the pad press-lock so the locked
        key center matches what an unlocked click at the same spot would type —
        the two must never disagree at a boundary."""
        return self._find_key_expanded(x_coord, y_coord, expand, want_rc=True)

    def _find_key_expanded(self, x_coord, y_coord, expand, want_rc):
        """Shared nearest-key resolution for find_key_expanded / _rc. Returns
        the (row, col) of the best hit, or None when the point is far from
        every key. The public wrappers map that cell to the key object (False
        for `want_rc`) or pass the cell through (True) — the hit-test math is
        identical either way."""
        best = None
        best_d2 = None
        for layout in self.gen_key_layouts():
            left, t = layout.x, layout.y
            r, b = layout.x + layout.w, layout.y + layout.h
            if not (
                left - expand <= x_coord <= r + expand
                and t - expand <= y_coord <= b + expand
            ):
                continue
            dx = (
                0.0
                if left <= x_coord <= r
                else min(abs(x_coord - left), abs(x_coord - r))
            )
            dy = (
                0.0
                if t <= y_coord <= b
                else min(abs(y_coord - t), abs(y_coord - b))
            )
            d2 = dx * dx + dy * dy
            if best_d2 is None or d2 < best_d2:
                best = (layout.row, layout.col)
                best_d2 = d2
        return best

    def get_key_layout(self, target_row, target_col):
        for layout in self.gen_key_layouts():
            if layout.row == target_row and layout.col == target_col:
                return layout
        return None

    def find_col_at_x(self, target_row, x):
        """Pick the column in `target_row` whose pixel range covers `x`,
        falling back to the closest by center if `x` lands in a gap."""
        if not (0 <= target_row < self.key_rows):
            return None
        layouts = [k for k in self.gen_key_layouts() if k.row == target_row]
        if not layouts:
            return None
        for k in layouts:
            if k.x <= x < k.x + k.w:
                return k.col
        best = layouts[0].col
        best_dist = abs((layouts[0].x + layouts[0].w / 2) - x)
        for k in layouts[1:]:
            d = abs((k.x + k.w / 2) - x)
            if d < best_dist:
                best = k.col
                best_dist = d
        return best

    def gen_key_layouts(self):
        """All key rects as KeyLayout namedtuples, flattened in render order.
        Cached until update_dimensions (the layout depends only on the current
        key_width/key_height, which change solely there) — callers (hover hit-
        tests, the render loop's per-key pass) only iterate, never mutate, so a
        shared tuple is safe and skips rebuilding ~90 namedtuples per call."""
        if self._layouts_cache is None:
            self._layouts_cache = tuple(self._iter_key_layouts())
        return self._layouts_cache

    def _iter_key_layouts(self):
        iterated_y = self.padding_outer

        for i_row, row in enumerate(self.keys):
            for i_key, x_start in self._row_key_positions(i_row):
                adj_x = x_start + self.padding_inner
                adj_y = iterated_y + self.padding_inner
                adj_w = row[i_key].width_weight * self._key_unit_width(
                    i_row, i_key
                )
                adj_h = self.key_height

                yield self.KeyLayout(
                    utils.round_to_int(adj_x),
                    utils.round_to_int(adj_y),
                    utils.round_to_int(adj_w),
                    utils.round_to_int(adj_h),
                    i_row,
                    i_key,
                )
            iterated_y += self.key_height + self.padding_inner * 2


class KeyButton:
    def __init__(
        self,
        str,
        keycode,
        callback,
        width_weight=1.0,
        shifted=None,
        modifier=False,
        align="center",
        valign="center",
        glyph=None,
        font="default",
        text_color=None,
        bg_color=None,
        swap_on_shift=False,
        shift_glyph=None,
        shift_valign=None,
        font_size=None,
        shift_keycode=None,
    ):
        self.str = str
        self.shifted = (
            shifted  # Label shown when shift is held; None means show `str`.
        )
        self.keycode = keycode
        self.callback = callback
        self.width_weight = width_weight
        self.modifier = modifier  # Renderer paints modifier keys with a pure-black background.
        self.align = align  # "left" | "center" | "right" — label alignment inside the key.
        self.valign = (
            valign  # "top" | "center" | "bottom" — vertical label alignment.
        )
        self.glyph = glyph  # Glyph name resolved from the Steam install via
        # steam_assets (e.g. "glyph_l2.png"); names with no Steam file (the
        # smiley, touch-cursor visuals) load from the bundled copy.
        self.font = font  # "default" | "symbol" — picks the symbol font for glyphs Segoe lacks.
        self.text_color = (
            text_color  # Optional Color overriding INACTIVE text color.
        )
        self.bg_color = (
            bg_color  # Optional Color overriding INACTIVE key background.
        )
        # When True, the shifted variant fully replaces the main label on shift
        # (e.g. arrow keys ◀↔▲); when False the shifted variant renders as a
        # small gray "shadow" label above the main one (typewriter keys).
        self.swap_on_shift = swap_on_shift
        self.shift_glyph = (
            shift_glyph  # Overrides glyph while shift held ("" = no glyph).
        )
        self.shift_valign = shift_valign  # Overrides valign while shift held.
        self.font_size = (
            font_size  # Optional explicit pixel size for the main label.
        )
        # Optional alternate keycode used when Shift is held at dispatch time
        # (e.g. ◀ sends KEY_LEFT unshifted, KEY_UP while Shift is held).
        self.shift_keycode = shift_keycode
        # True for the on-screen "Select" key (behavior: select) — the hold-
        # and-drag text-selection key that replaced the right Shift.
        self.is_select = False
        # True for the on-screen "Move" key (behavior: move) - the renderer
        # animates its shift slide (the label slides top->center on shift, like
        # the Paste/Copy dual-state key).
        self.is_move = False
        # Per-key DPAD navigation overrides (target column in the adjacent
        # row); when None, the main loop falls back to pixel-x mapping.
        self.dpad_up = None
        self.dpad_down = None
        self.dpad_left = None
        self.dpad_right = None
        # Per-key horizontal label nudge in px (e.g. arrows shifted outward).
        self.text_dx = 0
        # Optional smaller font for the shadow preview only (e.g. arrow ▲▼).
        self.shadow_font_size = None
        # Keep this dual-state key's labels at their pre-2026-06-09 rest
        # positions (upper at key.y+3, lower at 4px bottom pad) instead of the
        # nudged-up defaults — set per key in the layout YAML. Animation target
        # (centered) is unchanged either way.
        self.legacy_label_pos = False
        # Per-key fine offsets (px, +down) added to the dual-label REST
        # positions only (the Shift-centered target is unchanged): top_dy nudges
        # the upper label, bottom_dy the lower label. Opposite signs pull the
        # pair together / apart; equal signs shift it. Set per key in the YAML.
        self.dual_top_dy = 0
        self.dual_bottom_dy = 0
        # Per-key transparent-mode text-outline overrides (None = use the
        # Screen defaults): outline_px = sub-pixel ring offset (thicker outline),
        # outline_opacity = 0..1 outline alpha factor. Set per key in the YAML.
        self.outline_px = None
        self.outline_opacity = None

    def display_label(self, shift_held, caps_on=False):
        if self.shifted is None:
            return self.str
        # Single-letter alpha keys honor BOTH shift and caps lock.
        if len(self.str) == 1 and self.str.isalpha():
            return self.shifted if (shift_held ^ caps_on) else self.str
        # Number / symbol keys only honor shift (caps lock has no effect).
        return self.shifted if shift_held else self.str


def on_key_generic(virtual_kb, keycode):
    # Clear pynput's INTERNAL shift/caps state first: letters are sent as
    # virtual keys whose case must follow the REAL OS shift only. If this
    # instance's internal shift_pressed is stale (a Shift pressed here and
    # released on the controller thread's separate instance, or a single
    # tap of the Caps key whose OS toggle PowerToys remapped away), pynput
    # uppercases every letter regardless of what the user does.
    kb.reset_shift_state()
    kb.pressEvent([keycode])
    kb.releaseEvent([keycode])


def on_key_backspace(virtual_kb, keycode):
    # Backspace — always. The old Shift+Backspace = Delete shortcut is
    # removed, so no shift gymnastics are needed here; just clear pynput's
    # stale internal shift/caps bookkeeping and tap (same as on_key_generic).
    kb.reset_shift_state()
    kb.pressEvent([keycode])
    kb.releaseEvent([keycode])


def tap_keycode(keycode):
    """Press + release a single keycode (used by the mouse side buttons)."""
    kb.reset_shift_state()
    kb.pressEvent([keycode])
    kb.releaseEvent([keycode])


def _modifier_highlights():
    """Keycode set of every LATELY-latched modifier, so toggling one on or off
    keeps the others' on-screen highlight. A toggle must never clear the whole
    set (unlatching Shift must not un-highlight a latched Ctrl/Alt)."""
    keys = set()
    if state.is_shift_latched():
        keys.update((sui.Keys.KEY_LEFTSHIFT, sui.Keys.KEY_RIGHTSHIFT))
    if state.is_ctrl_latched():
        keys.update((sui.Keys.KEY_LEFTCTRL, sui.Keys.KEY_RIGHTCTRL))
    if state.is_alt_latched():
        keys.update((sui.Keys.KEY_LEFTALT, sui.Keys.KEY_RIGHTALT))
    return keys


def toggle_shift():
    """Flip the latched-Shift state. Unlike the controller's L2 (held only while
    the trigger is down), the mouse/keyboard path latches Shift so it stays on
    until clicked again — the only sane model when there's no button to hold.
    Holds real KEY_LEFTSHIFT on the OS so the next key produces its shifted
    form, and paints the on-screen Shift keys blue while engaged.

    Decides on/off from our OWN latch flag, not state.is_shift_held(): a
    connected controller rewrites is_shift_held() every input frame, so reading
    it here would see False on every click and re-press forever (never toggle
    off). The controller ORs the latch into the display state, so they cooperate."""
    new = not state.is_shift_latched()
    state.set_shift_latched(new)
    if new:
        kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
    else:
        kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
    state.set_highlighted(_modifier_highlights())
    state.set_shift_held(new)


def release_shift():
    """Force-release the OS Shift key so hiding/closing the keyboard never
    leaves Shift stuck down. Unconditional on purpose: the latched-state flag
    can be out of sync with the real OS key (a controller input frame
    overwrites state.is_shift_held() every tick, and either the mouse toggle or
    a held L2 may own the OS key), and a pynput Shift key-up is idempotent —
    harmless if nothing was held — so we always send it."""
    kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
    kb.releaseEvent([sui.Keys.KEY_RIGHTSHIFT])
    state.set_shift_latched(False)
    state.set_shift_held(False)
    state.set_highlighted(set())


def clear_shift_latch(release_os=True):
    """Drop a mouse-latched Shift when another input source takes over (the
    Steam Controller's A button or the DPAD), reverting the sticky mouse toggle
    to the hold model those controls use. Releases the OS Shift the latch was
    holding unless release_os is False (e.g. L2 is currently holding Shift, so
    the OS key must stay down). Display state/highlight are recomputed by the
    controller's next input frame."""
    if not state.is_shift_latched():
        return
    state.set_shift_latched(False)
    if release_os:
        kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
        state.set_shift_held(False)


def toggle_ctrl():
    """Flip the latched-Ctrl state (mirror of toggle_shift). Holds real
    KEY_LEFTCTRL on the OS while engaged so the next key press produces its
    Ctrl+ combination (Ctrl+C to copy, Ctrl+A select-all, ...), and paints the
    on-screen Ctrl key blue while engaged. Decides on/off from our OWN latch
    flag, exactly like toggle_shift."""
    new = not state.is_ctrl_latched()
    state.set_ctrl_latched(new)
    if new:
        kb.pressEvent([sui.Keys.KEY_LEFTCTRL])
    else:
        kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
    state.set_highlighted(_modifier_highlights())


def on_key_ctrl(virtual_kb, keycode):
    # Clicking the on-screen Ctrl key (mouse left-click or the A button)
    # toggles the latched Ctrl state.
    toggle_ctrl()


def toggle_alt():
    """Flip the latched-Alt state (mirror of toggle_ctrl). Holds real
    KEY_LEFTALT on the OS while engaged so the next key press produces its
    Alt+ combination (Alt+Tab, Alt+F4, ...), and paints the on-screen Alt
    key blue while engaged. Decides on/off from our OWN latch flag."""
    new = not state.is_alt_latched()
    state.set_alt_latched(new)
    if new:
        kb.pressEvent([sui.Keys.KEY_LEFTALT])
    else:
        kb.releaseEvent([sui.Keys.KEY_LEFTALT])
    state.set_highlighted(_modifier_highlights())


def on_key_alt(virtual_kb, keycode):
    # Clicking the on-screen Alt key toggles the latched Alt state.
    toggle_alt()


def release_ctrl():
    """Force-release the OS Ctrl key so hiding/closing the keyboard never
    leaves Ctrl stuck down (mirror of release_shift)."""
    kb.releaseEvent([sui.Keys.KEY_LEFTCTRL])
    kb.releaseEvent([sui.Keys.KEY_RIGHTCTRL])
    state.set_ctrl_latched(False)


def release_alt():
    """Force-release the OS Alt key so hiding/closing the keyboard never
    leaves Alt stuck down (mirror of release_ctrl)."""
    kb.releaseEvent([sui.Keys.KEY_LEFTALT])
    kb.releaseEvent([sui.Keys.KEY_RIGHTALT])
    state.set_alt_latched(False)


def on_key_shift(virtual_kb, keycode):
    # Clicking the on-screen Shift key (mouse left-click or the A button)
    # toggles the latched Shift state.
    toggle_shift()


def on_key_paste(virtual_kb, keycode):
    # Paste, or Copy when Shift is held: Shift → Ctrl+C, otherwise Ctrl+V.
    # ALWAYS release both shifts first — not only when our logical state says
    # Shift is held. A shift-mode press re-presses Shift on THIS keyboard
    # instance (vkb.kb) to restore an L2-held Shift, but L2's release lands on
    # the controller thread's SEPARATE instance, so vkb.kb is left believing
    # Shift is still down. pynput then re-asserts that Shift around the next key,
    # breaking the chord (e.g. Ctrl+V arrives as Shift+V → "V"). Releasing here
    # resets vkb.kb's modifier state; then the chord via tap_with_modifier (raw
    # VK so it combines with Ctrl), then restore Shift if it's logically held.
    shift_held = state.is_shift_held()
    kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT, sui.Keys.KEY_RIGHTSHIFT])
    kb.tap_with_modifier(
        sui.Keys.KEY_LEFTCTRL, sui.Keys.KEY_C if shift_held else sui.Keys.KEY_V
    )
    if shift_held:
        kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])


def on_key_emoji(virtual_kb, keycode):
    # Toggle the OS emoji picker: open it with Win+. (Windows) / Meta+. (Linux),
    # or — if our last emoji press opened it — close it with Escape, so pressing
    # the on-screen emoji key again dismisses the picker. ALWAYS release both
    # shifts first so the OS sees Meta+. (not Meta+Shift+., a different shortcut)
    # AND to clear any Shift stranded in vkb.kb's modifier state by a prior L2
    # shift paste (see on_key_paste), then restore Shift only if logically held.
    shift_held = state.is_shift_held()
    kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT, sui.Keys.KEY_RIGHTSHIFT])
    if state.is_emoji_open():
        # Already open → Escape closes the focused picker.
        kb.pressEvent([sui.Keys.KEY_ESC])
        kb.releaseEvent([sui.Keys.KEY_ESC])
        state.set_emoji_open(False)
    else:
        kb.tap_with_modifier(sui.Keys.KEY_LEFTMETA, sui.Keys.KEY_DOT)
        state.set_emoji_open(True)
    if shift_held:
        kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])


def on_key_move(virtual_kb, keycode):
    # A plain press of this key opens the OS emoji picker (the smiley icon is
    # the default face of the key). With Shift held it instead advances the
    # window through its 6-position rotation (handled in the main loop) - the
    # "Move" action is the shifted form. It does NOT close the keyboard - the
    # old unshifted-close (which never worked anyway) is gone; close stays on
    # B / LGRIP / Steam+X.
    if state.is_shift_held():
        state.request_position_cycle()
    else:
        on_key_emoji(virtual_kb, keycode)


# Sentinel keycode for the on-screen "Select" key (replaces the right Shift).
# It is NOT in the OS keymap, so a stray dispatch of it can never send a real
# keystroke — the select mode it triggers is driven entirely by the hold/drag
# logic on the pad/mouse threads (see ControllerManager and the mouse handler).
SELECT_KEYCODE = sui.Keys.KEY_SELECT


def on_key_select(virtual_kb, keycode):
    # The Select key (hold + drag left/right to select text, like iOS hold-
    # space) is handled as a hold-state on the input threads: pressing it
    # enters select mode, horizontal travel sends Shift+Left/Right, releasing
    # exits. A plain tap here (A-button / mouse click without a drag) has
    # nothing to select, so it is a deliberate no-op.
    pass


class VirtualKeyboardConfig(config.ObjectConfig):
    @staticmethod
    def decode_keycode(str):
        try:
            return sui.Keys[str]
        except KeyError:
            raise AssertionError(f"Invalid keycode `{str}`") from None

    @staticmethod
    def decode_callback(str):
        if str == "generic":
            pass
        elif str == "shift":
            return on_key_shift
        elif str == "ctrl":
            return on_key_ctrl
        elif str == "alt":
            return on_key_alt
        elif str == "paste":
            return on_key_paste
        elif str == "emoji":
            return on_key_emoji
        elif str == "move":
            return on_key_move
        elif str == "backspace":
            return on_key_backspace
        elif str == "select":
            return on_key_select
        else:
            raise AssertionError(f"Invalid behavior `{str}`")
        return on_key_generic

    def construct(self):
        keys = []

        yaml_rows = self.objects["keys"]

        for yaml_row in yaml_rows:
            row = []
            for yaml_key in yaml_row:
                label = yaml_key.get("label", "")
                shifted = yaml_key.get("shifted")
                modifier = bool(yaml_key.get("modifier", False))
                align = yaml_key.get("align", "center")
                valign = yaml_key.get("valign", "center")
                glyph = yaml_key.get("glyph")
                font = yaml_key.get("font", "default")
                text_color = _decode_key_color(yaml_key.get("text_color"))
                bg_color = _decode_key_color(yaml_key.get("bg_color"))
                swap_on_shift = bool(yaml_key.get("swap_on_shift", False))
                shift_glyph = yaml_key.get("shift_glyph")
                shift_valign = yaml_key.get("shift_valign")
                font_size = yaml_key.get("font_size")
                keycode = (
                    0
                    if "keycode" not in yaml_key
                    else self.decode_keycode(yaml_key["keycode"])
                )
                shift_keycode_str = yaml_key.get("shift_keycode")
                shift_keycode = (
                    self.decode_keycode(shift_keycode_str)
                    if shift_keycode_str
                    else None
                )
                behavior = yaml_key.get("behavior", "generic")
                width_weight = yaml_key.get("width_weight", 1.0)

                callback = self.decode_callback(behavior)
                kb_btn = KeyButton(
                    label,
                    keycode,
                    callback,
                    width_weight,
                    shifted=shifted,
                    modifier=modifier,
                    align=align,
                    valign=valign,
                    glyph=glyph,
                    font=font,
                    text_color=text_color,
                    bg_color=bg_color,
                    swap_on_shift=swap_on_shift,
                    shift_glyph=shift_glyph,
                    shift_valign=shift_valign,
                    font_size=font_size,
                    shift_keycode=shift_keycode,
                )
                if behavior == "select":
                    kb_btn.is_select = True
                if behavior == "move":
                    kb_btn.is_move = True
                kb_btn.dpad_up = yaml_key.get("dpad_up")
                kb_btn.dpad_down = yaml_key.get("dpad_down")
                kb_btn.dpad_left = yaml_key.get("dpad_left")
                kb_btn.dpad_right = yaml_key.get("dpad_right")
                kb_btn.text_dx = yaml_key.get("text_dx", 0)
                kb_btn.shadow_font_size = yaml_key.get("shadow_font_size")
                kb_btn.legacy_label_pos = yaml_key.get(
                    "legacy_label_pos", False
                )
                kb_btn.dual_top_dy = yaml_key.get("dual_top_dy", 0)
                kb_btn.dual_bottom_dy = yaml_key.get("dual_bottom_dy", 0)
                kb_btn.outline_px = yaml_key.get("outline_px")
                kb_btn.outline_opacity = yaml_key.get("outline_opacity")
                row.append(kb_btn)

            keys.append(row)
        return VirtualKeyboard(keys)


def step_cursor(virtual_kb, direction, haptic=False):
    start = state.get_cursor()
    row, col = start
    rows = len(virtual_kb.keys)
    if not (0 <= row < rows):
        row = max(0, min(row, rows - 1))
        col = 0
    cur_btn = (
        virtual_kb.keys[row][col]
        if 0 <= col < len(virtual_kb.keys[row])
        else None
    )

    if direction == "LEFT":
        override = cur_btn.dpad_left if cur_btn else None
        if override is not None:
            col = override
        elif col > 0:
            col -= 1
        else:
            # Wrap horizontally back to the last key in the row.
            col = len(virtual_kb.keys[row]) - 1
    elif direction == "RIGHT":
        override = cur_btn.dpad_right if cur_btn else None
        if override is not None:
            col = override
        elif col < len(virtual_kb.keys[row]) - 1:
            col += 1
        else:
            # Wrap horizontally back to the first key in the row.
            col = 0
    elif direction in ("UP", "DOWN"):
        new_row = row - 1 if direction == "UP" else row + 1
        if 0 <= new_row < rows:
            override = (
                (cur_btn.dpad_up if direction == "UP" else cur_btn.dpad_down)
                if cur_btn
                else None
            )
            if override is not None:
                col = override
            else:
                cur_layout = virtual_kb.get_key_layout(row, col)
                if cur_layout is not None:
                    x_center = cur_layout.x + cur_layout.w // 2
                    new_col = virtual_kb.find_col_at_x(new_row, x_center)
                    if new_col is not None:
                        col = new_col
            row = new_row

    col = max(0, min(col, len(virtual_kb.keys[row]) - 1))
    state.set_cursor(row, col)
    # This is stick/DPAD navigation — the ONLY path that should arm the
    # persistent DPAD cursor highlight (touchpad/mouse pointing uses the
    # pointer instead and must not arm it).
    state.mark_cursor_used()
    # Tick the haptics when the selected key changes AND the move was driven by
    # the left stick (haptic=True). DPAD navigation passes haptic=False so only
    # the stick buzzes. Gated internally by the global haptics switch.
    if haptic and (row, col) != start:
        state.haptic_tick()


def dispatch_key(virtual_kb, key):
    # Steam-keyboard click on each physical key press (not auto-repeats).
    # Single choke point: every input path (mouse click, controller A-button,
    # pad click) flows through here.
    global _dispatch_is_repeat, _dispatch_silent
    if not _dispatch_is_repeat and not _dispatch_silent:
        state.key_sound_tick()
    # Keys with a `shift_keycode` (e.g. ◀▶ → ▲▼) want to send the alternate
    # keycode WITHOUT the OS seeing a Shift modifier; otherwise Shift+Arrow
    # selects text instead of just moving the caret. Briefly drop and
    # re-raise Shift around the dispatch so the OS sees only the arrow.
    if key.shift_keycode and state.is_shift_held():
        kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
        key.callback(virtual_kb, key.shift_keycode)
        if state.is_shift_held():
            kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
    else:
        key.callback(virtual_kb, key.keycode)


# --- Diacritic variants (Feature B: hold a letter to pick accented variants) --


def diacritic_variants_for_key(key):
    """The accented-variant list for a key, or None if it has none: not a
    single ASCII letter key, the feature is disabled, or the active locale's
    map has no entry for that letter. The map is keyed lowercase, so the
    label is folded (the OSK types lowercase by default; case is still a
    single tap of the base key)."""
    if not state.is_diacritics_enabled():
        return None
    label = key.str
    if (
        not label
        or len(label) != 1
        or not label.isascii()
        or not label.isalpha()
    ):
        return None
    return diacritics.lookup_variants(
        state.get_diacritic_variants(),
        state.get_active_locale(),
        label.lower(),
    )


def open_diacritic_rc(virtual_kb, row, col, source):
    """Open the variant row for the letter key at (row, col) — the key that
    was held past the hold threshold. Source identifies the input that opened
    it ("pad" / "mouse" / "a") so that input drives the highlight + commit.
    Returns True if a row opened (the hold is now a variant pick); False if
    the key has no variants, the feature is off, or a row is already open."""
    if not state.is_diacritics_enabled() or state.is_diacritic_open():
        return False
    key = virtual_kb.keys[row][col]
    variants = diacritic_variants_for_key(key)
    if not variants:
        return False
    layout = virtual_kb.get_key_layout(row, col)
    if layout is None:
        return False
    rect = diacritics.variant_row_rect(layout, len(variants), screen.width)
    if rect is None:
        # A candidate strip wider than the window can't be clamped into it
        # (pathological user map, or a very narrow window) — refuse to open
        # rather than crash the session on a None rect.
        return False
    # The base letter fired at the press edge follows the OS shift state: a
    # shift-held hold types an uppercase base, so the variant row must carry
    # that case too — otherwise the commit's Backspace removes the uppercase
    # base and injects a lowercase variant (case loss, no path to "Á").
    if state.is_shift_held() or state.is_caps_on():
        variants = [v.upper() for v in variants]
    state.open_diacritic(key.str.lower(), variants, rect, source)
    return True


def open_diacritic_at(virtual_kb, x, y, source):
    """open_diacritic_rc for a pointer position — the pad path, resolved on
    the controller thread with the expanded hit-target so a slightly-off
    hold still opens the right key's row."""
    rc = virtual_kb.find_key_expanded_rc(x, y)
    if rc is None:
        return False
    return open_diacritic_rc(virtual_kb, rc[0], rc[1], source)


def commit_diacritic(char=None):
    """Type the chosen accented variant. Defer model (Feature B): the base
    letter was NOT typed at the press edge — a hold shows the variant row with
    the base still untyped, so a release over a variant just types that
    variant via uinput.tap_char. There is no Backspace to undo: the base never
    stood. `char` is read from the open row session when not given. Closes
    the row FIRST and in a finally. The click sound fired at the PRESS edge
    (see pad/controller defer handling), so a held variant pick doesn't sound
    laggy on release. A base selection (index -1) is a no-op: the caller types
    the base letter on release instead."""
    if char is None:
        char = state.get_diacritic_selected_char()
    ok = None
    try:
        if not char:
            return
        ok = kb.tap_char(char)
    finally:
        # ALWAYS close the row: if the injection throws, an open row would
        # make every later hold queue a ("repeat", coord) and silently break
        # typing. The row must never survive a commit attempt.
        state.close_diacritic()
    if ok is False:
        # Mirror injection failures into dualtouch.log (gated by the tray
        # logging toggle, via the applog write path) — a windowed exe has no
        # stdout, so the uinput print()s are invisible. This is the only way
        # to see WHICH path failed (VK vs paste vs UNICODE) on the user's
        # machine.
        try:
            from applog import log_line

            log_line(
                "triton",
                f"diacritic commit {char!r} FAILED (all injection paths)",
            )
        except Exception:
            pass


def process_click_queue(virtual_kb, queue):
    while len(queue) > 0:
        item = queue.popleft()
        try:
            _process_click_item(virtual_kb, item)
        except Exception as e:
            # One bad item must never kill the main loop — a throwing commit
            # (e.g. a clipboard/paste failure escaping tap_char) would freeze
            # ALL typing for the rest of the session. Drop it and move on.
            try:
                from applog import log_line

                log_line("triton", f"click item {item!r} failed: {e!r}")
            except Exception:
                pass


def _process_click_item(virtual_kb, item):
    # A ("variant", char) item is a diacritic commit queued by an input
    # thread that detected its press release (pad / A-button).
    if isinstance(item, tuple) and item and item[0] == "variant":
        commit_diacritic(item[1])
        return
    # A ("deferred", coord) item is a deferred variant-capable key's base
    # letter typed on release — the press edge already clicked, so dispatch it
    # WITHOUT a second click sound.
    if isinstance(item, tuple) and item and item[0] == "deferred":
        global _dispatch_silent
        _dispatch_silent = True
        try:
            _process_click_item(virtual_kb, item[1])
        finally:
            _dispatch_silent = False
        return
    # A bare coord is a normal first hit (any key). A ("repeat", coord)
    # tuple is an auto-repeat from holding L2/R2/pad-click — honoured only
    # over Backspace, so holding rubs out text but won't machine-gun
    # ordinary keys (matches the X-button delete repeat).
    is_repeat = isinstance(item, tuple) and item and item[0] == "repeat"
    coord = item[1] if is_repeat else item
    if isinstance(coord, tuple):
        return  # unexpected tagged item — drop defensively
    x, y = coord.to_absolute()
    # Expanded hit-target (small grab radius): a pad click a few px over a
    # key boundary / in a gap still lands on the intended key, so fast
    # two-finger typing doesn't mistype on near-misses.
    key = virtual_kb.find_key_expanded(x, y)
    if key is None:
        return
    if is_repeat and not is_repeatable(key):
        # Hold-to-extend (Feature B): a held letter fires its repeat —
        # letters aren't repeatable, so the repeat is dropped, but the
        # FIRST hold past the delay opens the key's variant row instead.
        # The base already fired on the press edge, so a quick tap still
        # types with zero added latency (option B). Safety net for any
        # pad-style source; the controller thread also opens the row at
        # repeat-arming (see pad._PadMixin._try_open_diacritic) so it can
        # watch the same press for the release commit.
        if open_diacritic_at(virtual_kb, x, y, "pad"):
            return
        return
    global _dispatch_is_repeat
    _dispatch_is_repeat = is_repeat
    try:
        dispatch_key(virtual_kb, key)
    finally:
        _dispatch_is_repeat = False
    # Note: the click haptic is fired earlier, on the controller thread at
    # click-detection (see ControllerManager.handle_pad_input), for lowest
    # latency — so it is intentionally NOT fired here.
