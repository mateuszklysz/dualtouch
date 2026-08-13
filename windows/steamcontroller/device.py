import threading
import time
from contextlib import suppress

import hid

from .constants import (
    BATTERY_FRESH_SECONDS,
    HAPTIC_CLICK_GAIN,
    HAPTIC_PAD_CLICK_FREQ,
    HAPTIC_PAD_CLICK_GAIN,
    HAPTIC_PAD_LEFT,
    HAPTIC_PAD_RIGHT,
    HAPTIC_RUMBLE_LEFT,
    HAPTIC_RUMBLE_RIGHT,
    LIZARD_REFRESH_SECONDS,
    OPEN_RETRY_ATTEMPTS,
    OPEN_RETRY_DELAY,
    PRODUCT_ID_PROTEUS,
    PRODUCT_ID_WIRED,
    TRITON_BATTERY_REPORT_ID,
    TRITON_INPUT_MIN_LEN,
    TRITON_INPUT_REPORT_IDS,
    TRITON_WIRELESS_STATUS_IDS,
    VENDOR_ID,
)
from .parse import _parse_battery, _parse_triton
from .reports import (
    DISABLE_LIZARD_REPORT,
    ENABLE_LIZARD_REPORT,
    _build_haptic_stop_report,
    _build_haptic_tone_report,
)

# Remembered across SteamController instances: the interface path that last
# returned input reports. Tried first on the next open so a rebuild skips the
# dongle's silent slots and comes live in milliseconds instead of probing each
# slot for up to 1.5s.
_LAST_GOOD_PATH = None


def _enumerate_data_interfaces():
    """Vendor-specific HID interfaces (usage page 0xFF00, usage 1) for both
    the wireless dongle (PID 0x1304) and the wired controller (PID 0x1302).
    The dongle typically exposes 4 interfaces (one per paired controller)."""
    out = []
    for pid in (PRODUCT_ID_PROTEUS, PRODUCT_ID_WIRED):
        for d in hid.enumerate(VENDOR_ID, pid):
            if d.get("usage_page") == 0xFF00 and d.get("usage") == 1:
                out.append(d)
    out.sort(
        key=lambda d: (d.get("product_id", 0), d.get("interface_number", 0))
    )
    return out


def present_product_ids():
    """Set of Steam Controller product IDs (e.g. PRODUCT_ID_PROTEUS for the
    wireless receiver/puck, PRODUCT_ID_WIRED for a USB-C-tethered controller)
    currently enumerable on USB. Cheap presence probe — lists HID, never opens
    a handle — so it works whether or not we (or Steam) hold the device, and
    even when nothing is paired/connected. Used by the tray's device watcher."""
    pids = set()
    try:
        for d in hid.enumerate(VENDOR_ID, 0):
            pid = d.get("product_id")
            if pid:
                pids.add(pid)
    except Exception:
        pass
    return pids


class SteamController:
    """API-compatible with triton's expectations:
    SteamController(callback, callback_args=None)
    sc.run()
    sc.addExit()
    """

    def __init__(
        self,
        callback,
        callback_args=None,
        passive=False,
        exclusive=False,
        keep_open=False,
        restore_lizard_on_close=True,
    ):
        self._cb = callback
        self._cb_args = callback_args if callback_args is not None else ()
        self._passive = passive
        # When True, run() keeps the HID handle open when its loop exits on a
        # normal trigger (addExit), so Steam Input never sees the controller
        # detach/re-enumerate between keyboard sessions. The handle is then
        # released by close(), or on a read error / device drop.
        self._keep_open = keep_open
        # Whether teardown re-enables firmware lizard mode. Off when Steam is
        # running (the tray's persistent SC and the OSK's SteamHidSource set
        # it): Steam Input owns the lizard state, and re-enabling firmware
        # lizard on close makes Steam re-assert it — a ~1s blip after closing
        # the keyboard. Standalone use keeps the restore so the controller
        # works as a plain mouse/keyboard right away.
        self._restore_lizard_on_close = restore_lizard_on_close
        # When True, open the controller with no sharing so other apps (Steam)
        # can't grab it. Falls back to shared if exclusive open is denied.
        self._exclusive = exclusive
        self._dev = None
        self._dev_lock = threading.Lock()
        self._exit = threading.Event()
        self._lizard_thread = None
        # True once this instance has successfully opened a controller. Lets the
        # launcher tell "device absent" (open failed) from "ran then was kicked"
        # so it can back off reconnect attempts only when nothing is there.
        self.opened = False
        # In non-passive mode, the lizard state we want the watchdog to keep
        # re-asserting. Defaults to off; set_lizard() flips it on the fly.
        self._lizard_enabled = False
        # Last battery status seen on the wire (a SteamControllerBattery, or
        # None until the controller streams its first 0x43 power report). Set on
        # the read thread, read via get_battery(); a single attribute
        # read/write of an immutable tuple is atomic under the GIL, so no lock.
        self._battery = None
        # time.monotonic() of the last input/battery frame, for get_battery()'s
        # freshness check (see BATTERY_FRESH_SECONDS).
        self._last_frame_t = 0.0

    def _open_device(self, path):
        """Open `path`. In exclusive mode, try a no-sharing open (blocks Steam)
        and fall back to normal shared hidapi if that's denied — e.g. because
        Steam already holds the device — so the controller still works.

        Both opens are retried (see OPEN_RETRY_ATTEMPTS): a block toggle reopens
        in the other mode right after closing our own handle, which can briefly
        race the OS releasing it in either direction. Retries fire only on a
        failed open, so normal opens and the probe loop are unaffected."""
        last_err = None
        if self._exclusive:
            for attempt in range(OPEN_RETRY_ATTEMPTS):
                try:
                    from . import winhid

                    dev = winhid.ExclusiveHidDevice()
                    dev.open_path(path)
                    print("steamcontroller: opened EXCLUSIVE (Steam blocked)")
                    return dev
                except Exception as e:
                    # A sharing violation right after our own close clears once
                    # the OS releases the device; a genuine conflict (Steam holds
                    # it) won't, so cap the retries and then fall back to shared.
                    last_err = e
                    if attempt + 1 < OPEN_RETRY_ATTEMPTS:
                        time.sleep(OPEN_RETRY_DELAY)
            print(
                f"steamcontroller: exclusive open denied ({last_err}); "
                "falling back to shared"
            )
        # Shared hidapi open — retried for the same race when a block is toggled
        # OFF and we reopen shared right after releasing the exclusive handle.
        for attempt in range(OPEN_RETRY_ATTEMPTS):
            try:
                dev = hid.device()
                dev.open_path(path)
                return dev
            except Exception as e:
                last_err = e
                if attempt + 1 < OPEN_RETRY_ATTEMPTS:
                    time.sleep(OPEN_RETRY_DELAY)
        raise (
            last_err
            if last_err is not None
            else OSError(f"could not open {path}")
        )

    def _open_first_responsive(self):
        global _LAST_GOOD_PATH
        candidates = _enumerate_data_interfaces()
        if not candidates:
            raise RuntimeError(
                "No Steam Controller 2026 interface found "
                f"(VID 0x{VENDOR_ID:04X}, "
                f"PID 0x{PRODUCT_ID_PROTEUS:04X} dongle / "
                f"0x{PRODUCT_ID_WIRED:04X} wired)."
            )

        # Try the last-known-good interface first. Stable sort: the matching
        # path (key False/0) moves to the front, everything else keeps order.
        if _LAST_GOOD_PATH is not None:
            candidates.sort(key=lambda c: c["path"] != _LAST_GOOD_PATH)

        last_err = None
        for cand in candidates:
            path = cand["path"]
            try:
                dev = self._open_device(path)
            except Exception as e:
                last_err = e
                continue

            # Tell the controller to stop pretending to be a keyboard/mouse,
            # unless we're in passive mode (just listening for hotkeys).
            if not self._passive:
                try:
                    rc = dev.send_feature_report(DISABLE_LIZARD_REPORT)
                    print(
                        f"steamcontroller: disable-lizard on iface "
                        f"{cand['interface_number']} returned {rc}"
                    )
                except Exception as e:
                    last_err = e
                    dev.close()
                    continue

            # Probe: wait briefly for input reports. Unpaired wireless ports
            # stay silent so we keep moving in that case.
            dev.set_nonblocking(0)
            deadline = time.time() + 1.5
            got_input = False
            while time.time() < deadline:
                try:
                    data = dev.read(64, 200)
                except Exception as e:
                    last_err = e
                    break
                if (
                    data
                    and len(data) >= TRITON_INPUT_MIN_LEN
                    and data[0] in TRITON_INPUT_REPORT_IDS
                ):
                    got_input = True
                    break

            if got_input:
                self._dev = dev
                _LAST_GOOD_PATH = path
                print(
                    f"steamcontroller: opened iface {cand['interface_number']}"
                )
                return

            dev.close()

        raise RuntimeError(
            "Found Steam Controller 2026 interfaces but none returned "
            "input reports. Is the controller paired/powered? "
            f"Last error: {last_err!r}"
        )

    def _lizard_watchdog(self):
        """Re-assert whichever lizard state we currently want every
        LIZARD_REFRESH_SECONDS, so the controller's own watchdog doesn't
        revert it. _lizard_enabled is read under _dev_lock so set_lizard()
        can never lose a race with a watchdog tick."""
        while not self._exit.is_set():
            if self._exit.wait(LIZARD_REFRESH_SECONDS):
                return
            with self._dev_lock:
                if self._dev is None:
                    return
                report = (
                    ENABLE_LIZARD_REPORT
                    if self._lizard_enabled
                    else DISABLE_LIZARD_REPORT
                )
                with suppress(Exception):
                    self._dev.send_feature_report(report)

    def set_lizard(self, enabled):
        """Toggle lizard (firmware mouse/kb) mode at runtime. Works in both
        passive and non-passive modes — passive callers use this to briefly
        suppress firmware kb/mouse during chord injections (e.g. so the
        Steam+VIEW → Alt+Tab chord isn't fighting a firmware-emitted Tab
        from the same VIEW button). The hardware watchdog re-asserts lizard
        in 3-5s if we don't keep re-sending, so callers needing longer
        suppression must re-send periodically."""
        with self._dev_lock:
            self._lizard_enabled = bool(enabled)
            if self._dev is None:
                return
            report = (
                ENABLE_LIZARD_REPORT
                if self._lizard_enabled
                else DISABLE_LIZARD_REPORT
            )
            with suppress(Exception):
                self._dev.send_feature_report(report)

    def haptic_pad_click(self):
        """'Physical pad click' tick for the simulated trackpad click (press
        AND release) and the L2/R2 selects. A short, crisp, slightly firmer pop
        than the light key tap — high-frequency and brief so it feels like a
        real button click rather than a deep buzz."""
        self.haptic_click(
            freq_hz=HAPTIC_PAD_CLICK_FREQ,
            gain=HAPTIC_PAD_CLICK_GAIN,
            count=4,
            duration=0.014,
        )

    def haptic_click(
        self, freq_hz=550, gain=HAPTIC_CLICK_GAIN, count=5, duration=0.018
    ):
        """Crisp trackpad 'click' for UI feedback: play a very short burst
        (`count` cycles) on both trackpad actuators so it snaps rather than
        buzzes. A higher frequency gives a faster attack (snappier onset) and
        the short safety-stop keeps the tail tight, so rapid press/release
        ticks read as distinct clicks instead of smearing into a buzz. Both
        pad writes go out under a single lock for minimal onset latency; the
        timed stop after `duration` is a safety net in case the hardware
        ignores the burst count and plays continuously."""
        pads = (HAPTIC_PAD_LEFT, HAPTIC_PAD_RIGHT)
        with self._dev_lock:
            if self._dev is None:
                return
            try:
                for act in pads:
                    self._dev.write(
                        _build_haptic_tone_report(act, freq_hz, gain, count)
                    )
            except Exception as e:
                print(f"steamcontroller: haptic_click failed: {e}")
                return

        def _stop():
            with self._dev_lock:
                if self._dev is None:
                    return
                for act in pads:
                    with suppress(Exception):
                        self._dev.write(_build_haptic_stop_report(act))

        threading.Timer(duration, _stop).start()

    def get_battery(self):
        """Most recent SteamControllerBattery seen on the wire, or None if the
        controller hasn't streamed one yet this session OR has gone silent
        (powered off via Steam+Y / dropped its wireless link) — detected as no
        input/battery frame for BATTERY_FRESH_SECONDS, so the tray doesn't keep
        showing a stale % while the dongle stays plugged in."""
        b = self._battery
        if b is None:
            return None
        if time.monotonic() - self._last_frame_t > BATTERY_FRESH_SECONDS:
            return None
        return b

    def addExit(self):
        self._exit.set()

    def run(self):
        self._exit.clear()
        try:
            if self._dev is None:
                self._open_first_responsive()
                self.opened = True
        except Exception as e:
            print(f"steamcontroller: open failed: {e}")
            self.opened = False
            return

        if not self._passive and (
            self._lizard_thread is None or not self._lizard_thread.is_alive()
        ):
            self._lizard_thread = threading.Thread(
                target=self._lizard_watchdog, daemon=True
            )
            self._lizard_thread.start()

        # A normal trigger exit with _keep_open set leaves the device open so
        # the next run() reuses it; release only on a device drop, on close(),
        # or when not in keep-open mode (legacy: release on every exit).
        close_dev = not self._keep_open
        try:
            while not self._exit.is_set():
                with self._dev_lock:
                    dev = self._dev
                if dev is None:
                    close_dev = True
                    break
                try:
                    data = dev.read(64, 200)
                except Exception as e:
                    print(f"steamcontroller: read error: {e}")
                    close_dev = True
                    break
                if not data:
                    continue
                # Power-status and link-status reports stream interleaved with
                # the game-input reports. Pull battery out here (cheap: one byte
                # compare on the read thread, off the watcher hot path) and drop
                # the link-status frames so they aren't mis-parsed as input.
                head = data[0]
                if head == TRITON_BATTERY_REPORT_ID:
                    self._last_frame_t = time.monotonic()
                    batt = _parse_battery(bytes(data))
                    if batt is not None:
                        self._battery = batt
                    continue
                if head in TRITON_WIRELESS_STATUS_IDS:
                    continue
                sci = _parse_triton(bytes(data))
                if sci is None:
                    continue
                # A real input frame — the controller is alive and streaming.
                self._last_frame_t = time.monotonic()
                try:
                    self._cb(self, sci, *self._cb_args)
                except Exception as e:
                    print(f"steamcontroller: callback raised: {e}")
        finally:
            self._exit.set()
            if close_dev:
                self._teardown_device()

    def _teardown_device(self):
        """Release the HID handle: stop haptics, restore firmware lizard mode,
        close the device. Idempotent and thread-safe (takes _dev_lock)."""
        with self._dev_lock:
            try:
                if self._dev is not None:
                    # Stop any haptics still playing so the controller
                    # doesn't keep buzzing after we release the device
                    # (e.g. a haptic_click whose timed stop hasn't fired).
                    for act in (
                        HAPTIC_PAD_LEFT,
                        HAPTIC_PAD_RIGHT,
                        HAPTIC_RUMBLE_LEFT,
                        HAPTIC_RUMBLE_RIGHT,
                    ):
                        with suppress(Exception):
                            self._dev.write(_build_haptic_stop_report(act))
                    # Restore lizard mode immediately so the controller
                    # works as a normal mouse/keyboard right away instead
                    # of waiting for the hardware watchdog (~3-5 sec).
                    # Skipped while Steam is running: Steam Input owns the
                    # lizard state and would re-assert it (a ~1s blip).
                    if not self._passive and self._restore_lizard_on_close:
                        with suppress(Exception):
                            self._dev.send_feature_report(ENABLE_LIZARD_REPORT)
                    self._dev.close()
            except Exception:
                pass
            self._dev = None
            self.opened = False

    def close(self):
        """Explicit teardown (idempotent, safe from any thread): release the
        HID handle so the controller detaches cleanly. Use when retiring an
        instance permanently (app exit, mode change); run() alone keeps the
        handle open while keep_open is set."""
        self._exit.set()
        self._keep_open = False
        self._teardown_device()
