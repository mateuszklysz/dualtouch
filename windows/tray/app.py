"""Tray application core: App class and main()."""

import os
import threading
from contextlib import suppress
from typing import Any

import pystray
import steam_shortcut as ssc
from applog import is_logging_enabled as applog_is_logging_enabled
from applog import log_line as applog_log_line
from applog import resolve_log_action, set_logging_enabled
from appsettings import (
    _SC_ACTUATION_THRESHOLDS,
    _SC_CLICK_BUTTONS,
    _load_settings,
    _save_settings,
)
from steamcontroller import SCButtons
from triton import diacritics
from triton import screen as triton_screen
from triton import skins as triton_skins
from triton import state as triton_state
from triton.win32 import _foreground_exe_name
from watchers import _ChordState

from .battery import _BatteryMixin
from .helpers import _apply_autostart
from .icon import _load_icon_image
from .launcher import _LauncherMixin
from .menu import build_menu
from .steam import _SteamLayerMixin


class App(_BatteryMixin, _LauncherMixin, _SteamLayerMixin):
    def __init__(self):
        self.settings = _load_settings()
        # Publish the logging toggle FIRST so every subsequent log call (even
        # install_helper/_apply_autostart error paths below) respects the
        # saved setting instead of the applog off-default.
        set_logging_enabled(bool(self.settings.get("logging_enabled", False)))
        # Non-elevated system-cursor helper: register the scheduled task
        # that runs cursor_helper.py in the user's interactive session
        # (the elevated tray can't touch the session's cursors itself),
        # and defensively un-hide any blank cursors left by a crashed
        # previous run.
        try:
            import cursor_ctrl

            # install_helper writes a "show" sentinel + starts the
            # non-elevated cursor daemon (stays alive watching the marker).
            # The "show" sentinel also un-blanks cursors left by a crashed
            # previous run. Do NOT call force_restore_cursor here — it
            # removes the marker, killing the daemon before the first OSK.
            cursor_ctrl.install_helper()
        except Exception:
            pass
        # Push the current startup setting into the registry so the on-disk
        # state matches the user's saved preference.
        _apply_autostart(self.settings["start_with_windows"])
        # Publish the SC haptics switch to the shared runtime flag all haptic
        # paths (OSK click feedback + desktop rumble) read.
        triton_state.set_rumble_enabled(self.settings["rumble_enabled_sc"])
        # Publish the Steam-keyboard click-sound switch (settings.json
        # "key_sound_enabled_sc"). Gated in state.key_sound_tick().
        triton_state.set_key_sound_enabled(
            self.settings.get("key_sound_enabled_sc", True)
        )
        # Normalize + publish the selected OSK skin so screen.Screen picks it up
        # the next time the keyboard opens. Fall back to the default if the
        # saved name no longer matches a bundled skin.
        if self.settings.get("skin") not in triton_skins.available_skins():
            self.settings["skin"] = triton_skins.DEFAULT_SKIN
        triton_skins.set_active_skin(self.settings["skin"])
        # Publish the OSK transparency level so screen.Screen renders it.
        triton_skins.set_transparency(
            self.settings.get("osk_transparency", "off")
        )
        # Publish the OSK window size so screen.Screen() builds the cached
        # window (below) at the right dimensions.
        triton_screen.set_osk_size(self.settings.get("osk_size", "medium"))
        # True once a size change is saved while the OSK is open — the cached
        # Screen can't be rebuilt while triton.main() is using it, so
        # launcher_thread rebuilds it right after that run finishes.
        self._pending_size_change = False
        # Publish the Steam Controller-only OSK settings (left-stick nav + L2/R2
        # actuation) so controller.py applies them on the input thread.
        triton_state.set_sc_kbd_stick_nav(
            self.settings.get("sc_left_stick_nav", True)
        )
        triton_state.set_sc_osk_trigger_threshold(
            _SC_ACTUATION_THRESHOLDS.get(
                self.settings.get("sc_osk_trigger_actuation", "default")
            )
        )
        # Split keyboard layout (left/right halves, one touchpad per side).
        triton_state.set_split_layout(
            bool(self.settings.get("osk_split_layout", False))
        )

        # Trackpad press-force calibration (settings.json, hand-edited).
        triton_state.set_sc_pad_press(
            self.settings.get("sc_pad_click_engage", 2500),
            self.settings.get("sc_pad_click_release", 1000),
            self.settings.get("sc_pad_press_hold", 2000),
            self.settings.get("sc_pad_lock_glide_alpha", 0.35),
        )
        # Key-insert paths (settings.json, hand-edited): physical trackpad
        # click on/off + which button acts as the per-side "click".
        triton_state.set_sc_pad_click_enter(
            bool(self.settings.get("sc_pad_click_enter", False))
        )
        triton_state.set_sc_click_button(
            _SC_CLICK_BUTTONS.get(
                self.settings.get("sc_click_button", "L1/R1"),
                (SCButtons.LB, SCButtons.RB),
            )
        )
        triton_state.set_sc_trigger_focus_pull(
            self.settings.get("sc_trigger_focus_pull", 16384)
        )
        # Publish the per-foreground-app remembered OSK positions so triton
        # restores each app's last-used spot on open and persists updates.
        triton_state.set_window_position_per_app(
            self.settings.get("window_position_per_app", {})
        )
        # Publish the per-foreground-app remembered OSK look (size + skin) so
        # the launcher applies each app's own look on open; the tray updates
        # these maps when a size/skin is selected while an app is foreground.
        triton_state.set_osk_size_per_app(
            self.settings.get("osk_size_per_app", {})
        )
        triton_state.set_skin_per_app(self.settings.get("skin_per_app", {}))
        # Publish the diacritic-variant config (Feature B): merge the built-in
        # per-locale map with the user's settings map (user wins per letter),
        # resolve the active locale from the Windows keyboard layout unless a
        # specific one is picked, and push the merged map + locale + on/off to
        # the OSK runtime state.
        self._publish_diacritics()

        self._stop_event = threading.Event()
        # Steam-required cache for _should_abort_sc (2s TTL — the per-frame
        # abort check must not call psutil on every HID report).
        self._steam_ok_at = 0.0
        self._steam_ok_cached = False
        # Set once when block_sc_hid's exclusive grab keeps failing while Steam
        # holds the controller (warned in the log) — re-armed on toggle enable.
        self._block_held_warned = False
        # Wake event so steam_watch_thread can BLOCK (zero polling) while its
        # feature is inactive instead of waking on a timer.
        self._steam_watch_wake = threading.Event()
        self._current_sc = None
        # The SteamController instance is persisted across launcher iterations
        # (opened with keep_open) so the HID handle never closes between
        # keyboard sessions — a close/re-open makes Steam Input see the
        # controller detach and re-init, which shows as a lizard-mode blip
        # right after closing the keyboard. Rebuilt only when the
        # passive/exclusive flags change or the device dropped.
        self._persistent_sc = None
        self._persistent_sc_passive = None
        self._persistent_sc_exclusive = None
        # Open-keyboard request plumbing: _open_kbd_event asks
        # launcher_thread to open the on-screen keyboard (tray menu);
        # _launcher_wake wakes the launcher out of its reconnect backoff so the
        # request is honored promptly even with no controller attached; _kbd_open
        # tracks whether triton_app.main() is currently running.
        self._open_kbd_event = threading.Event()
        self._launcher_wake = threading.Event()
        self._kbd_open = False
        # One-shot: the startup /0 (auto-config restore) has been dispatched.
        self._startup_appid_zeroed = False
        # Window to restore focus to after the OSK opens.
        self._pending_restore_hwnd = None
        # Steam Input config appid that was active BEFORE the OSK forced
        # the keyboard layer (captured at open from controller_ui.txt). On
        # close we force back to THIS appid instead of /0: a specific-appid
        # force is applied by Steam instantly (no activation change needed),
        # so the close path needs no helper-window hop / no flash. None if
        # the capture failed — close then falls back to /0 + the nudge hop.
        self._osk_restore_appid = None
        # True while the keyboard layer force is active (set when the
        # open forceinputappid succeeded). The on_close restore callback
        # must only act when the layer was actually forced — otherwise it
        # would dispatch /0 / force-back when steam_kbd_layer is off or
        # the open force failed.
        self._steam_layer_active = False
        # True once the close-time Steam Input restore (force-back + /0)
        # has been dispatched. The on_close callback fires it EARLY (at
        # close-detection, overlapping triton teardown); the finally block
        # must not dispatch a second time.
        self._steam_restore_dispatched = False
        # Publish the focus-flash experiments (settings — see
        # windows/flashing_issue.md) BEFORE the cached Screen is built: the
        # always-visible flag changes how Screen creates the OSK window, so it
        # must be set before pre-warm.
        triton_state.set_osk_always_visible(
            self.settings.get("focus_fix_open", "always-visible")
            == "always-visible"
        )
        # Pre-build the OSK Screen once at startup (loads 6 TTF fonts + creates
        # SDL window/renderer). triton_app.main() reuses this on every open
        # instead of rebuilding from scratch — cuts open latency from ~300ms to
        # near-zero (just show the already-built hidden window). None if SDL
        # VIDEO unavailable or Screen construction fails.
        self._cached_screen = None
        try:
            self._cached_screen = triton_screen.Screen()
            from triton import triton as _triton_mod

            _triton_mod._make_window_non_activating(self._cached_screen.window)
        except Exception as e:
            print(f"Screen pre-warm failed (will build on first open): {e!r}")
        # Chord state shared across every _Watcher rebuild so an in-progress
        # Steam+VIEW=Alt+Tab doesn't lose track of held keys when sc.run()
        # is kicked mid-chord.
        self._chord = _ChordState()
        # Battery status (see battery_thread). _battery is the last
        # SteamControllerBattery polled from the live SteamController, or None
        # until one streams a power report. _battery_label is the cached menu /
        # tooltip text. _low_warned_at is the lowest low-battery band (20/10/5)
        # we've already toasted at this discharge cycle, so each band warns once;
        # it resets when the pack charges or recovers above the hysteresis line.
        # _charge_complete_notified latches the "fully charged" toast.
        # _was_charging tracks the charge state across polls so we can toast the
        # discharging→charging edge (the "plugged in" notification).
        self._battery = None
        self._battery_label = None
        self._low_warned_at = None
        self._charge_complete_notified = False
        self._was_charging = False

    # tray menu state predicates --------------------------------------------

    def is_start_with_windows_checked(self, item):
        return self.settings["start_with_windows"]

    # Skin submenu: one radio item per bundled skin. pystray needs a distinct
    # checked-predicate and action per name, so we build small closures.
    def is_skin_checked(self, name):
        return lambda item: self.settings.get("skin") == name

    def _update_per_app_look(self, per_app_key, value):
        """Record a tray-selected OSK size/skin as the CURRENT foreground
        app's remembered look (the per-app override), so each app reopens with
        the look it had when last changed. No-op for non-positionable
        foregrounds (None) — those only set the global. Mutates self.settings
        and republishes the map to triton state; the caller's _save_settings
        writes it to disk."""
        exe = _foreground_exe_name()
        if exe is None:
            return
        per_app = dict(self.settings.get(per_app_key, {}))
        if per_app.get(exe) == value:
            return
        per_app[exe] = value
        self.settings[per_app_key] = per_app
        if per_app_key == "osk_size_per_app":
            triton_state.set_osk_size_per_app(per_app)
        else:
            triton_state.set_skin_per_app(per_app)

    def select_skin(self, name):
        def _select(icon, item):
            self.settings["skin"] = name
            # A foreground app picks up this skin as its remembered look
            # (switching apps restores each one's own skin); apps with no
            # entry keep the global.
            self._update_per_app_look("skin_per_app", name)
            _save_settings(self.settings)
            triton_skins.set_active_skin(name)
            # If the keyboard is open it re-skins live on its next frame (the
            # render loop polls skins.get_generation); otherwise it just opens
            # with the new skin next time.
            self._refresh_menu()

        return _select

    def is_transparency_checked(self, level):
        return lambda item: (
            self.settings.get("osk_transparency", "off") == level
        )

    def select_transparency(self, level):
        # OSK transparency level (Keyboard Skin → Transparent submenu). Shares the
        # skin generation counter, so an open keyboard switches live on its next
        # frame; otherwise it applies on the next open.
        def _select(icon, item):
            self.settings["osk_transparency"] = level
            _save_settings(self.settings)
            triton_skins.set_transparency(level)
            self._refresh_menu()

        return _select

    # OSK size (Keyboard Skin → Size submenu): "small" / "medium" (default) /
    # "full" (fills the display - good for a Steam Deck). Unlike skin/
    # transparency this changes the window's pixel size and font sizes, which
    # are baked in at Screen() construction time, so it needs the cached
    # Screen rebuilt (see _rebuild_cached_screen).
    def is_osk_size_checked(self, name):
        return lambda item: self.settings.get("osk_size", "medium") == name

    def select_osk_size(self, name):
        def _select(icon, item):
            self.settings["osk_size"] = name
            # A foreground app picks up this size as its remembered look
            # (switching apps restores each one's own size).
            self._update_per_app_look("osk_size_per_app", name)
            _save_settings(self.settings)
            triton_screen.set_osk_size(name)
            if self._kbd_open:
                # triton.main() is using _cached_screen on launcher_thread right
                # now — rebuild it once that run finishes (see the tray open).
                self._pending_size_change = True
            else:
                self._rebuild_cached_screen()
            self._refresh_menu()

        return _select

    # OSK-open chord (Startup → Open Keyboard Chord): the Steam+<button>
    # combination that opens the on-screen keyboard while it's closed. Read by
    # the watcher from the raw HID (works elevated). Radio submenu.
    def is_osk_open_chord_checked(self, name):
        return lambda item: self.settings.get("sc_osk_open_chord", "X") == name

    def select_osk_open_chord(self, name):
        def _select(icon, item):
            self.settings["sc_osk_open_chord"] = name
            _save_settings(self.settings)
            self._refresh_menu()
            # Make Steam consume the newly-selected chord so it doesn't open
            # its own menu on the same press (backs up the config first).
            try:
                ssc.block_open_chord(chord=name)
            except Exception as e:
                print(f"guide-chord block failed: {e!r}")

        return _select

    # tray menu actions -----------------------------------------------------

    def toggle_start_with_windows(self, icon, item):
        self.settings["start_with_windows"] = not item.checked
        _save_settings(self.settings)
        _apply_autostart(self.settings["start_with_windows"])

    def toggle_logging(self, icon, item):
        self.settings["logging_enabled"] = not item.checked
        _save_settings(self.settings)
        set_logging_enabled(self.settings["logging_enabled"])
        self._refresh_menu()

    def is_logging_checked(self, item):
        return self.settings.get("logging_enabled", False)

    def view_log(self, icon, item):
        """Tray "View Log": open dualtouch.log with the default handler.
        If logging is off or the file doesn't exist yet, enable logging for
        THIS call only (write a marker so the file exists) — never persist
        logging_enabled, so the tray toggle stays exactly as the user set it.
        Viewing a log must not silently flip the user's logging preference."""
        if not applog_is_logging_enabled():
            set_logging_enabled(True)  # in-memory only; not saved
        action, path = resolve_log_action()
        if action != "open":
            # Logging is on (this call) but the file was never written (e.g.
            # no activity yet) — write a marker so there is something to open.
            applog_log_line("tray", "Log opened via tray View Log")
            action, path = resolve_log_action()
        if action == "open":
            try:
                os.startfile(path)
                return
            except Exception as e:
                print(f"view log open failed: {e!r}")

    # --- Live Steam Controller settings (tray "Steam Controller" submenu) ---
    # Every toggle/radio below saves to settings.json AND republishes to the
    # runtime state immediately, so a change applies without a restart or a
    # hand-edited file. Follows the existing tray item pattern.

    def toggle_key_sound(self, icon, item):
        self.settings["key_sound_enabled_sc"] = not item.checked
        _save_settings(self.settings)
        triton_state.set_key_sound_enabled(
            self.settings["key_sound_enabled_sc"]
        )
        self._refresh_menu()

    def is_key_sound_checked(self, item):
        return self.settings.get("key_sound_enabled_sc", True)

    def toggle_pad_click_enter(self, icon, item):
        self.settings["sc_pad_click_enter"] = not item.checked
        _save_settings(self.settings)
        triton_state.set_sc_pad_click_enter(
            self.settings["sc_pad_click_enter"]
        )
        self._refresh_menu()

    def is_pad_click_enter_checked(self, item):
        return self.settings.get("sc_pad_click_enter", False)

    def select_click_button(self, name):
        def _select(icon, item):
            self.settings["sc_click_button"] = name
            _save_settings(self.settings)
            triton_state.set_sc_click_button(
                _SC_CLICK_BUTTONS.get(name, (SCButtons.LB, SCButtons.RB))
            )
            self._refresh_menu()

        return _select

    def is_click_button_checked(self, name):
        return lambda item: (
            self.settings.get("sc_click_button", "L1/R1") == name
        )

    # Split keyboard layout: left/right halves, each touchpad covers its own
    # half (no cross-body reach). Published to triton state immediately, so an
    # already-open keyboard resizes its window + re-lays-out live (the main
    # loop handles the flag change, exactly like a resize). The window WIDTH
    # (full display width in split mode) is baked at Screen construction, so
    # the cached Screen is rebuilt once the current run finishes / when closed.
    def toggle_split_layout(self, icon, item):
        self.settings["osk_split_layout"] = not item.checked
        _save_settings(self.settings)
        triton_state.set_split_layout(self.settings["osk_split_layout"])
        if self._kbd_open:
            self._pending_size_change = True
        else:
            self._rebuild_cached_screen()
        self._refresh_menu()

    def is_split_layout_checked(self, item):
        return self.settings.get("osk_split_layout", False)

    # --- Diacritic variants (Feature B: hold a letter to pick accents) -------
    # Follows the existing toggle/radio pattern: save to settings.json AND
    # republish to the runtime state immediately, so a change applies without
    # a restart (and to a keyboard already open).

    def _publish_diacritics(self):
        """Recompute + republish the merged variant map, active locale and
        enabled flag (called at startup and on tray locale/map changes)."""
        user_variants = self.settings.get("diacritic_variants", {}) or {}
        merged_variants = diacritics.merge_diacritic_maps(
            diacritics.DIACRITIC_VARIANTS, user_variants
        )
        locale = str(self.settings.get("diacritic_locale", "auto")).lower()
        if locale == "auto":
            locale = diacritics.detect_windows_locale() or "en"
        triton_state.set_diacritic_variants(merged_variants)
        triton_state.set_active_locale(locale)
        triton_state.set_diacritics_enabled(
            bool(self.settings.get("diacritics_enabled", True))
        )

    def toggle_diacritics(self, icon, item):
        self.settings["diacritics_enabled"] = not item.checked
        _save_settings(self.settings)
        triton_state.set_diacritics_enabled(
            self.settings["diacritics_enabled"]
        )
        self._refresh_menu()

    def is_diacritics_checked(self, item):
        return self.settings.get("diacritics_enabled", True)

    def diacritic_locale_options(self):
        """Radio options for the Locale submenu: "auto" (resolve from the
        Windows keyboard layout) plus the locales present in the merged map."""
        user_variants = self.settings.get("diacritic_variants", {}) or {}
        merged = diacritics.merge_diacritic_maps(
            diacritics.DIACRITIC_VARIANTS, user_variants
        )
        return ["auto"] + sorted(loc for loc in merged if loc != "auto")

    def select_diacritic_locale(self, locale):
        def _select(icon, item):
            self.settings["diacritic_locale"] = locale
            _save_settings(self.settings)
            # Re-resolve + republish so an open keyboard switches locale live
            # ("auto" re-reads the Windows keyboard layout).
            self._publish_diacritics()
            self._refresh_menu()

        return _select

    def is_diacritic_locale_checked(self, locale):
        return lambda item: (
            self.settings.get("diacritic_locale", "auto") == locale
        )

    def exit_app(self, icon, item):
        self._stop_event.set()
        # Wake any event-idle background threads so they observe the stop.
        self._steam_watch_wake.set()
        self._launcher_wake.set()
        triton_state.close()
        if self._current_sc is not None:
            with suppress(Exception):
                self._current_sc.addExit()
        # Defensive: if exit happens mid-chord, make sure we don't leave
        # Alt held at the OS level.
        with suppress(Exception):
            self._chord.release_alt()
        self._close_persistent_sc()
        # Un-hide the system cursors on exit in case the OSK was open
        # when the user quit (the close-path restore already covers a
        # normal close; this covers quitting while the OSK is up).
        with suppress(Exception):
            import cursor_ctrl

            cursor_ctrl.force_restore_cursor()
        icon.stop()

    def _notify(self, title, message):
        icon = self._icon_ref
        if icon is None:
            return
        with suppress(Exception):
            icon.notify(message, title)

    # Set by main() so the tray menu can be rebuilt when the OSK opens/closes.
    _icon_ref: Any = None

    def _refresh_menu(self):
        """Rebuild the tray menu so the dynamic Open/Close Keyboard label
        re-reads _kbd_open. Called whenever _kbd_open flips on the launcher
        thread — the keyboard opens/closes asynchronously, so the rebuild
        pystray does right after a menu click happens before _kbd_open has
        actually changed and would otherwise leave the label stale."""
        icon = self._icon_ref
        if icon is not None:
            with suppress(Exception):
                icon.update_menu()


def main():
    app = App()
    image = _load_icon_image()

    menu = build_menu(app)

    icon = pystray.Icon("SteamControllerKeyboard", image, "DualTouch", menu)
    app._icon_ref = icon

    def setup(icon):
        icon.visible = True
        # Verify/register the Steam keyboard-layer shortcut before the app
        # threads start — a no-op once registered (fast file check).
        app._steam_kbd_setup()
        # Make Steam consume the OSK-open chord (Steam+<button>) so pressing
        # it doesn't ALSO open Steam's guide menu. No-op if Steam is missing.
        with suppress(Exception):
            ssc.block_open_chord(
                chord=app.settings.get("sc_osk_open_chord", "X")
            )
        threading.Thread(target=app.launcher_thread, daemon=True).start()
        threading.Thread(target=app.steam_watch_thread, daemon=True).start()
        threading.Thread(target=app.battery_thread, daemon=True).start()
        threading.Thread(target=app.device_watch_thread, daemon=True).start()
        # NOTE: no global hotkey / Esc-close listeners. The OSK opens via the
        # controller Steam+<chord> (watcher) or the tray menu. The OSK used to
        # close when Esc was pressed while it was open — but Caps Lock is often
        # was pressed while it was open — but Caps Lock is often remapped to
        # Esc (PowerToys Keyboard Manager), so pressing Caps on the OSK sent
        # an Esc that closed the keyboard. Esc is a normal key now: it reaches
        # the focused app like any other key, and the OSK stays open until the
        # user closes it (B / LGRIP / Steam+<chord> / tray).

    try:
        icon.run(setup=setup)
    except OSError as e:
        # pystray's win32 backend can raise "[WinError 1401] Invalid menu
        # handle" while tearing down the tray menu during Exit (icon.stop()).
        # The app is already shutting down, so swallow that specific error to
        # avoid a spurious PyInstaller crash dialog; re-raise anything else.
        if getattr(e, "winerror", None) != 1401:
            raise
