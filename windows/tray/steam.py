"""Steam keyboard-layer mixin."""

import json
import threading
import traceback
from collections.abc import Callable

import steam_shortcut as ssc
from applog import _log
from appsettings import _save_settings


class _SteamLayerMixin:
    # Attributes provided by the composed tray App (declared here so static
    # tooling knows the mixin's contract — see tray/app.py __init__).
    settings: dict
    _stop_event: threading.Event
    _steam_watch_wake: threading.Event
    _notify: Callable[[str, str], None]

    # Steam keyboard-layer switching (see steam_shortcut.py) ----------------

    def is_steam_kbd_layer_checked(self, item):
        return self.settings.get("steam_kbd_layer", True)

    def toggle_steam_kbd_layer(self, icon, item):
        self.settings["steam_kbd_layer"] = not item.checked
        _save_settings(self.settings)
        # Enabling should take effect immediately, not on the next app start.
        if self.settings["steam_kbd_layer"]:
            self._steam_kbd_setup()

    def register_steam_kbd_layer(self, icon, item):
        """Tray action: force registration/verification of the keyboard-layer
        shortcut now, with a notification so the user sees what happened."""
        self._steam_kbd_setup(notify=True)

    def _steam_kbd_setup(self, notify=False):
        """Verify or register the "DualTouch Keyboard Layer" shortcut.
        Runs at app start, when the setting is enabled, and (via the
        steam-watch thread) on the registration poll. Works with Steam
        running (Steam hot-reloads shortcuts.vdf — no restart needed).
        Logs and notifies only when the status CHANGES — an unchanged
        result (e.g. the registration poll) must stay silent."""
        if not self.settings.get("steam_kbd_layer", True):
            return
        try:
            result = ssc.verify_or_register()
        except Exception as e:
            _log(
                f"steam shortcut: registration crashed: {e!r}"
                f"\n{traceback.format_exc()}"
            )
            return
        if result == getattr(self, "_steam_kbd_last_status", None):
            return  # unchanged — no log, no toast
        self._steam_kbd_last_status = result
        _log("steam shortcut: " + json.dumps(result, default=str))
        status = result.get("status")
        if status in ("written", "pending"):
            self._notify(
                "Steam keyboard layer",
                "Shortcut registered — configure it once in Steam: "
                "Library → DualTouch Keyboard Layer → controller "
                "config → unbind LB/RB.",
            )
        elif status == "ready" and not ssc.load_state().get(
            "config_hint_shown"
        ):
            self._notify(
                "Steam keyboard layer ready",
                "Configure it once: Library → DualTouch Keyboard "
                "Layer → controller config → unbind LB/RB.",
            )
            state = ssc.load_state()
            state["config_hint_shown"] = True
            ssc.save_state(state)

    def steam_watch_thread(self):
        """Shepherd the keyboard-layer shortcut registration until its AppID
        converges (Steam hot-reloads shortcuts.vdf, but read-back can take a
        few seconds). Wrapped like launcher_thread: a crash is logged and the
        loop restarts (with backoff) instead of silently losing the
        registration poll."""
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                self._steam_watch_loop()
                return
            except Exception as e:
                _log(
                    f"steam-watch loop crashed: {e!r}\n{traceback.format_exc()}"
                )
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _steam_watch_loop(self):
        while not self._stop_event.is_set():
            # Keyboard-layer shortcut registration: shortcuts.vdf can be
            # written with Steam running (Steam hot-reloads it — the entry
            # appears live), and the effective AppID converges via read-back /
            # placeholder-adoption grace. While no usable AppID is cached,
            # poll at a slow cadence; once the AppID is cached (or the
            # feature is off), the poll stops permanently and we block with
            # zero wakeups.
            if (
                self.settings.get("steam_kbd_layer", True)
                and ssc.cached_appid() is None
            ):
                self._steam_kbd_setup()
                self._stop_event.wait(5.0)
                continue
            self._steam_watch_wake.wait()
            self._steam_watch_wake.clear()
