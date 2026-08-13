"""Input source for the on-screen keyboard.

The custom Steam Controller hidapi driver (steamcontroller/) is the single
backend — the SDL/gamepad subsystem was removed. SteamHidSource makes it
pollable; InputMerger wraps it and presents the `sc`-facade (set_lizard / the
two haptic ticks / addExit / close) that ControllerManager.handle_input expects
from a SteamController, with `sc_input` a real SteamControllerInput frame.
"""

import threading
import time
from contextlib import suppress
from threading import Lock, Thread

from steamcontroller import (
    SCI_NULL,
    SCButtons,
    SCStatus,
    SteamController,
    SteamControllerInput,
)

from triton import state

# Reconnect cadence for a dropped/absent Steam Controller (mirrors the old
# input_thread loop).
_RECONNECT_DELAY = 0.5


# Stick magnitude (of 32767) above which a frame counts as "actively in use".
# Comfortably above resting drift (~3000) but below an intentional push.
_ACTIVITY_STICK = 8000


def _frame_has_activity(f):
    """True if this input frame shows the controller is actively being used
    (any button/trigger, or a stick pushed past the deadzone). Used to decide
    which controller 'owns' the current interaction so haptics go only to it."""
    if f.buttons:
        return True
    return (
        abs(f.lstick_x) > _ACTIVITY_STICK
        or abs(f.lstick_y) > _ACTIVITY_STICK
        or abs(f.rstick_x) > _ACTIVITY_STICK
        or abs(f.rstick_y) > _ACTIVITY_STICK
    )


def merge_inputs(a, b):
    """OR-merge two SteamControllerInput frames into one. Buttons OR together,
    triggers take the max, each stick takes the larger-magnitude source, and a
    trackpad's coordinates come from whichever source is actively touching it
    (`a` wins ties — pass the Steam Controller as `a` so its tuned pads lead)."""
    buttons = a.buttons | b.buttons
    ltrig = a.ltrig if a.ltrig >= b.ltrig else b.ltrig
    rtrig = a.rtrig if a.rtrig >= b.rtrig else b.rtrig
    lpad_press = a.lpad_press if a.lpad_press >= b.lpad_press else b.lpad_press
    rpad_press = a.rpad_press if a.rpad_press >= b.rpad_press else b.rpad_press
    lstick_x = a.lstick_x if abs(a.lstick_x) >= abs(b.lstick_x) else b.lstick_x
    lstick_y = a.lstick_y if abs(a.lstick_y) >= abs(b.lstick_y) else b.lstick_y
    rstick_x = a.rstick_x if abs(a.rstick_x) >= abs(b.rstick_x) else b.rstick_x
    rstick_y = a.rstick_y if abs(a.rstick_y) >= abs(b.rstick_y) else b.rstick_y
    if (b.buttons & SCButtons.LPADTOUCH) and not (
        a.buttons & SCButtons.LPADTOUCH
    ):
        lpad_x, lpad_y = b.lpad_x, b.lpad_y
    else:
        lpad_x, lpad_y = a.lpad_x, a.lpad_y
    if (b.buttons & SCButtons.RPADTOUCH) and not (
        a.buttons & SCButtons.RPADTOUCH
    ):
        rpad_x, rpad_y = b.rpad_x, b.rpad_y
    else:
        rpad_x, rpad_y = a.rpad_x, a.rpad_y
    return SteamControllerInput(
        status=SCStatus.INPUT,
        seq=a.seq,
        buttons=buttons,
        ltrig=ltrig,
        rtrig=rtrig,
        lpad_x=lpad_x,
        lpad_y=lpad_y,
        rpad_x=rpad_x,
        rpad_y=rpad_y,
        lstick_x=lstick_x,
        lstick_y=lstick_y,
        rstick_x=rstick_x,
        rstick_y=rstick_y,
        lpad_press=lpad_press,
        rpad_press=rpad_press,
    )


class SteamHidSource:
    """The custom Steam Controller hidapi driver, made pollable. Runs
    SteamController.run() on its own thread (with reconnect), stashing the
    latest input frame for the merge loop to read. Haptics/lizard forward to
    the live device; teardown lets run()'s cleanup restore lizard mode."""

    # No input frame for this long => treat the device as released/gone.
    STALE_AFTER = 1.0

    def __init__(self):
        self._lock = Lock()
        self._sc = None
        self._latest = SCI_NULL
        self._latest_t = 0.0
        self._exit = False
        self._stop_event = threading.Event()
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _on_frame(self, sc, sci):
        # Runs on the SteamController read thread, once per HID input frame.
        with self._lock:
            self._latest = sci
            self._latest_t = time.monotonic()

    def _run_loop(self):
        while not self._exit and not state.should_close():
            # Skip the firmware lizard-restore on close while Steam runs —
            # Steam Input owns the lizard state and re-asserts it (a ~1s
            # "lizard mode" blip after closing the keyboard otherwise).
            sc = SteamController(
                callback=self._on_frame,
                restore_lizard_on_close=not state.is_steam_running(),
            )
            with self._lock:
                self._sc = sc
            try:
                sc.run()  # blocks until the device drops or addExit() fires
            except Exception as e:
                print(f"SteamHidSource: run error: {e!r}")
            finally:
                with self._lock:
                    self._sc = None
                    self._latest = SCI_NULL
            if self._exit or state.should_close():
                break
            self._stop_event.wait(_RECONNECT_DELAY)

    def poll(self):
        with self._lock:
            if self._sc is None:
                return None
            if time.monotonic() - self._latest_t > self.STALE_AFTER:
                return None
            return self._latest

    def _live(self):
        with self._lock:
            return self._sc

    def set_lizard(self, enabled):
        sc = self._live()
        if sc is not None:
            sc.set_lizard(enabled)

    def haptic_click(self):
        sc = self._live()
        if sc is not None:
            sc.haptic_click()

    def haptic_pad_click(self):
        sc = self._live()
        if sc is not None:
            sc.haptic_pad_click()

    def addExit(self):
        sc = self._live()
        if sc is not None:
            sc.addExit()

    def close(self):
        self._exit = True
        self._stop_event.set()  # wake the reconnect sleep immediately
        self.addExit()
        self._thread.join(timeout=1.0)


class InputMerger:
    """Holds the active input sources, OR-merges their frames, and presents the
    `sc`-facade (set_lizard / haptic_click / haptic_pad_click / addExit) that
    ControllerManager.handle_input expects, fanning each call out to every
    source."""

    # A source stays the haptic target this long after it last showed activity
    # (covers the gap between an input and the key dispatch that fires the tick).
    _ACTIVE_WINDOW = 1.0

    def __init__(self):
        self._sources = []
        # The source whose controller is actively driving the OSK, and when it
        # last showed activity. Haptics route ONLY here so a key press buzzes
        # just the controller being used — not every connected controller.
        self._active_src = None
        self._active_t = 0.0

    def add(self, src):
        self._sources.append(src)

    def poll(self):
        merged = None
        now = time.monotonic()
        for src in self._sources:
            try:
                f = src.poll()
            except Exception as e:
                print(f"InputMerger: source poll error: {e!r}")
                f = None
            if f is None:
                continue
            if _frame_has_activity(f):
                self._active_src = src
                self._active_t = now
            merged = f if merged is None else merge_inputs(merged, f)
        return merged

    def _active_source(self):
        """The source actively in use, or None if nothing has been touched
        recently (callers then fall back to fanning out to all sources)."""
        if (
            self._active_src is not None
            and (time.monotonic() - self._active_t) <= self._ACTIVE_WINDOW
        ):
            return self._active_src
        return None

    def set_lizard(self, enabled):
        for src in self._sources:
            with suppress(Exception):
                src.set_lizard(enabled)

    def _fan_haptic(self, method):
        # Route the tick to the controller actually being used; if none is
        # clearly active (rare — e.g. the one-shot open tick), fall back to all.
        active = self._active_source()
        targets = [active] if active is not None else self._sources
        for src in targets:
            with suppress(Exception):
                getattr(src, method)()

    def haptic_click(self):
        self._fan_haptic("haptic_click")

    def haptic_pad_click(self):
        self._fan_haptic("haptic_pad_click")

    def addExit(self):
        for src in self._sources:
            with suppress(Exception):
                src.addExit()

    def close(self):
        for src in self._sources:
            with suppress(Exception):
                src.close()
