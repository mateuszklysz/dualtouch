import steamcontroller.uinput as sui

from triton import state


class _StickMixin:
    # Attributes provided by the composed ControllerManager (declared here so
    # static tooling knows the mixin's contract — see controller.py).
    _kb: sui.Keyboard
    STICK_DEADZONE: int
    STICK_HOLD_DELAY: float
    STICK_VOL_REPEAT: float
    KBD_STICK_HOLD_DELAY: float
    KBD_STICK_REPEAT: float
    KBD_STICK_DEADZONE: int
    KBD_SCROLL_HOLD_DELAY: float
    KBD_SCROLL_REPEAT: float
    _SCROLL_ARROW_KEYS: dict

    # Steam + left-stick media zone -> keycode map (built once; a fresh dict
    # per input frame is pure waste).
    _MEDIA_ZONE_KEYS = {
        "UP": sui.Keys.KEY_VOLUMEUP,
        "DOWN": sui.Keys.KEY_VOLUMEDOWN,
        "LEFT": sui.Keys.KEY_PREVIOUSSONG,
        "RIGHT": sui.Keys.KEY_NEXTSONG,
    }
    # Cached "Sticks Control Keyboard" (sc_left_stick_nav): the tray publishes
    # it ONCE at startup and never republishes while the process runs, so it is
    # static per OSK session. ControllerManager.__init__ overrides this with the
    # live value; the class default only covers standalone mixin harnesses.
    _stick_nav_enabled = True

    def _handle_media_stick(self, sc_input, steam_now, now):
        """Steam + left stick → media transport. Up/Down = volume (repeats
        while held); Left/Right = previous/next track (one per deflection).
        Edge-triggered: the stick must return toward center before the same
        direction fires again."""
        x = sc_input.lstick_x
        y = sc_input.lstick_y  # positive = up (same hardware sign as the pads)

        zone = "NEUTRAL"
        if steam_now and (
            abs(x) > self.STICK_DEADZONE or abs(y) > self.STICK_DEADZONE
        ):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        key = self._MEDIA_ZONE_KEYS.get(zone)

        fire = False
        is_edge = False
        if zone != self._stick_zone_prev:
            # Entering a new non-neutral zone always fires once (the "tap").
            # Then wait STICK_HOLD_DELAY before any rapid repeat begins, so a
            # quick tap (or a sub-second hold) is exactly one step.
            fire = zone != "NEUTRAL"
            is_edge = fire
            self._stick_repeat_at = now + self.STICK_HOLD_DELAY
        elif zone in ("UP", "DOWN") and now >= self._stick_repeat_at:
            # Held past the delay: volume ramps fast. Track skip never repeats.
            fire = True
            self._stick_repeat_at = now + self.STICK_VOL_REPEAT
        self._stick_zone_prev = zone

        if fire and key is not None:
            self._kb.pressEvent([key])
            self._kb.releaseEvent([key])
            # Mark the Steam press as "used" so releasing it doesn't close the OSK.
            self._saw_x_during_steam = True
            # Haptic tick on a volume TAP only (one 2% step) — not the rapid
            # hold-ramp, and not track skip (left/right).
            if is_edge and zone in ("UP", "DOWN"):
                state.haptic_tick()

    def _handle_kbd_stick(self, sc_input, steam_now, now):
        """Left stick → move the on-screen-keyboard cursor (one key per
        deflection; auto-repeats while held). Only active when Steam is NOT
        held, since Steam + left stick is the media chord above. The actual
        cursor move — and its key-switch haptic — happens in the main loop
        via step_cursor, so this just posts DPAD direction events."""
        # With "Sticks Control Keyboard" turned off (sc_left_stick_nav
        # settings.json key), the left stick scrolls the window behind the OSK
        # instead of moving the key cursor — so you can scroll a page while the
        # OSK is open (firmware lizard is OFF while the OSK owns the controller,
        # so the app injects the scroll itself).
        if not self._stick_nav_enabled:
            self._kbd_stick_zone_prev = "NEUTRAL"
            self._handle_kbd_stick_scroll(sc_input, steam_now, now)
            return
        # Not scrolling: clear the scroll zone so toggling the setting mid-hold
        # re-fires an initial tap instead of treating the deflection as ongoing.
        self._kbd_scroll_zone_prev = "NEUTRAL"
        x = sc_input.lstick_x
        y = sc_input.lstick_y  # positive = up

        zone = "NEUTRAL"
        if not steam_now and (
            abs(x) > self.KBD_STICK_DEADZONE
            or abs(y) > self.KBD_STICK_DEADZONE
        ):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"

        fire = False
        if zone != self._kbd_stick_zone_prev:
            # Entering a new direction always steps once (the "tap"), then
            # waits KBD_STICK_HOLD_DELAY before the held auto-repeat begins.
            fire = zone != "NEUTRAL"
            self._kbd_stick_repeat_at = now + self.KBD_STICK_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._kbd_stick_repeat_at:
            fire = True
            self._kbd_stick_repeat_at = now + self.KBD_STICK_REPEAT
        self._kbd_stick_zone_prev = zone

        if fire:
            state.queue_dpad(zone, haptic=True)

    def _handle_kbd_stick_scroll(self, sc_input, steam_now, now):
        """Left stick → scroll the window behind the OSK (used when "Sticks
        Control Keyboard" is off). Sends ARROW-KEY taps on the dominant axis —
        one on entering a direction, then auto-repeating while held — with the
        exact deadzone / hold delay / repeat cadence the tray _Watcher uses for
        desktop arrow-stick scrolling, so the speed matches the OSK-closed scroll.
        The taps land on the focused window behind the no-focus OSK."""
        x = sc_input.lstick_x
        y = sc_input.lstick_y  # positive = up
        dz = self.STICK_DEADZONE
        zone = "NEUTRAL"
        if not steam_now and (abs(x) > dz or abs(y) > dz):
            if abs(y) >= abs(x):
                zone = "UP" if y > 0 else "DOWN"
            else:
                zone = "RIGHT" if x > 0 else "LEFT"
        fire = False
        if zone != self._kbd_scroll_zone_prev:
            # New direction (or release): the press fires immediately, then we
            # wait KBD_SCROLL_HOLD_DELAY before the first repeat.
            fire = zone != "NEUTRAL"
            self._kbd_scroll_at = now + self.KBD_SCROLL_HOLD_DELAY
        elif zone != "NEUTRAL" and now >= self._kbd_scroll_at:
            fire = True
            self._kbd_scroll_at = now + self.KBD_SCROLL_REPEAT
        self._kbd_scroll_zone_prev = zone
        key = self._SCROLL_ARROW_KEYS.get(zone)
        if fire and key is not None:
            self._kb.pressEvent([key])
            self._kb.releaseEvent([key])
