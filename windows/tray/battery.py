"""Battery status and USB device-watch mixin."""

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from steamcontroller import SteamController
from triton import state as triton_state


class _BatteryMixin:
    # Attributes provided by the composed tray App (declared here so static
    # tooling knows the mixin's contract — see tray/app.py __init__).
    _stop_event: threading.Event
    _current_sc: SteamController | None
    _icon_ref: Any
    _notify: Callable[[str, str], None]
    _refresh_menu: Callable[[], None]

    # battery status --------------------------------------------------------

    # Discharge bands that trigger a low-battery toast (and a haptic nudge),
    # ascending so `next(b for b in bands if pct <= b)` picks the tightest
    # (most severe) band the pack is under. Each band warns once; dropping to a
    # more-severe (lower) band warns again.
    _LOW_BATT_BANDS = (5, 10, 20, 30)
    # Recovery hysteresis: clear the low-battery latch only once the pack climbs
    # back above this (above the highest band), so a reading hovering at a
    # threshold doesn't re-warn.
    _LOW_BATT_RECOVER = 35
    # How often to poll the live controller's cached battery reading. Short so
    # plug-in / unplug feedback is prompt; the poll itself is a single attribute
    # read and we only touch the UI when the reading actually changes.
    _BATTERY_POLL_SECONDS = 5.0
    # Drop the battery display after the controller has been gone this long, so
    # a USB-C unplug doesn't leave a stale "(charging)" line in the menu. Longer
    # than a normal sc rebuild (gamepad-mode toggle / brief drop) so those don't
    # blink the line off and back on.
    _BATTERY_STALE_SECONDS = 8.0

    def is_battery_known(self, item):
        """Visibility callback for the battery menu line — hidden until the
        controller has actually reported a level."""
        return self._battery is not None

    def battery_menu_label(self, item):
        return self._battery_label or "Steam Controller: …"

    def _await_battery_pct(self, timeout=8.0):
        """Wait up to `timeout`s for a live battery reading (e.g. just after a
        USB-C connect, before battery_thread's slower poll has it), reading the
        live controller directly. Returns the percent, or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop_event.is_set():
            sc = self._current_sc
            b = sc.get_battery() if sc is not None else None
            if b is not None:
                return b.percent
            self._stop_event.wait(0.5)
        return None

    def _update_battery_ui(self, batt):
        """Refresh the tray tooltip + menu line from a battery reading."""
        pct = batt.percent
        if batt.charge_complete:
            state = f"{pct}% (charged)"
        elif batt.charging:
            state = f"{pct}% (charging)"
        else:
            state = f"{pct}%"
        self._battery_label = f"Steam Controller: {state}"
        icon = self._icon_ref
        if icon is not None:
            with suppress(Exception):
                icon.title = f"DualTouch — Steam Controller {state}"
        self._refresh_menu()

    def _battery_notifications(self, batt):
        """Fire battery toasts: charging started, fully charged, and low-battery
        threshold crossings."""
        # Charging started. If the controller is tethered via USB-C the device
        # watcher already toasts "connected … charging" (a USB presence event),
        # so only announce here for the wireless puck dock — docking on the puck
        # causes no USB presence change, so this is the only signal for it.
        # charge_complete has its own toast below; the reverse (off-charger)
        # edge gets the "unplugged" toast.
        charging = batt.charging
        if charging and not self._was_charging and not batt.charge_complete:
            from steamcontroller import PRODUCT_ID_WIRED, present_product_ids

            if PRODUCT_ID_WIRED not in present_product_ids():
                self._notify(
                    "Steam Controller charging",
                    f"On the puck — {batt.percent}%.",
                )
        elif not charging and self._was_charging:
            self._notify(
                "Steam Controller unplugged",
                f"Off the puck — {batt.percent}% on battery.",
            )
        self._was_charging = charging

        # Fully charged: notify once per charge completion.
        if batt.charge_complete:
            if not self._charge_complete_notified:
                self._charge_complete_notified = True
                self._notify(
                    "Steam Controller fully charged",
                    "Steam Controller battery is full.",
                )
        else:
            self._charge_complete_notified = False

        pct = batt.percent
        # On the charger (or comfortably recovered) → arm the low-battery
        # warning again for the next discharge cycle.
        if batt.charging or pct > self._LOW_BATT_RECOVER:
            self._low_warned_at = None
        if batt.charging:
            return

        band = next((b for b in self._LOW_BATT_BANDS if pct <= b), None)
        if band is None:
            return
        # Warn on the first low band hit, and again each time we drop to a
        # more-severe (lower) band — but not repeatedly within the same band.
        if self._low_warned_at is not None and band >= self._low_warned_at:
            return
        self._low_warned_at = band
        if band <= 5:
            self._notify(
                "Steam Controller battery critical",
                f"{pct}% left — charge the controller now.",
            )
        elif band <= 10:
            self._notify(
                "Steam Controller battery low", f"{pct}% left — charge soon."
            )
        else:
            self._notify(
                "Steam Controller battery getting low", f"{pct}% remaining."
            )
        # A short haptic nudge so it's noticeable mid-game (haptics switch
        # permitting, and only if the device is still live).
        sc = self._current_sc
        if sc is not None and triton_state.is_rumble_enabled():
            with suppress(Exception):
                sc.haptic_click()

    def battery_thread(self):
        """Poll the live controller's cached battery reading and drive the
        tray tooltip/menu plus low-battery / charged notifications. The reading
        itself is captured for free on the SteamController read loop; this
        thread just samples it on a slow timer (battery changes slowly) so the
        gaming hot path stays untouched."""
        last_key = None
        last_seen = None
        while not self._stop_event.is_set():
            sc = self._current_sc
            batt = sc.get_battery() if sc is not None else None
            now = time.monotonic()
            if batt is not None:
                last_seen = now
                self._battery = batt
                # Only touch the UI / re-evaluate notifications when the
                # reading actually changes, so a tight poll doesn't churn the
                # menu or re-toast.
                key = (batt.percent, batt.charging, batt.charge_complete)
                if key != last_key:
                    last_key = key
                    self._update_battery_ui(batt)
                    self._battery_notifications(batt)
            elif self._battery is not None and (
                sc is not None
                or last_seen is None
                or now - last_seen > self._BATTERY_STALE_SECONDS
            ):
                # Drop the now-stale reading. `sc is not None` = the controller
                # link is up but it reported no battery (powered off via Steam+Y
                # or dropped its wireless link while the dongle stays plugged) —
                # clear promptly. Otherwise (sc None: a brief rebuild or a full
                # unplug) wait the grace window so a gamepad-mode rebuild doesn't
                # blink the line off and back on. Reset the latches so a
                # reconnect is treated as a fresh charge cycle.
                self._battery = None
                self._battery_label = None
                last_key = None
                self._was_charging = False
                self._low_warned_at = None
                self._charge_complete_notified = False
                icon = self._icon_ref
                if icon is not None:
                    with suppress(Exception):
                        icon.title = "DualTouch"
                self._refresh_menu()
            self._stop_event.wait(self._BATTERY_POLL_SECONDS)

    # How often to poll USB for the receiver / wired controller appearing.
    _DEVICE_POLL_SECONDS = 3.0

    def device_watch_thread(self):
        """Toast when the USB-C wired controller (PID 0x1302) is plugged into /
        unplugged from the PC. (The wireless receiver/puck's own USB presence
        isn't announced.) Independent of the battery poll — only enumerates HID,
        so it fires even when nothing is paired."""
        from steamcontroller import PRODUCT_ID_WIRED, present_product_ids

        wired_was = None
        while not self._stop_event.is_set():
            wired = PRODUCT_ID_WIRED in present_product_ids()
            # First loop just seeds state so we don't toast what's already
            # plugged in at startup.
            if wired_was is not None and wired != wired_was:
                if wired:
                    pct = self._await_battery_pct()
                    extra = f" — {pct}%" if pct is not None else ""
                    self._notify(
                        "Steam Controller connected",
                        f"Plugged in via USB-C — charging{extra}.",
                    )
                else:
                    self._notify(
                        "Steam Controller disconnected",
                        "USB-C cable unplugged.",
                    )
            wired_was = wired
            self._stop_event.wait(self._DEVICE_POLL_SECONDS)
