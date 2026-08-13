from collections import deque
from typing import Protocol

import steamcontroller.uinput as sui

from triton import diacritics, state, vkb
from triton.screen import CoordFraction


class _PadClickHost(Protocol):
    """Subset of sui.Keyboard the pad mixin drives (press/release a key)."""

    def pressEvent(self, keys) -> None: ...

    def releaseEvent(self, keys) -> None: ...


class _PadControllerState(Protocol):
    """Subset of controller.ControllerState the pad mixin reads/writes."""

    click_queue: deque

    def set_pointers(self, ptr_left, ptr_right) -> None: ...


class _PadFrame(Protocol):
    """Subset of SteamControllerInput the pad mixin reads (button mask)."""

    buttons: int


class _PadMixin:
    # Attributes provided by the composed ControllerManager (declared here so
    # static tooling knows the mixin's contract — see controller.py __init__).
    _kb: _PadClickHost
    controller_state: _PadControllerState
    sc_input_previous: _PadFrame
    _click_repeat_at: dict
    _click_settle_at: dict
    _pad_click_engage: float
    _pad_click_release: float
    _pad_press_hold: float
    BACKSPACE_HOLD_DELAY: float
    BACKSPACE_REPEAT: float

    # Horizontal pad travel (raw units) that fires one Shift+Left/Right arrow
    # while the Select key is held. The pad x range is +/-0x8000 for a finger
    # on the pad; this step means roughly the full pad width selects ~16
    # characters per swipe — a small drag selects a few chars precisely, a
    # long sweep reaches across a word or line.
    SELECT_DRAG_STEP = 0x1000
    # Lift-off roll-back cancel window: every reverse arrow (a Shift+Left/
    # Right fired opposite to the drag's prevailing direction) is fired
    # IMMEDIATELY, 1:1, the instant its SELECT_DRAG_STEP of travel is seen —
    # there is no firing delay, so precise back-and-forth micro-adjustments
    # never get swallowed or one-sided. Instead we keep a short buffer of
    # just-fired reverse arrows, each tagged with the time they fired. If the
    # touch drops (select ends) while a reverse arrow is still sitting inside
    # this window, it is treated as the finger's natural lift-off roll-back
    # (which happens ~50-120ms before the finger actually leaves the pad) and
    # is cancelled by firing one compensating arrow in the opposite
    # direction, restoring the selection to exactly where the drag stopped.
    # Reverse arrows older than this window have already been on-screen long
    # enough to be a deliberate reversal and are left alone.
    SELECT_ROLLBACK_WINDOW = 0.12

    # Per-pad select-mode latch: which pad (LT/RT mask) currently owns the held
    # Select key, plus the raw pad-x anchor that horizontal drag is measured
    # from. Only the pad that pressed the Select key drives the selection.
    _select_pad = None
    _select_anchor_x = 0.0
    # Per-pad diacritic latch (Feature B): which pad (LT/RT mask) opened a
    # variant row via a held press. While set, the finger's x within the row
    # highlights a variant and releasing the press commits it. None = no
    # pad-driven variant row open.
    _diacritic_pad = None
    # Defer model (Feature B): per-pad {select-button mask: CoordFraction} of
    # a variant-capable letter pressed but NOT yet typed. Overridden with a
    # fresh dict by ControllerManager/__init__; this class default is only a
    # safety net for the standalone test harnesses.
    _deferred_base = {}
    # Minimum gap (s) between two "enter the key" edges on the same pad —
    # debounce/settle. A physical pad click can wobble: the force dips below
    # RELEASE and re-crosses ENGAGE within a few ms of the mechanical click's
    # vibration, which would otherwise fire a phantom SECOND insert. This
    # window swallows that re-engage. It stays well SHORTER than a genuine
    # human retype (~100 ms even for a hammering double-tap), so a fast retype
    # after a lift still fires.
    PAD_CLICK_SETTLE = 0.05

    def handle_pad_input(
        self,
        coord_frac,
        buttons,
        touch_button_mask,
        select_button_mask,
        click_button_mask=0,
        allow_click=True,
        now=0.0,
        trigger_pressed=False,
        trigger_prev=False,
        raw_x=0,
        real_touch=0,
    ):
        prev = self.sc_input_previous.buttons
        # Releasing the physical pad press rumbles too, so a click feels like a
        # full button: one tick pressing down, one coming back up. Checked
        # before the touch gate so it still fires if the finger lifts off at
        # the same instant the pad-click releases. Uses the stronger pad-click
        # haptic (deeper/more intense than the light UI tick).
        if (not (buttons & click_button_mask)) and (prev & click_button_mask):
            state.pad_click_haptic()
        repeat_key = int(select_button_mask)
        if self._select_pad == repeat_key:
            still_active = (allow_click and trigger_pressed) or bool(
                buttons & click_button_mask
            )
            # The click button (R1/L1) synthesizes TOUCH continuously while
            # held, so `buttons & touch_button_mask` does NOT reflect the real
            # finger. Use the caller's `real_touch` (actual pad contact) for
            # placement detection and for deciding when the finger is off.
            touch_now = bool(real_touch)
            touch_was = self._select_real_touch
            self._select_real_touch = touch_now
            if still_active and touch_now:
                # Finger is actually on the pad and holding: a FRESH placement
                # (touch just rising) must not fire arrows — the finger may
                # land anywhere to type, so re-anchor to where it is now. Only
                # subsequent horizontal travel from this spot drags.
                if not touch_was:
                    self._select_anchor_x = raw_x
                    self._select_dir = 0
                    self._select_base_dir = 0
                    self._select_reverse_buffer = []
            elif not still_active:
                # The press/click holding select was released (or finger lifted
                # while a pad press is required): end selection cleanly. Any
                # reverse arrow(s) within the roll-back window are cancelled.
                self._end_select(now)
                return state.InputState.INACTIVE
            elif not touch_now:
                # Still holding (e.g. the click button) but the finger lifted:
                # freeze selection — nothing fires, and re-anchoring happens on
                # the next fresh placement above.
                return state.InputState.INACTIVE
        if self._diacritic_pad == repeat_key:
            # The click/trigger holding this pad's diacritic press released
            # (or the finger lifted off a pad-press path) — commit the
            # highlighted variant now. Checked BEFORE the touch gate: on the
            # bumper (L1/R1) path controller.py drops the synthesized touch
            # the same frame the click releases, so with the finger already
            # lifted the gate below would return INACTIVE and the release —
            # and its commit — would never run (the row would stay latched,
            # swallowing every later press on this pad).
            if not (
                (allow_click and trigger_pressed)
                or bool(buttons & click_button_mask)
            ):
                self._end_diacritic_pad()
                return state.InputState.INACTIVE
            # Self-heal: the row is latched to this pad but the diacritic
            # session is gone (closed by another input / teardown) — drop the
            # latch so it can't swallow every later press on this pad.
            if not state.is_diacritic_open():
                self._diacritic_pad = None
                self._click_repeat_at.pop(repeat_key, None)
        elif repeat_key in self._deferred_base:
            # A deferred variant-capable press (the base was NOT typed at the
            # press edge) was released WITHOUT a row ever opening — a quick
            # tap. Type the base letter now. Also before the touch gate: the
            # release frame may have the finger already lifted (bumper path).
            #
            # Release is detected by EITHER the click/trigger being gone OR
            # the real finger being off the pad: on hardware the pad-force
            # click latch (_press_click hysteresis) can linger one frame past
            # the release, and that frame's touch gate would otherwise swallow
            # the release and lose the deferred base entirely.
            still_active = (allow_click and trigger_pressed) or bool(
                buttons & click_button_mask
            )
            finger_off = bool(prev & touch_button_mask) and not bool(
                buttons & touch_button_mask
            )
            if not still_active or finger_off:
                coord = self._deferred_base.pop(repeat_key)
                self._click_repeat_at.pop(repeat_key, None)
                # Deferred release: the press edge already clicked, so dispatch
                # the base SILENTLY (no second click sound).
                self.controller_state.click_queue.append(("deferred", coord))
                self._diag("defer release k={} queued base", repeat_key)
                return state.InputState.INACTIVE
        if not (buttons & touch_button_mask):
            return state.InputState.INACTIVE
        # Two ways to "enter" the key under the pointer while touching:
        #   • a physical pad press (pressing the trackpad down), which always
        #     selects regardless of the trigger role;
        #   • the trigger (L2/R2) — when allowed by its role — on the rising
        #     edge of the touch+trigger combo.
        # (The SC's triggers are disabled — see handle_input — so its pad
        # press is the sole "enter the key" action.)
        # Each fires a rumble once on the rising edge — that rumble IS the
        # simulated click. All "enter the key" paths use the stronger pad-click
        # haptic so the feedback matches. Fired here on the controller thread
        # for lowest latency (fires even off a key).
        # trigger_pressed/_prev are the analog-aware actuation (see
        # _osk_trigger_pressed) so the touchpad-click honors the lowered
        # "Trigger Actuation" setting too, not just the firmware full-pull bit.
        trigger_held = allow_click and trigger_pressed
        pad_clicked = bool(buttons & click_button_mask)
        touch_was = bool(prev & touch_button_mask)
        trigger_edge = trigger_held and (not touch_was or not trigger_prev)
        pad_edge = pad_clicked and not (prev & click_button_mask)
        click_active = trigger_held or pad_clicked
        # Hold-to-repeat, keyed per pad. First hit enters the key, rumbles, and
        # arms the repeat clock; held past BACKSPACE_HOLD_DELAY it re-enters the
        # key every BACKSPACE_REPEAT. Repeat hits are tagged so the main thread
        # only acts on them over Backspace (no rumble on repeat — matches X).
        repeat_key = int(select_button_mask)
        # Select mode (the on-screen "Select" key, iOS hold-space style): a pad
        # press landing on the Select key holds OS Shift while the press stays
        # engaged, and horizontal pad travel fires Shift+Left/Right — so holding
        # Select and dragging left/right selects text. Releasing the pad press
        # (or lifting the finger) ends selection and releases Shift.
        if self._select_pad == repeat_key:
            # This pad is driving the selection right now. Only drag while the
            # finger is actually on the pad (real_touch): with the click button
            # held the synthesized touch stays set, but an off-pad finger must
            # not fire arrows.
            if click_active and bool(real_touch):
                self._select_drag(raw_x, now)
                return state.InputState.CLICK
            # The press released — end selection. (State stays on this pad
            # until the press is gone so a force-wobble re-engage below the
            # settle window can't immediately re-grab it.)
            self._end_select(now)
            if not (buttons & touch_button_mask):
                return state.InputState.INACTIVE
            return state.InputState.HOVER
        # Diacritic hold-to-extend (Feature B): this pad's press opened a
        # variant row (see _try_open_diacritic). While the press stays engaged
        # the finger's position within the row highlights the variant; a
        # release commits it (the base already fired on the press edge).
        if self._diacritic_pad == repeat_key:
            if click_active:
                rect = state.get_diacritic_rect()
                if rect is not None:
                    px, py = coord_frac.to_absolute()
                    state.set_diacritic_index(
                        diacritics.variant_index_at_point(
                            rect, px, py, state.get_diacritic_variant_count()
                        )
                    )
                return state.InputState.CLICK
            self._end_diacritic_pad()
            return state.InputState.INACTIVE
        if trigger_edge or pad_edge:
            if self._select_pad is None and self._is_select_key(coord_frac):
                # Pad press on the Select key: enter select mode instead of a
                # normal key press. Hold Shift now so arrow taps select text.
                self._select_pad = repeat_key
                self._select_anchor_x = raw_x
                self._select_dir = 0
                self._select_base_dir = 0
                self._select_reverse_buffer = []
                self._select_real_touch = bool(real_touch)
                self._kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
                state.set_select_active(True)
                state.pad_click_haptic()
                return state.InputState.CLICK
            settle_until = self._click_settle_at.get(repeat_key, 0.0)
            if now > 0 and now < settle_until:
                # Force-wobble re-engage inside the settle window (a single
                # press's mechanical bounce crossing RELEASE then ENGAGE
                # again): swallow the phantom second insert, but re-arm the
                # hold-repeat clock — the pad is held again, so a long hold
                # must still rub out on the Backspace cadence. The DEFER must
                # still be re-stored: a real release (finger up) inside the
                # settle window must not lose the base letter.
                if self._should_defer_press(coord_frac):
                    self._deferred_base[repeat_key] = coord_frac
                self._click_repeat_at[repeat_key] = (
                    now + self.BACKSPACE_HOLD_DELAY
                )
            else:
                # Defer model (Feature B): a press landing on a variant-capable
                # letter does NOT type anything at the press edge. The hold
                # opens its variant row (see _try_open_diacritic); the release
                # then types either the picked variant or the base letter. A
                # QUICK tap just types the base on release. Non-variant keys
                # keep firing at the press edge exactly as before.
                if self._should_defer_press(coord_frac):
                    self._deferred_base[repeat_key] = coord_frac
                    self._diag(
                        "defer press k={} cf={!r}", repeat_key, coord_frac
                    )
                    # Click sound fires at the PRESS edge like any other key —
                    # the release commit types the char but must not re-tick,
                    # or a held variant pick would sound laggy.
                    state.key_sound_tick()
                else:
                    self.controller_state.click_queue.append(coord_frac)
                    self._diag(
                        "PRESS k={} immediate type cf={!r}",
                        repeat_key,
                        coord_frac,
                    )
                state.pad_click_haptic()
                self._click_repeat_at[repeat_key] = (
                    now + self.BACKSPACE_HOLD_DELAY
                )
                self._click_settle_at[repeat_key] = now + self.PAD_CLICK_SETTLE
        elif click_active and now >= self._click_repeat_at.get(
            repeat_key, float("inf")
        ):
            # Held past the repeat delay. Letters aren't repeatable, so before
            # queueing a (meaningless) repeat, try opening the key's variant
            # row instead (hold-to-extend, Feature B). The base already fired
            # at the press edge; the row turns the rest of the hold into a
            # variant pick that commits on release.
            if not self._try_open_diacritic(coord_frac, repeat_key):
                self.controller_state.click_queue.append(
                    ("repeat", coord_frac)
                )
            self._click_repeat_at[repeat_key] = now + self.BACKSPACE_REPEAT
        if not click_active:
            self._click_repeat_at.pop(repeat_key, None)
        if click_active:
            return state.InputState.CLICK
        return state.InputState.HOVER

    def _is_select_key(self, coord_frac):
        """True if the pointer coordinate sits on the on-screen Select key.
        Reuses the expanded hit-target so a press a few px over the Select
        key edge still enters select mode (matches the click resolution)."""
        kb = state.get_virtual_kb()
        if kb is None:
            return False
        x, y = coord_frac.to_absolute()
        key = kb.find_key_expanded(x, y)
        return key is not None and key.is_select

    def _diag(self, fmt, *args):
        """Mirror a pad-state diagnostic into dualtouch.log (gated by the tray
        logging toggle, via the applog write path). Lets us see whether the
        defer press/release fired on real hardware without guessing. Same
        applog path as uinput._diag, so if [uinput] lines appear then [pad]
        lines must too — their absence is a genuine control-flow clue, not a
        logging-config artifact. The format string is only applied when
        logging is enabled, so a press edge never pays for the message
        construction on a normal (logging-off) run."""
        try:
            from applog import is_logging_enabled, log_line

            if not is_logging_enabled():
                return
            msg = fmt.format(*args) if args else fmt
            log_line("pad", msg)
        except Exception:
            pass

    def _should_defer_press(self, coord_frac):
        """Defer model (Feature B): True if the press under the pointer should
        NOT be typed at the press edge — it's a letter key that has accented
        variants, so holding opens its variant row and the release picks base
        vs variant. A quick tap of such a key still types the base, on release.
        Non-variant keys (and the feature disabled) keep firing immediately at
        the press edge."""
        if not state.is_diacritics_enabled():
            return False
        kb = state.get_virtual_kb()
        if kb is None:
            return False
        x, y = coord_frac.to_absolute()
        key = kb.find_key_expanded(x, y)
        return (
            key is not None and vkb.diacritic_variants_for_key(key) is not None
        )

    def _try_open_diacritic(self, coord_frac, repeat_key):
        """Hold-to-extend (Feature B): the press on this pad has been held
        past BACKSPACE_HOLD_DELAY over a letter key that has variants — open
        its variant row instead of letting the hold fire a (meaningless) key
        repeat. Returns True if a row opened; the pad then watches the press
        for the release commit and the finger's x for the highlight."""
        if not state.is_diacritics_enabled() or state.is_diacritic_open():
            return False
        kb = state.get_virtual_kb()
        if kb is None:
            return False
        x, y = coord_frac.to_absolute()
        if not vkb.open_diacritic_at(kb, x, y, "pad"):
            return False
        self._diacritic_pad = repeat_key
        return True

    def _end_diacritic_pad(self):
        """End the held-to-extend session for this pad: the press released. In
        the DEFER model the base was never typed at the press edge, so a
        highlighted variant is committed as-is (no Backspace — the main thread
        types it via commit_diacritic), and a base selection (index -1) types
        the base letter by re-queueing the deferred press coordinate.

        The latch reset is UNCONDITIONAL (in finally): whatever branch or
        exception fires, this pad must never stay routed into row-handling —
        a stuck `_diacritic_pad` silently swallows every later press on it
        (nothing types), matching the intermittent wedge."""
        repeat_key = self._diacritic_pad
        try:
            self._deferred_base.pop(repeat_key, None)
            char = state.get_diacritic_selected_char()
            if char is not None:
                self.controller_state.click_queue.append(("variant", char))
                self._diag(
                    "end diacritic k={} variant U+{:04X}",
                    repeat_key,
                    ord(char[0]),
                )
            else:
                # No variant was highlighted. The user's model is "while the
                # row is open, only special letters are selectable" — so a
                # release with no explicit pick commits the FIRST variant,
                # never the base letter (the base would type the same letter
                # they already held, which reads as "nothing happened").
                variants = state.get_diacritic_variants_list()
                if variants:
                    self.controller_state.click_queue.append(
                        ("variant", variants[0])
                    )
                    self._diag(
                        "end diacritic k={} default v0 U+{:04X}",
                        repeat_key,
                        ord(variants[0][0]),
                    )
                state.close_diacritic()
        finally:
            self._diacritic_pad = None
            self._click_repeat_at.pop(repeat_key, None)

    def _fire_arrow(self, dirn):
        """Tap Shift+Left (dirn<0) or Shift+Right (dirn>0) once. Shift is
        already held for the duration of the select session."""
        key = sui.Keys.KEY_RIGHT if dirn > 0 else sui.Keys.KEY_LEFT
        self._kb.pressEvent([key])
        self._kb.releaseEvent([key])

    def _prune_reverse_buffer(self, now):
        """Drop buffered reverse-arrow timestamps that have already aged out
        of SELECT_ROLLBACK_WINDOW — they're deliberate travel, not roll-back,
        and are no longer eligible for lift-cancellation."""
        cutoff = now - self.SELECT_ROLLBACK_WINDOW
        buf = self._select_reverse_buffer
        while buf and buf[0][0] < cutoff:
            buf.pop(0)

    def _end_select(self, now):
        """Common teardown for ending a select-mode session (finger lifted,
        or the press/trigger holding it released). Cancels any reverse
        arrow(s) still sitting inside the lift-off roll-back window — each
        one gets a single compensating arrow in the opposite direction,
        which exactly restores the selection to where it was before that
        roll-back travel — then releases Shift and clears session state."""
        self._prune_reverse_buffer(now)
        for _, dirn in self._select_reverse_buffer:
            # Undo: a buffered arrow fired in direction `dirn`, so the
            # compensating tap is the opposite direction.
            self._fire_arrow(-dirn)
        self._select_reverse_buffer = []
        self._select_pad = None
        self._select_dir = 0
        self._select_base_dir = 0
        self._select_real_touch = False
        self._kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
        state.set_select_active(False)

    def _select_drag(self, raw_x, now):
        """Relative-position model for horizontal select drag. While the
        Select key is held, map horizontal pad travel to Shift+Left/Right
        arrow taps: one arrow for every SELECT_DRAG_STEP of net travel from
        the anchor, moving the anchor with the finger.

        Every arrow — forward or reverse — fires IMMEDIATELY, 1:1, with no
        firing delay, so rapid back-and-forth micro-adjustments each land
        their own arrow the instant their step of travel is seen (no
        swallowed travel, no one-sided creep). Reverse arrows (direction
        opposite the drag's prevailing direction) are additionally recorded
        in a short timestamped buffer; if the touch drops while one is still
        inside SELECT_ROLLBACK_WINDOW, _end_select cancels it as lift-off
        roll-back. That is the only place travel ever gets undone — this
        method never withholds or rolls back a fire itself."""
        self._prune_reverse_buffer(now)
        step = self.SELECT_DRAG_STEP
        while abs(raw_x - self._select_anchor_x) >= step:
            delta = raw_x - self._select_anchor_x
            dirn = 1 if delta > 0 else -1
            if self._select_base_dir == 0:
                self._select_base_dir = dirn
            is_reverse = dirn != self._select_base_dir

            self._fire_arrow(dirn)
            self._select_anchor_x += step * dirn

            if is_reverse:
                self._select_reverse_buffer.append((now, dirn))
            # A same-direction (forward) arrow needs no bookkeeping: it can
            # never be lift-off roll-back (roll-back is, by definition, a
            # reversal), so it's never buffered and never cancelled.

    def _press_click(self, press, held):
        """Hysteresis latch: a press counts as "clicked" once force reaches
        ENGAGE, stays clicked while force sits between the thresholds, and
        releases only when force falls below RELEASE."""
        if press >= self._pad_click_engage:
            return True
        if press <= self._pad_click_release:
            return False
        return held

    def _lock_key_center(self, prev_target, prev_coords, prev_ptr):
        """The key-center CoordFraction under the pointer (the currently
        selected key — the one under the rendered cursor, or under the finger
        if none has rendered yet), or None if the pointer isn't on a key.
        The shared target of the pad-press lock and the click-button lock
        below: freezing the cursor on the center makes the resulting click
        deterministic (physical presses and button pulls shift the finger a
        little otherwise)."""
        if prev_target is not None:
            return prev_target
        kb = state.get_virtual_kb()
        if kb is None:
            return None
        # Prefer the CURRENT pointer coords unless they match the last
        # RENDERED pointer exactly: the stored prev_ptr lags the finger by a
        # frame (the lock resolves BEFORE the pointer is updated for this
        # frame), so on a fast first press it can still sit on the seeded
        # default position and freeze the row/click onto the WRONG key. Only
        # when the finger is exactly where it was rendered does the rendered
        # coord win.
        if (
            prev_ptr is not None
            and prev_ptr.coord_frac.to_absolute() == prev_coords.to_absolute()
        ):
            x, y = prev_ptr.coord_frac.to_absolute()
        else:
            x, y = prev_coords.to_absolute()
        rc = kb.find_key_expanded_rc(x, y)
        if rc is None:
            return None
        layout = kb.get_key_layout(*rc)
        return CoordFraction.from_absolute(
            int(layout.x + layout.w // 2), int(layout.y + layout.h // 2)
        )

    def _pad_lock_target(
        self, press, touched, prev_target, prev_coords, prev_ptr
    ):
        """Press-aware cursor lock (trackpad-click path only — inert while
        sc_pad_click_enter is OFF, since a disabled pad click must not freeze
        the cursor either). A pad press crossing the sc_pad_press_hold setting
        freezes the cursor on the center of the currently selected key. It
        stays frozen through the click (sc_pad_click_engage fires on that
        spot) and releases only when the press falls below
        sc_pad_click_release. Returns the key-center CoordFraction while
        locked, else None (normal finger tracking)."""
        if not state.get_sc_pad_click_enter():
            return None
        if not touched or press < self._pad_click_release:
            return None
        if prev_target is not None:
            return prev_target
        if press < self._pad_press_hold:
            return None
        return self._lock_key_center(prev_target, prev_coords, prev_ptr)

    def _button_lock_target(self, pressed, prev_target, prev_coords, prev_ptr):
        """Click-button version of the pad-press lock: while the click button
        is held — L1/R1 on its press, L2/R2 on its analog pull crossing
        sc_trigger_focus_pull (half pull by default; the click itself still
        fires on the full-pull digital bit) — the pointer freezes on the key
        center so the click lands on the key that was under the pointer when
        the lock engaged, even with the thumb off the pad."""
        if not pressed:
            return None
        return self._lock_key_center(prev_target, prev_coords, prev_ptr)
