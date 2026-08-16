import time
from collections import deque
from threading import Lock
from typing import TYPE_CHECKING

import steamcontroller.uinput as sui
from steamcontroller import SCI_NULL, SCButtons, SCStatus
from steamcontroller.events import EventMapper

from triton import inputsrc, screen, state, utils, vkb, vptr
from triton.pad import _PadMixin
from triton.screen import CoordFraction
from triton.stick import _StickMixin
from triton.triggers import _TriggerMixin

if TYPE_CHECKING:
    from triton.vptr import VirtualPointer


class ControllerState:
    click_queue = deque()

    _pointers: "tuple[VirtualPointer, VirtualPointer] | None" = None
    _pointer_lock = Lock()

    def set_pointers(self, ptr_left, ptr_right):
        with self._pointer_lock:
            self._pointers = (ptr_left, ptr_right)

    def get_pointers(self) -> "tuple[VirtualPointer, VirtualPointer] | None":
        # Returns the SAME tuple (no deepcopy): a published pointer is never
        # mutated after it is smoothed+published — handle_input creates a fresh
        # VirtualPointer each frame and only the fresh one's coord is updated
        # in place (VirtualPointer.smoothen); the stored prev/`published
        # pointers are only ever READ. So the readers (render loop, open/close
        # anim, next frame's smoothen) can share the objects safely.
        with self._pointer_lock:
            return self._pointers


# Bit masks that are static per input frame (rebuilt once at module level).
# Buttons asserted while the controller is merely held/resting (see
# handle_input's "controller is actively in use" activity gate).
_RESTING_BITS = (
    SCButtons.LPADTOUCH
    | SCButtons.RPADTOUCH
    | SCButtons.LPADJOY_TOUCH
    | SCButtons.RPADJOY_TOUCH
    | SCButtons.LGRIP_REST
    | SCButtons.RGRIP_REST
)
# The four DPAD directions (the full set of directional bits).
_DPAD_MASK = (
    SCButtons.DPAD_UP
    | SCButtons.DPAD_DOWN
    | SCButtons.DPAD_LEFT
    | SCButtons.DPAD_RIGHT
)


# Right-stick-as-mouse tuning (shared by every controller while the OSK is
# open). Mirrors the tray's desktop-mode cursor feel.
_MOUSE_DEADZONE = 6000
_MOUSE_SPEED = 1400.0  # px/sec at full stick deflection
# Bigger exponent = longer ramp (more stick travel maps to slow speeds), so
# precise control needs less surgical thumb precision. Matches the tray _Watcher.
_MOUSE_EXPONENT = 5.0
# Minimum speed (fraction of full) the instant the stick passes the deadzone, so
# the first bit of travel moves a usable amount (>1px/frame) instead of the
# near-zero the steep exponent gives — fine control needs perceptible feedback.
_MOUSE_MIN = 0.05


def _mouse_vector(x, y, deadzone, exponent):
    """Radial stick → mouse velocity (vx, vy), each component scaled 0..1. Speed
    is a function of the stick's DISTANCE from center applied to the unit
    direction — NOT per-axis — so a diagonal full push moves at the same speed as
    a pure horizontal/vertical push. (Applying the exponent per-axis made
    diagonals ~radius·exp slower, very visible at high exponents.)"""
    mag = (x * x + y * y) ** 0.5
    if mag <= deadzone:
        return 0.0, 0.0
    m = min(1.0, (mag - deadzone) / (32767.0 - deadzone))
    # Floor + ramp: a small flat minimum the moment we pass the deadzone, then the
    # m**exponent curve on top (the floor only matters near center, where m**exp≈0).
    unit = _MOUSE_MIN + (1.0 - _MOUSE_MIN) * (m**exponent)
    scaled = unit / mag  # `unit` speed along the unit vector (x/mag, y/mag)
    return x * scaled, y * scaled


def adjust_raw_x(raw_x, center_fraction, scalar=6 / 5):
    """Map the touchpad's raw X to a screen X. The scalar overshoots the window
    on purpose so every key is reachable, but the raw extremes land FAR outside
    the window (e.g. x=-579 / +2508), where a click resolves to NO key — the
    edges of the keyboard are dead. Clamp into the window so the full pad maps
    to clickable keys: the finger reaches the window edge and stops instead of
    entering a dead zone."""
    abs_max = 0x20000
    x = screen.width * (center_fraction + scalar * raw_x / abs_max)
    return utils.round_to_int(utils.clamp(x, 0, screen.width))


def adjust_raw_x_span(raw_x, span_start, span_end):
    """Map the touchpad's raw X across the fixed screen interval
    [span_start, span_end]. Split layout uses this so each pad covers exactly
    its own half's key span — the left pad [0, band_left], the right pad
    [band_right, width] — instead of half the (much wider) display, most of
    which is transparent middle gap where no key lives. The raw X is the HID
    int16 (±0x8000) from the report struct — normalized against the TRUE full
    scale so the pad's whole travel lands in its band (a ±0x20000 scale would
    squeeze every pad into the middle 25% of its span, making the outer keys
    unreachable)."""
    abs_max = 0x8000
    frac = (raw_x + abs_max) / (2 * abs_max)
    x = span_start + frac * (span_end - span_start)
    return utils.round_to_int(utils.clamp(x, 0, screen.width))


def adjust_raw_y(raw_y, center_fraction, scalar=6 / 5):
    """Map the touchpad's raw Y to a screen Y, clamped into the window (see
    adjust_raw_x). Without the clamp the top of the pad maps to y=-258 and the
    bottom to y=+627 — both outside the keyboard, so the top/bottom edges were
    not clickable."""
    abs_max = 0x10000
    y = screen.height * (center_fraction + scalar * -raw_y / abs_max)
    return utils.round_to_int(utils.clamp(y, 0, screen.height))


class ControllerManager(_PadMixin, _StickMixin, _TriggerMixin):
    # Touchpad pointer tracking. The touchpad is an ABSOLUTE pointer (finger ->
    # screen position); the OSK pointer must track the finger exactly, frame to
    # frame, so there is NO smoothing here. Alpha 1.0 makes the low-pass an
    # identity (prev + 1.0*(curr-prev) = curr), so the pointer is the raw
    # finger position every frame - no lag, and the raw coords are already
    # integer pixels, so nothing jitters. The only deliberate glide is
    # _pad_lock_glide_alpha, applied when a pad press locks the cursor onto a
    # key center.
    pad_track_alpha = 1.0
    sc_input_previous = SCI_NULL
    # Grace window after the OSK opens during which a Steam release is NOT
    # treated as a "close" gesture. Covers the Steam Controller HID handoff: the
    # OSK appears a beat before its SteamHidSource re-acquires the controller, so
    # the merged Steam reads released first (clearing the open seed below), then
    # the SC reconnects still carrying the open chord's lingering Steam — whose
    # release would otherwise instantly close the just-opened keyboard (~0.5 s).
    _OPEN_CLOSE_GRACE = 1.0

    def __init__(self, controller_state):
        self.controller_state = controller_state

        prev_ptrs = controller_state.get_pointers()
        self.prev_ptr_left = prev_ptrs[0]
        self.prev_ptr_right = prev_ptrs[1]

        # Steam+X / Steam-alone chord tracking. Seed both TRUE: the OSK was just
        # opened by a Steam(+X) chord that may still be held on the first frame —
        # the OSK appears a beat after the chord, by which point X is released
        # but Steam often isn't. Seeding _steam_was_pressed=True stops the first
        # frame from treating that lingering Steam as a fresh press, and
        # _saw_x_during_steam=True marks the opening chord as "used" so the tail
        # of its Steam release doesn't immediately close the keyboard. A later,
        # deliberate Steam tap still closes normally (its own rising edge
        # re-clears the flag).
        self._steam_was_pressed = True
        self._saw_x_during_steam = True
        # When the OSK opened, for _OPEN_CLOSE_GRACE (the Steam-release auto-close
        # is suppressed until this elapses, so the SC reconnect blip can't close).
        self._open_t = time.monotonic()

        self.evm = EventMapper()
        self._map_events()

    def _map_events(self):
        # Face buttons whose action is unconditional ride EventMapper. The
        # conditional bindings (LT/RT switch role while the same-side touchpad
        # is being touched) and the latching ones (L3, B, LGRIP, A, DPAD) are
        # handled manually in handle_input.
        # X → Backspace is handled manually below so it can hold-to-repeat
        # (slow continuous delete); the rest ride EventMapper as single taps.
        self.evm.setButtonAction(SCButtons.Y, sui.Keys.KEY_SPACE)  # Y → Space
        # R4 / R5 back paddles → Space (Steam OSK official mapping).
        self.evm.setButtonAction(SCButtons.RGRIP1, sui.Keys.KEY_SPACE)
        self.evm.setButtonAction(SCButtons.RGRIP2, sui.Keys.KEY_SPACE)

        # Rising-edge latches for manually-handled buttons.
        self._l3_was_pressed = False
        self._b_was_pressed = False
        self._lgrip_was_pressed = False
        self._a_was_pressed = False
        # A-button (press key under cursor) hold-to-repeat clock, same cadence
        # as X; the main thread only repeats it over Backspace.
        self._a_repeat_at = 0.0
        self._start_was_pressed = False  # START / "+" (position-cycle edge)
        self._view_was_pressed = (
            False  # VIEW / "-" (Steam+VIEW Alt+Tab; alone = position-cycle)
        )
        self._alt_held_for_tab = False
        self._dpad_prev = 0
        # X (Backspace) hold-to-repeat: deletes once on press, then slow-repeats
        # while held. _x_repeat_at is the monotonic time of the next repeat.
        self._x_was_pressed = False
        self._x_repeat_at = 0.0
        # Same hold-to-repeat for the pad "enter the key" action (trackpad
        # click; L2/R2 only on SDL pads — SC triggers are disabled): while
        # held, re-enter the key on the same BACKSPACE clock. Keyed per pad
        # (by select-button mask) so the left and right pads keep independent
        # timers. The main thread only repeats a hit
        # that lands on Backspace, so holding rubs out text like the X button.
        self._click_repeat_at = {}
        # Per-pad debounce/settle clock (see pad._PadMixin.PAD_CLICK_SETTLE):
        # monotonic time until which a re-engage on that pad is swallowed as a
        # force-wobble phantom insert. Keyed like _click_repeat_at, by the
        # select-button mask, so the left and right pads stay independent.
        self._click_settle_at = {}
        # Per-pad deferred variant-capable presses (Feature B defer model):
        # {select-button mask: CoordFraction}. A press edge on a letter with
        # variants queues NOTHING here-first — the coord sits in this dict
        # until the release decides base (queue it back) vs variant (committed
        # by the row). Cleared on release / teardown so a held pad press never
        # leaks a base insert across presses.
        self._deferred_base = {}
        # LT's role is decided on its rising edge from whether the left pad was
        # being touched: "shift" (pressed untouched) or "click" (pressed while
        # touching). Latched until LT is released so a later touch can't flip it.
        self._lt_prev = False
        self._rt_prev = False
        self._lt_role = None
        # Steam + left-stick media chords: track the stick's current direction
        # zone (edge-triggered) and the next allowed repeat time for volume.
        self._stick_zone_prev = "NEUTRAL"
        self._stick_repeat_at = 0.0
        # Left stick → keyboard cursor navigation (when Steam is NOT held).
        # Separate zone/repeat state from the media chord so the two stick
        # roles don't clobber each other's edge tracking.
        self._kbd_stick_zone_prev = "NEUTRAL"
        self._kbd_stick_repeat_at = 0.0
        self._kbd_scroll_at = 0.0  # next left-stick scroll tick (nav-off mode)
        self._kbd_scroll_zone_prev = (
            "NEUTRAL"  # arrow-stick zone for the scroll
        )
        # Fire a single haptic "open" tick on the first input frame.
        self._open_tick_pending = True
        # Steam-hold suppression of firmware lizard (kb/mouse) — see comment
        # in handle_input below.
        self._passive_lizard_suppressed = False
        self._last_lizard_suppress = 0.0
        # Tracks whether we are currently holding KEY_LEFTSHIFT / KEY_ENTER on
        # the OS side (driven by LT/RT but gated by touchpad contact).
        self._shift_active = False
        self._enter_active = False
        # Pad "click" latch from the raw trackpad FORCE (see _press_click):
        # the SC's discrete click bits can be suppressed by the Steam config,
        # so the OSK detects the press from the always-streamed force fields.
        self._lpad_click_held = False
        self._rpad_click_held = False
        # Press-lock targets (see _pad_lock_target): the key-center
        # CoordFraction the cursor is frozen on while a pad press sits in
        # the hold band, or None while tracking the finger.
        self._lpad_lock_target = None
        self._rpad_lock_target = None
        # Pad press-force calibration from settings.json (published by the
        # tray at OSK open): (click engage, click release, press hold,
        # lock-glide alpha). See the calibration comment above.
        (
            self._pad_click_engage,
            self._pad_click_release,
            self._pad_press_hold,
            self._pad_lock_glide_alpha,
        ) = state.get_sc_pad_press()
        # Static-per-session settings, cached here like _pad_click_*: the tray
        # publishes these ONCE at startup (no live re-publish path), so the
        # per-frame state reads below are pure lock overhead.
        self._trigger_focus_pull = state.get_sc_trigger_focus_pull()
        self._stick_nav_enabled = state.is_sc_kbd_stick_nav_enabled()
        # In "control desktop" mode (Sticks Control Keyboard OFF) L2/R2 act as
        # the left/right MOUSE buttons instead of Shift/Enter — unless the pad is
        # being touched, where they keep the OSK key-press role. Track the held
        # state so the button mirrors the trigger (press/release, drag).
        self._mouse_l_active = False
        self._mouse_r_active = False
        self._select_dir = 0  # direction (+1/-1) of the last arrow fired this drag, 0 = none yet
        self._select_base_dir = (
            0  # prevailing (first) drag direction; reverse = opposite this
        )
        self._select_reverse_buffer = []
        self._select_real_touch = False  # last real finger-contact state for select mode  # (t, dirn) reverse arrows within SELECT_ROLLBACK_WINDOW
        self._kb = sui.Keyboard()
        # Right-stick-as-mouse while the OSK is open. Works for ANY controller in
        # the merged frame (Steam Controller, Switch Pro, Xbox, ...): the right
        # stick moves the system cursor so you can point-and-click the keys (or
        # anything else) without closing the keyboard. The right stick is unused
        # by the OSK otherwise (it navigates via the left stick / DPAD / pads).
        self._mouse = sui.Mouse()
        self._mouse_acc_x = 0.0
        self._mouse_acc_y = 0.0
        self._mouse_last_t = 0.0
        # Defer model (Feature B): A-button press on a variant-capable letter
        # is NOT typed at the press edge; the cell is held here until release
        # decides base (typed via queue_key_press) vs variant (committed by
        # the row). None = no deferred A press.
        self._a_deferred_cell = None

    def _a_cell_has_variants(self, row, col):
        """True if the key at (row, col) has accented variants (Feature B),
        so an A press on it must defer the base until release."""
        kb = state.get_virtual_kb()
        if kb is None:
            return False
        if not (0 <= row < len(kb.keys) and 0 <= col < len(kb.keys[row])):
            return False
        return vkb.diacritic_variants_for_key(kb.keys[row][col]) is not None

    # Left-stick deflection (int16) past this magnitude counts as a direction.
    STICK_DEADZONE = 14000
    # Volume feel: a tap = one step. Holding up/down past STICK_HOLD_DELAY
    # seconds then rapidly ramps, one step every STICK_VOL_REPEAT seconds.
    STICK_HOLD_DELAY = 0.5
    STICK_VOL_REPEAT = 0.021
    # Left-stick keyboard navigation: tap = one key; held past the delay it
    # repeats one key every KBD_STICK_REPEAT seconds (slow enough to land on
    # the intended key without overshooting).
    KBD_STICK_HOLD_DELAY = 0.35
    KBD_STICK_REPEAT = 0.15
    # Deflection before the left stick steps the OSK key cursor. 32% larger than
    # the base STICK_DEADZONE (20% then another 10%) so the cursor doesn't
    # actuate on a light push (the media chord keeps the smaller STICK_DEADZONE).
    KBD_STICK_DEADZONE = round(STICK_DEADZONE * 1.32)
    # When "Sticks Control Keyboard" is OFF, the SC left stick scrolls the window
    # behind the OSK. It sends ARROW-KEY taps (not mouse-wheel notches) with the
    # SAME deadzone (STICK_DEADZONE) / hold / repeat as the tray _Watcher's
    # desktop arrow-stick, so the scroll speed is identical whether the OSK is
    # open or closed. (A wheel notch scrolls ~3 lines vs an arrow's ~1, which is
    # why the old wheel-based scroll felt faster than the closed-OSK scroll.)
    KBD_SCROLL_HOLD_DELAY = 0.35
    KBD_SCROLL_REPEAT = 0.05 / 0.7 * 1.1
    _SCROLL_ARROW_KEYS = {
        "UP": sui.Keys.KEY_UP,
        "DOWN": sui.Keys.KEY_DOWN,
        "LEFT": sui.Keys.KEY_LEFT,
        "RIGHT": sui.Keys.KEY_RIGHT,
    }
    # Hold-to-repeat cadence for every controller "press a key" path (X, A,
    # L2/R2/pad-click): one hit on press, then (after holding past the delay) a
    # deliberately slow repeat. Single-sourced from vkb so the mouse path and
    # every key (Backspace + arrows) rub out / step at one matched speed.
    BACKSPACE_HOLD_DELAY = vkb.KEY_REPEAT_DELAY
    BACKSPACE_REPEAT = vkb.KEY_REPEAT_INTERVAL
    # Pad "click" from the raw trackpad FORCE (OpenPuck protocol: s16 press
    # fields at 0x16/0x1C of the 0x45 report). Steam's config can suppress
    # the firmware's discrete click bits (an unbound click stops them from
    # being generated), but the force is always streamed — so the OSK detects
    # the press itself and pad-click typing works out of the box with no
    # Steam-side setup. The four calibration values (click engage / click
    # release / press hold / lock-glide alpha) are NOT constants: they live
    # in settings.json (tray.py DEFAULT_SETTINGS sc_pad_* keys) and are read
    # once per OSK session in __init__ via state.get_sc_pad_press(). Measured
    # on hardware (2026-08-09): the pad's physical click/vibration engages at
    # ~2500 force — an earlier 3000 engage made the key record a hair late,
    # after the vibration was already felt. Light resting/sliding reads well
    # below 1000, so movement doesn't false-trigger.

    def handle_input(self, sc, sc_input):
        self.evm.process(sc, sc_input)

        # Haptic feedback: one tick when the keyboard first opens.
        if self._open_tick_pending:
            self._open_tick_pending = False
            state.haptic_tick()

        # Single monotonic timestamp for this frame, used by every hold-to-
        # repeat clock below (DPAD/A, X, the pad-click repeat, media stick).
        now = time.monotonic()
        # Publish "the controller is actively in use" (see state.py) so triton's
        # mouse handling can ignore Steam Input's injected duplicates — Steam's
        # desktop config emulates a mouse from the controller, and the OSK
        # can't tell that injection apart from a real physical mouse. Resting
        # TOUCH alone doesn't count — not just the trackpads, but the
        # always-on grip-rest bits (asserted while the controller is merely
        # held) and the pad-as-joystick touch bits (asserted on contact) — so
        # the gate clears as soon as typing stops even with a hand parked on
        # the controller.
        if (
            (sc_input.buttons & ~_RESTING_BITS)
            or abs(sc_input.lstick_x) > _MOUSE_DEADZONE
            or abs(sc_input.lstick_y) > _MOUSE_DEADZONE
            or abs(sc_input.rstick_x) > _MOUSE_DEADZONE
            or abs(sc_input.rstick_y) > _MOUSE_DEADZONE
        ):
            state.set_last_controller_activity(now)

        # Right stick -> system mouse cursor so any pad can point-and-click the
        # OSK keys (hover highlights, A presses the hovered key) without closing
        # the keyboard. Sub-pixel motion is accumulated so slow nudges register.
        dt = now - self._mouse_last_t if self._mouse_last_t else 0.0
        self._mouse_last_t = now
        if dt <= 0.0 or dt > 0.1:
            dt = 1.0 / 60.0
        # Radial speed (see _mouse_vector) so diagonals aren't slower than axes.
        mouse_speed = _MOUSE_SPEED
        _mvecx, _mvecy = _mouse_vector(
            sc_input.rstick_x,
            sc_input.rstick_y,
            _MOUSE_DEADZONE,
            _MOUSE_EXPONENT,
        )
        self._mouse_acc_x += _mvecx * mouse_speed * dt
        # Stick-up moves the cursor up; screen Y grows downward, so invert.
        self._mouse_acc_y += -_mvecy * mouse_speed * dt
        _mvx, _mvy = int(self._mouse_acc_x), int(self._mouse_acc_y)
        self._mouse_acc_x -= _mvx
        self._mouse_acc_y -= _mvy
        if _mvx or _mvy:
            self._mouse.move(_mvx, _mvy)
            # Flag this injection so triton's mouse gate still lets the OSK
            # highlight follow OUR movement (see state.set_osk_mouse_inject).
            state.set_osk_mouse_inject(now)

        # Steam held gates the media chords below (Steam + left stick / L3).
        steam_now = bool(
            sc_input.buttons & (SCButtons.STEAM | SCButtons.QAM)
        )  # "..." (QAM) acts like Steam

        # L3 → Caps Lock, unless Steam is held, in which case Steam + L3 is
        # Play/Pause. Manual rising-edge detection so the binding doesn't
        # re-fire while the user keeps their finger on the stick after clicking.
        l3_pressed = bool(sc_input.buttons & SCButtons.L3)
        if l3_pressed and not self._l3_was_pressed:
            if steam_now:
                self._kb.pressEvent([sui.Keys.KEY_PLAYPAUSE])
                self._kb.releaseEvent([sui.Keys.KEY_PLAYPAUSE])
                # Mark the Steam press as "used" so releasing it doesn't close
                # the OSK (same rule as the Steam + VIEW chord below).
                self._saw_x_during_steam = True
            else:
                self._kb.pressEvent([sui.Keys.KEY_CAPSLOCK])
                self._kb.releaseEvent([sui.Keys.KEY_CAPSLOCK])
        self._l3_was_pressed = l3_pressed

        # Publish touchpad capacitive-touch state so the renderer can hide
        # the L2/R2 hint glyphs while LT/RT's pad-click alternate is active.
        lpad_touched = bool(sc_input.buttons & SCButtons.LPADTOUCH)
        rpad_touched = bool(sc_input.buttons & SCButtons.RPADTOUCH)
        state.set_pad_touched(lpad_touched, rpad_touched)

        # "Control the desktop" mode (sc_left_stick_nav OFF): the OSK is
        # click-through and L2/R2 act as the LEFT/RIGHT mouse buttons — UNLESS
        # the matching pad is touched, where they keep their OSK key-press role.
        desktop_mode = not self._stick_nav_enabled

        # SC triggers are dead in the OSK. Steam Input delivers trigger presses
        # to the game natively (per-game "game actions" — the Satisfactory
        # leak), so ANY OSK trigger role (Shift / Enter / mouse / key-insert)
        # would leak every press into the game under the frozen cursor. The SC
        # enters keys by TRACKPAD CLICK instead — a physical pad press always
        # selects, see handle_pad_input.
        sc_triggers_off = True

        # LT (L2) role, fixed at the moment it's pressed:
        #   • touching the left pad → "click" the key under the pointer (queued
        #     by handle_pad_input below); shift state is whatever is currently
        #     latched/held, same as a plain touchpad click with no L2.
        #   • else in desktop mode  → "mouse" = hold the LEFT mouse button.
        #   • else                  → "shift". Held until LT releases, even if you
        #     then touch the pad, so you can slide the pad without dropping Shift.
        lt_pressed = (
            False
            if sc_triggers_off
            else self._osk_trigger_pressed(
                sc_input.buttons, SCButtons.LT, sc_input.ltrig
            )
        )
        lt_was = (
            self._lt_prev
        )  # capture before the update below, for handle_pad_input's edge
        if lt_pressed and not self._lt_prev:
            if lpad_touched:
                self._lt_role = "click"
            elif desktop_mode:
                self._lt_role = "mouse"
            else:
                self._lt_role = "shift"
                # Pulling L2 takes over from a mouse-toggled Shift latch and
                # stops the toggle. The controller re-presses Shift just
                # below, so it stays held under L2 instead of the latch.
                # Only the "shift" role does this: a "click" (pad touched) or
                # "mouse" (desktop mode) L2 press is unrelated to Shift, and
                # clearing the latch there would un-latch a Shift the user
                # just toggled on and turn the click's key unshifted.
                vkb.clear_shift_latch(release_os=not self._shift_active)
        elif not lt_pressed:
            self._lt_role = None
        self._lt_prev = lt_pressed
        shift_should_hold = lt_pressed and self._lt_role == "shift"
        if shift_should_hold and not self._shift_active:
            self._kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = True
            state.pad_click_haptic()  # strong tick when Shift engages (match pad click)
        elif not shift_should_hold and self._shift_active:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = False
        # L2 → RIGHT mouse button while in the "mouse" role (press / hold to drag
        # / release). L2/R2 are swapped vs the obvious mapping, per user pref.
        # (_mouse_l_active just means "L2's mouse button is held".)
        mouse_l_hold = lt_pressed and self._lt_role == "mouse"
        if mouse_l_hold and not self._mouse_l_active:
            self._mouse.press("right")
            self._mouse_l_active = True
            state.pad_click_haptic()  # click feedback, like a pad press
        elif not mouse_l_hold and self._mouse_l_active:
            self._mouse.release("right")
            self._mouse_l_active = False

        # RT (R2): right pad touched → OSK key-press (handle_pad_input, below);
        # else in desktop mode → hold the LEFT mouse button; else → Enter.
        rt_pressed = (
            False
            if sc_triggers_off
            else self._osk_trigger_pressed(
                sc_input.buttons, SCButtons.RT, sc_input.rtrig
            )
        )
        rt_was = (
            self._rt_prev
        )  # for handle_pad_input's analog-aware click edge
        self._rt_prev = rt_pressed
        enter_should_hold = (
            rt_pressed and not rpad_touched and not desktop_mode
        )
        if enter_should_hold and not self._enter_active:
            self._kb.pressEvent([sui.Keys.KEY_ENTER])
            self._enter_active = True
            state.pad_click_haptic()  # strong tick when Enter engages (match pad click)
        elif not enter_should_hold and self._enter_active:
            self._kb.releaseEvent([sui.Keys.KEY_ENTER])
            self._enter_active = False
        # R2 → LEFT mouse button in desktop mode while the right pad is not
        # touched. (_mouse_r_active just means "R2's mouse button is held".)
        mouse_r_hold = rt_pressed and not rpad_touched and desktop_mode
        if mouse_r_hold and not self._mouse_r_active:
            self._mouse.press("left")
            self._mouse_r_active = True
            state.pad_click_haptic()  # click feedback, like a pad press
        elif not mouse_r_hold and self._mouse_r_active:
            self._mouse.release("left")
            self._mouse_r_active = False

        # Mirror Shift state to the renderer so it can show uppercase labels.
        # OR in the mouse/click latch so a controller frame doesn't stomp a
        # latched Shift (which would desync the display and break the toggle).
        # Note: select mode does NOT count as a displayed Shift -- the Select key
        # holds OS Shift for arrow-selection, but the on-screen labels must stay
        # lowercase while it is held.
        state.set_shift_held(self._shift_active or state.is_shift_latched())

        # B and L4/L5 (LGRIP) close the keyboard, on rising edge.
        b_pressed = bool(sc_input.buttons & SCButtons.B)
        if b_pressed and not self._b_was_pressed:
            state.close()
        self._b_was_pressed = b_pressed

        lgrip_pressed = bool(sc_input.buttons & SCButtons.LGRIP)
        if lgrip_pressed and not self._lgrip_was_pressed:
            state.close()
        self._lgrip_was_pressed = lgrip_pressed

        # DPAD navigates the cursor over the keyboard grid (one step per
        # press). Direction events are queued for the main loop, which knows
        # the layout's pixel widths and can pick the visually-aligned target.
        dpad_mask = _DPAD_MASK
        dpad_now = sc_input.buttons & dpad_mask
        dpad_newly = (self._dpad_prev ^ dpad_now) & dpad_now
        # DPAD navigation deliberately does NOT clear the Shift latch: a Shift
        # toggled on via DPAD+A must persist while you move the cursor to the
        # keys you want to capitalise, just like the mouse toggle. Only the L2
        # hold model resets the latch (handled in the LT block above).
        if dpad_newly & SCButtons.DPAD_UP:
            state.queue_dpad("UP")
        if dpad_newly & SCButtons.DPAD_DOWN:
            state.queue_dpad("DOWN")
        if dpad_newly & SCButtons.DPAD_LEFT:
            state.queue_dpad("LEFT")
        if dpad_newly & SCButtons.DPAD_RIGHT:
            state.queue_dpad("RIGHT")
        self._dpad_prev = dpad_now

        # A → press the key currently under the DPAD cursor. Press once on the
        # rising edge, then (held past BACKSPACE_HOLD_DELAY) repeat on the
        # BACKSPACE clock. The main thread only repeats a hit that lands on
        # Backspace, so holding A on the delete key rubs out text like X.
        a_pressed = bool(sc_input.buttons & SCButtons.A)
        a_row, a_col = state.get_cursor()
        if a_pressed and not self._a_was_pressed:
            # A is a press/toggle model like the mouse: pressing it does NOT
            # clear the Shift latch, so a Shift toggled on via DPAD+A stays on
            # until Shift is pressed again (only L2's hold model resets it).
            # Defer model (Feature B): a press on a variant-capable letter
            # types NOTHING at the press edge — the hold opens its variant
            # row (via the repeat path below) and the release picks base vs
            # variant. Remember the cell so the release can type the base.
            if self._a_cell_has_variants(a_row, a_col):
                self._a_deferred_cell = (a_row, a_col)
                # Click sound fires at the PRESS edge like any other key; the
                # release commit types the char but must not re-tick.
                state.key_sound_tick()
            else:
                state.queue_key_press(a_row, a_col)
            self._a_repeat_at = now + self.BACKSPACE_HOLD_DELAY
        elif a_pressed and now >= self._a_repeat_at:
            state.queue_key_press(a_row, a_col, repeat=True)
            self._a_repeat_at = now + self.BACKSPACE_REPEAT
        elif self._a_was_pressed and not a_pressed:
            # A released. If the hold opened a variant row (Feature B, held
            # past the delay), commit the chosen variant — the base was NEVER
            # typed at the press edge (defer model), so the commit just types
            # the variant (no Backspace). A base selection (index -1), or a
            # quick tap with no row opened, types the base letter instead.
            cell = self._a_deferred_cell
            self._a_deferred_cell = None
            if (
                state.is_diacritic_open()
                and state.get_diacritic_source() == "a"
            ):
                char = state.get_diacritic_selected_char()
                if char is not None:
                    self.controller_state.click_queue.append(("variant", char))
                else:
                    # Same model as the pad: while the row is open, only
                    # special letters are selectable — no pick defaults to
                    # the first variant, never the base letter.
                    variants = state.get_diacritic_variants_list()
                    if variants:
                        self.controller_state.click_queue.append(
                            ("variant", variants[0])
                        )
                    state.close_diacritic()
            elif cell is not None:
                # A-release quick tap of a deferred variant-capable key: the
                # press edge already clicked, so type the base SILENTLY.
                state.queue_key_press(*cell, silent=True)
            self._a_repeat_at = 0.0
        # Paint the cursor key blue (CLICK) while A is held, so a controller
        # press flashes like a mouse click. Reuses the mouse press-cell slot;
        # only touched on A's edges/hold so it never clobbers a mouse press.
        if a_pressed:
            state.set_mouse_press_cell((a_row, a_col))
        elif self._a_was_pressed:
            state.set_mouse_press_cell(None)
        self._a_was_pressed = a_pressed

        # Visual highlight: paint the on-screen key blue while its bound
        # controller button is held down.
        highlights = set()
        if self._shift_active or state.is_shift_latched():
            highlights.add(sui.Keys.KEY_LEFTSHIFT)
            highlights.add(sui.Keys.KEY_RIGHTSHIFT)
        if state.is_ctrl_latched():
            highlights.add(sui.Keys.KEY_LEFTCTRL)
            highlights.add(sui.Keys.KEY_RIGHTCTRL)
        if state.is_alt_latched():
            highlights.add(sui.Keys.KEY_LEFTALT)
            highlights.add(sui.Keys.KEY_RIGHTALT)
        if l3_pressed and not steam_now:
            highlights.add(sui.Keys.KEY_CAPSLOCK)
        if sc_input.buttons & SCButtons.X:
            highlights.add(sui.Keys.KEY_BACKSPACE)
        if self._enter_active:
            highlights.add(sui.Keys.KEY_ENTER)
        if sc_input.buttons & (SCButtons.Y | SCButtons.RGRIP):
            highlights.add(sui.Keys.KEY_SPACE)
        if state.is_select_active():
            highlights.add(sui.Keys.KEY_SELECT)
        state.set_highlighted(highlights)

        # Steam+X opens the keyboard; Steam pressed and released alone closes it.
        # (steam_now was computed at the top of this method.)
        x_now = bool(sc_input.buttons & SCButtons.X)
        if steam_now and not self._steam_was_pressed:
            self._saw_x_during_steam = False
        if steam_now and x_now and not self._saw_x_during_steam:
            self._saw_x_during_steam = True
            state.show()

        # The OSK owns the controller while it's open, so firmware lizard
        # (kb/mouse) must stay OFF the whole time — otherwise the firmware
        # ALSO emits its own keys/clicks (D-pad→arrows, A→click/Enter) into the
        # focused window on top of the OSK reading the same buttons. In gamepad
        # mode the OSK opens with Steam+X (Steam held); releasing Steam used to
        # restore lizard ON, which then navigated and launched items in the
        # focused Start menu while the user typed. The device is opened lizard-
        # off (with a watchdog); re-assert OFF during a Steam hold (so a
        # Steam+VIEW=Alt+Tab chord isn't fought by a firmware Tab) and force it
        # back OFF — never ON — on release.
        if steam_now:
            if (
                not self._passive_lizard_suppressed
                or now - self._last_lizard_suppress > 2.0
            ):
                sc.set_lizard(False)
                self._passive_lizard_suppressed = True
                self._last_lizard_suppress = now
        elif self._passive_lizard_suppressed:
            sc.set_lizard(False)
            self._passive_lizard_suppressed = False

        # X → Backspace, handled here (not via EventMapper) so holding it
        # slow-repeats the delete. One delete on press, then after
        # BACKSPACE_HOLD_DELAY a delete every BACKSPACE_REPEAT seconds. Gated
        # off while Steam is held, since Steam+X opens the keyboard.
        x_pressed = bool(sc_input.buttons & SCButtons.X) and not steam_now
        if x_pressed and not self._x_was_pressed:
            self._kb.pressEvent([sui.Keys.KEY_BACKSPACE])
            self._kb.releaseEvent([sui.Keys.KEY_BACKSPACE])
            self._x_repeat_at = now + self.BACKSPACE_HOLD_DELAY
        elif x_pressed and now >= self._x_repeat_at:
            self._kb.pressEvent([sui.Keys.KEY_BACKSPACE])
            self._kb.releaseEvent([sui.Keys.KEY_BACKSPACE])
            self._x_repeat_at = now + self.BACKSPACE_REPEAT
        self._x_was_pressed = x_pressed

        # Steam + left stick → media transport (volume / track skip).
        self._handle_media_stick(sc_input, steam_now, now)

        # Left stick (no Steam) → move the on-screen-keyboard cursor.
        self._handle_kbd_stick(sc_input, steam_now, now)

        # Steam + VIEW ("-" on the Switch Pro / small button upper-right of the
        # Steam logo) → Alt+Tab. Hold Alt for the duration of the Steam hold so
        # the switcher stays visible; each VIEW rising edge taps Tab once to
        # advance one slot. Releasing Steam drops Alt and commits the selection.
        # Marks the Steam press as "used" so releasing Steam doesn't close the OSK.
        view_now = bool(sc_input.buttons & SCButtons.VIEW)
        if steam_now and view_now and not self._view_was_pressed:
            if not self._alt_held_for_tab:
                self._kb.pressEvent([sui.Keys.KEY_LEFTALT])
                self._alt_held_for_tab = True
            self._kb.pressEvent([sui.Keys.KEY_TAB])
            self._kb.releaseEvent([sui.Keys.KEY_TAB])
            self._saw_x_during_steam = True
        elif view_now and not self._view_was_pressed:
            # VIEW alone ("-") → advance the OSK position rotation. This is the
            # Steam Controller's original position-cycle button.
            state.request_position_cycle()
        self._view_was_pressed = view_now
        # START ("+") → same OSK position cycle, the action as the Move key held
        # with Shift. Rising edge so a held button cycles once. (The Steam
        # Controller's original position-cycle button is VIEW, handled above;
        # both are accepted so the mapping survives either hand.)
        start_now = bool(sc_input.buttons & SCButtons.START)
        if start_now and not self._start_was_pressed:
            state.request_position_cycle()
        self._start_was_pressed = start_now
        if not steam_now and self._alt_held_for_tab:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTALT])
            self._alt_held_for_tab = False

        if (
            self._steam_was_pressed
            and not steam_now
            and not self._saw_x_during_steam
            and now - self._open_t > self._OPEN_CLOSE_GRACE
        ):
            state.close()
        self._steam_was_pressed = steam_now

        # SC pad "click" from raw FORCE (see _press_click). The latch is OR'd
        # into the buttons fed to handle_pad_input (and stored as the previous
        # frame) so its bit-based edge/repeat/release logic runs unchanged;
        # sc_input.buttons itself stays raw — only the latch is synthesized.
        # The physical press inserts only while sc_pad_click_enter is ON
        # (settings.json; default OFF — the click button is the primary
        # insert). The click button (sc_click_button settings.json, per side:
        # "L1/R1" bumpers default, "L2/R2" triggers) adds an "enter the key"
        # path with the exact same semantics as a same-side trackpad click:
        # while the left/right click button is held, that side's TOUCH+CLICK
        # bits are synthesized, so handle_pad_input fires the key under the
        # pad's pointer (press/release rumble, hold-to-repeat, CLICK
        # highlight) even if the thumb isn't on the pad — the pointer simply
        # sits where the finger last left it.
        click_left, click_right = state.get_sc_click_button() or (
            SCButtons.LB,
            SCButtons.RB,
        )
        click_l_pressed = bool(sc_input.buttons & click_left)
        click_r_pressed = bool(sc_input.buttons & click_right)
        l_pressed = (
            lpad_touched
            and state.get_sc_pad_click_enter()
            and self._press_click(sc_input.lpad_press, self._lpad_click_held)
        )
        r_pressed = (
            rpad_touched
            and state.get_sc_pad_click_enter()
            and self._press_click(sc_input.rpad_press, self._rpad_click_held)
        )
        self._lpad_click_held = l_pressed
        self._rpad_click_held = r_pressed
        frame_buttons = sc_input.buttons
        if not state.get_sc_pad_click_enter():
            # The firmware also asserts its own discrete LPAD/RPAD click bits
            # on a physical pad press (the force-latch is only needed because
            # a Steam config can suppress them). Those must NOT reach
            # handle_pad_input as a click while the pad click is disabled —
            # only the synthesized click-button bits below may insert.
            frame_buttons &= ~(SCButtons.LPAD | SCButtons.RPAD)
        if l_pressed or click_l_pressed:
            frame_buttons |= SCButtons.LPAD
        if r_pressed or click_r_pressed:
            frame_buttons |= SCButtons.RPAD
        if click_l_pressed:
            frame_buttons |= SCButtons.LPADTOUCH
        if click_r_pressed:
            frame_buttons |= SCButtons.RPADTOUCH
        pad_frame = (
            sc_input
            if frame_buttons is sc_input.buttons
            else sc_input._replace(buttons=frame_buttons)
        )

        if self.sc_input_previous == SCI_NULL:
            self.sc_input_previous = pad_frame
            return

        split = state.is_split_layout_enabled()
        band = None
        if split:
            kb = state.get_virtual_kb()
            if kb is not None:
                band = kb.split_gap_band()
        if band is not None:
            band_left, band_right = band
            ptr_left_coords = CoordFraction.from_absolute(
                adjust_raw_x_span(sc_input.lpad_x, 0, band_left),
                adjust_raw_y(sc_input.lpad_y, 1 / 2),
            )
            ptr_right_coords = CoordFraction.from_absolute(
                adjust_raw_x_span(
                    sc_input.rpad_x, band_right, screen.width
                ),
                adjust_raw_y(sc_input.rpad_y, 1 / 2),
            )
        else:
            ptr_left_coords = CoordFraction.from_absolute(
                adjust_raw_x(
                    sc_input.lpad_x,
                    1 / 4,
                    scalar=1 / 4 if split else 6 / 5,
                ),
                adjust_raw_y(sc_input.lpad_y, 1 / 2),
            )
            ptr_right_coords = CoordFraction.from_absolute(
                adjust_raw_x(
                    sc_input.rpad_x,
                    3 / 4,
                    scalar=1 / 4 if split else 6 / 5,
                ),
                adjust_raw_y(sc_input.rpad_y, 1 / 2),
            )

        # Feature B (diacritic variants): while a pad-driven variant row is
        # open, that pad's press lock is bypassed (and its stored target
        # cleared) so the pointer — and the row highlight — follow the finger
        # freely to pick a variant. The lock only makes sense for landing a
        # fixed click on a key; a held-to-extend needs free travel.
        diacritic_left = (
            state.is_diacritic_open()
            and state.get_diacritic_source() == "pad"
            and self._diacritic_pad == int(SCButtons.LT)
        )
        diacritic_right = (
            state.is_diacritic_open()
            and state.get_diacritic_source() == "pad"
            and self._diacritic_pad == int(SCButtons.RT)
        )
        if diacritic_left:
            self._lpad_lock_target = None
        if diacritic_right:
            self._rpad_lock_target = None

        # Press-aware cursor lock: while a pad press sits in the hold band the
        # pointer coordinates are pinned to the selected key's center (the
        # glide to it uses sc_pad_lock_glide_alpha below).
        lock_left = (
            None
            if diacritic_left
            else self._pad_lock_target(
                sc_input.lpad_press,
                lpad_touched,
                self._lpad_lock_target,
                ptr_left_coords,
                self.prev_ptr_left,
            )
        )
        lock_right = (
            None
            if diacritic_right
            else self._pad_lock_target(
                sc_input.rpad_press,
                rpad_touched,
                self._rpad_lock_target,
                ptr_right_coords,
                self.prev_ptr_right,
            )
        )
        # Click-button lock (sc_click_button): the button's own focus-on-key,
        # mirroring the pad-press lock — L1/R1 engages on the press itself,
        # L2/R2 on its analog pull crossing sc_trigger_focus_pull (default
        # half pull). Takes precedence over the pad lock when both apply.
        trigger_focus = self._trigger_focus_pull
        lt_focus = (
            click_left == SCButtons.LT
            and trigger_focus is not None
            and sc_input.ltrig >= trigger_focus
        )
        rt_focus = (
            click_right == SCButtons.RT
            and trigger_focus is not None
            and sc_input.rtrig >= trigger_focus
        )
        btn_lock_left = self._button_lock_target(
            (click_l_pressed or lt_focus) and not diacritic_left,
            self._lpad_lock_target,
            ptr_left_coords,
            self.prev_ptr_left,
        )
        btn_lock_right = self._button_lock_target(
            (click_r_pressed or rt_focus) and not diacritic_right,
            self._rpad_lock_target,
            ptr_right_coords,
            self.prev_ptr_right,
        )
        if btn_lock_left is not None:
            lock_left = btn_lock_left
        if btn_lock_right is not None:
            lock_right = btn_lock_right
        self._lpad_lock_target = lock_left
        self._rpad_lock_target = lock_right
        if lock_left is not None:
            # Fresh copy, not the stored target: VirtualPointer.smoothen
            # mutates its coord in place (update_absolute), and without the
            # copy every locked frame would lerp the stored key center off
            # the key — the click would land short of center.
            ptr_left_coords = CoordFraction.from_absolute(
                *lock_left.to_absolute()
            )
        if lock_right is not None:
            ptr_right_coords = CoordFraction.from_absolute(
                *lock_right.to_absolute()
            )

        input_state_left = self.handle_pad_input(
            ptr_left_coords,
            pad_frame.buttons,
            SCButtons.LPADTOUCH,
            SCButtons.LT,
            click_button_mask=SCButtons.LPAD,
            allow_click=self._lt_role == "click",
            now=now,
            trigger_pressed=lt_pressed,
            trigger_prev=lt_was,
            raw_x=sc_input.lpad_x,
            real_touch=lpad_touched,
        )
        input_state_right = self.handle_pad_input(
            ptr_right_coords,
            pad_frame.buttons,
            SCButtons.RPADTOUCH,
            SCButtons.RT,
            click_button_mask=SCButtons.RPAD,
            now=now,
            trigger_pressed=rt_pressed,
            trigger_prev=rt_was,
            raw_x=sc_input.rpad_x,
            real_touch=rpad_touched,
        )

        ptr_left = vptr.VirtualPointer(input_state_left, ptr_left_coords)
        ptr_right = vptr.VirtualPointer(input_state_right, ptr_right_coords)

        ptr_left.smoothen(
            self.prev_ptr_left,
            self._pad_lock_glide_alpha
            if lock_left is not None
            else self.pad_track_alpha,
        )
        ptr_right.smoothen(
            self.prev_ptr_right,
            self._pad_lock_glide_alpha
            if lock_right is not None
            else self.pad_track_alpha,
        )
        # Store the smoothed pointers as this frame's "previous" and publish
        # them. No copy needed: smoothen only ever mutates ITS receiver (the
        # fresh ptr_* above), never the stored prev — so the shared instances
        # are read-only from here on (see ControllerState.get_pointers).
        self.prev_ptr_left = ptr_left
        self.prev_ptr_right = ptr_right
        self.sc_input_previous = pad_frame

        self.controller_state.set_pointers(ptr_left, ptr_right)

    def release_held(self):
        """Release anything we're holding on the OS side so closing the OSK
        (which tears down this manager) can never strand a key or — worse — a
        mouse button down. Called from input_thread's finally."""
        if self._mouse_l_active:  # L2 holds the RIGHT button (swapped)
            self._mouse.release("right")
            self._mouse_l_active = False
        if self._mouse_r_active:  # R2 holds the LEFT button (swapped)
            self._mouse.release("left")
            self._mouse_r_active = False
        if self._shift_active:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self._shift_active = False
        if self._enter_active:
            self._kb.releaseEvent([sui.Keys.KEY_ENTER])
            self._enter_active = False
        if self._select_pad is not None:
            self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
            self._select_pad = None
            state.set_select_active(False)
        # Drop any deferred variant-capable press so a held pad press or A
        # button can never leak a base insert across a teardown.
        self._deferred_base.clear()
        self._a_deferred_cell = None


def update(sc, sc_input, manager):
    if state.should_close():
        # Triton is shutting down — tell the controller thread to exit so it
        # can run its cleanup (re-enable lizard mode) before being killed.
        sc.addExit()
        return
    if sc_input.status != SCStatus.INPUT:
        return
    manager.handle_input(sc, sc_input)


# Delay between controller (re)connect attempts while the keyboard is open but
# no controller is responding. Only ticks in that transient state; once a
# controller is open sc.run() blocks (no polling), and a closed keyboard isn't
# running this thread at all.
_RECONNECT_DELAY = 0.5


# Poll/merge cadence. The Steam Controller still streams at its own HID rate
# (SteamHidSource stashes its latest frame on its own thread); this loop reads
# every source, OR-merges, and dispatches one combined frame. ~250 Hz keeps the
# touchpad-pointer and haptic latency low without busy-spinning.
_MERGE_INTERVAL = 0.004


def input_thread(controller_state):
    manager = ControllerManager(controller_state)
    # The single input source: the custom Steam Controller hidapi driver
    # (trackpads, tuned haptics, lizard), wrapped by InputMerger — the
    # `sc` facade handle_input drives (set_lizard + the two haptic ticks fan
    # out to every source). The source self-reconnects, so a controller that
    # drops mid-session starts working again without reopening.
    merger = inputsrc.InputMerger()
    merger.add(inputsrc.SteamHidSource())
    # Expose haptic "ticks" to the main thread (dispatch_key buzzes on each key
    # press; the stronger pad-click tick for the simulated trackpad click).
    # Cleared on exit so a closed device's haptic methods are never called.
    state.set_haptic_tick(merger.haptic_click)
    state.set_pad_click_haptic(merger.haptic_pad_click)
    try:
        while not state.should_close():
            merged = merger.poll()
            if merged is not None:
                update(merger, merged, manager)
            time.sleep(_MERGE_INTERVAL)
    finally:
        # Drop any OS key / mouse button we were holding before tearing down,
        # so closing the OSK mid-pull can't strand (e.g.) the left mouse button.
        manager.release_held()
        state.set_haptic_tick(None)
        state.set_pad_click_haptic(None)
        # Stops the SDL pads and signals the Steam Controller thread to exit so
        # its cleanup (re-enable lizard mode) runs before we return.
        merger.close()
