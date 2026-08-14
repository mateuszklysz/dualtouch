"""Launcher mixin: owner of the OSK window, the controller and Steam restore."""

import threading
import time
import traceback
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import sdl3w as S
import steam_shortcut as ssc
from applog import _log
from appsettings import _SC_OSK_OPEN_CHORDS
from steamcontroller import SCButtons, SteamController

if TYPE_CHECKING:
    pass
from triton import applook
from triton import screen as triton_screen
from triton import skins as triton_skins
from triton import state as triton_state
from triton import triton as triton_app
from triton.win32 import _foreground_exe_name
from watchers import _ChordState, _Watcher
from win_focus import (
    _capture_active_appid,
    _foreground_target_hwnd,
    _nudge_after_restore,
    _restore_auto_after_force,
)

from .helpers import _steam_running


def _verify_force_appid(steam_path, appid):
    """LOW-2: only force an appid that actually corresponds to the DualTouch
    shortcut in the CURRENT shortcuts.vdf. The cached appid round-trips
    through the user-writable state file; a stale/forged value would pin
    Steam Input to a dead config until alt-tab. Best-effort with existing
    helpers (no new subprocess / config reads): if the vdf can't be read or
    Steam's path is unresolvable, the force is allowed through."""
    try:
        if not steam_path:
            return True
        data, path, err, _ = ssc._read_shortcuts(steam_path)
        if data is None:
            return True  # unreadable — can't verify (best-effort allow)
        entry = ssc._find_entry(data)
        if entry is None or entry.get("appid") is None:
            return False
        return int(entry["appid"]) == int(appid)
    except Exception:
        return True


class _LauncherMixin:
    # Attributes provided by the composed tray App (declared here so static
    # tooling knows the mixin's contract — see tray/app.py __init__).
    settings: dict
    _stop_event: threading.Event
    _launcher_wake: threading.Event
    _open_kbd_event: threading.Event
    _chord: _ChordState
    _current_sc: SteamController | None
    _persistent_sc: SteamController | None
    _notify: Callable[[str, str], None]
    _refresh_menu: Callable[[], None]
    _icon_ref: Any

    def _rebuild_cached_screen(self):
        """Destroy and recreate the cached OSK Screen so a new "Size" setting
        takes effect on the next open. Only safe while the OSK is closed (the
        cached Screen isn't being used by triton.main() on launcher_thread)."""
        if self._cached_screen is None:
            return
        try:
            S.SDL_DestroyRenderer(self._cached_screen.renderer)
            S.SDL_DestroyWindow(self._cached_screen.window)
        except Exception:
            pass
        try:
            self._cached_screen = triton_screen.Screen()
            from triton import triton as _triton_mod

            _triton_mod._make_window_non_activating(self._cached_screen.window)
        except Exception as e:
            print(f"Screen rebuild failed: {e!r}")
            self._cached_screen = None

    def _apply_app_look(self):
        """Apply the CURRENT foreground app's remembered OSK size + skin (the
        per-app override) so this open shows that app's look; apps without an
        entry (and non-positionable foregrounds, where _foreground_exe_name
        returns None) fall back to the global osk_size / skin. Size is baked
        into the cached Screen at construction, so a per-app size that differs
        from the current one rebuilds the cached Screen before triton opens.
        Must be called before triton_app.main() while the OSK is closed."""
        exe = _foreground_exe_name()
        size = applook._size_for_app(exe, triton_state.get_osk_size_per_app())
        if size is None:
            size = self.settings.get("osk_size", "medium")
        if size != triton_screen.get_osk_size():
            triton_screen.set_osk_size(size)
            self._rebuild_cached_screen()
        skin = applook._skin_for_app(exe, triton_state.get_skin_per_app())
        if skin is None:
            skin = self.settings.get("skin", triton_skins.DEFAULT_SKIN)
        # A saved per-app skin that no longer matches an available skin
        # (removed from Steam, renamed, or Steam missing) falls back to the
        # default rather than a no-palette skin.
        if skin not in triton_skins.available_skins():
            skin = triton_skins.DEFAULT_SKIN
        triton_skins.set_active_skin(skin)

    def _dispatch_steam_restore(self, restore_hwnd):
        """Restore the Steam Input config that was active before the OSK.

        PREFERRED: force back to the appid captured at open
        (_osk_restore_appid) — a specific-appid force is applied by Steam
        INSTANTLY (no activation change needed), so no helper-window hop,
        no flash, no timing gamble — then dispatch /0 to return Steam
        Input to auto-switching (so alt-tab to a game / Big Picture still
        picks the right config). FALLBACK: /0 + the delayed focus hop
        (old path), used only when the capture failed.

        Fired by triton's on_close callback at close-detection so the
        explorer.exe shell hop overlaps the teardown (faster restore),
        and re-invoked from the finally block as a safety net if the
        callback never ran. Idempotent via _steam_restore_dispatched."""
        # Only restore if the keyboard layer was actually forced at open
        # (steam_kbd_layer on AND the open force succeeded); never touch
        # the appid on tray Exit (see the finally guard).
        if (
            not self._steam_layer_active
            or self._stop_event.is_set()
            or self._steam_restore_dispatched
        ):
            return
        self._steam_restore_dispatched = True
        steam_path = ssc.find_steam_path()
        restore_appid = self._osk_restore_appid
        if restore_appid and steam_path:
            if ssc.force_appid(restore_appid):
                _log(
                    "steam input: OSK closed — forced back to "
                    f"config appid {restore_appid} (instant restore, no hop)"
                )
                # The force leaves Steam Input pinned to that appid —
                # alt-tab to a game / Big Picture wouldn't switch configs.
                # Dispatch /0 AFTER the force is applied to return to
                # auto-switching. No hop: the current app already has the
                # right config; /0 just re-enables focus-following.
                threading.Thread(
                    target=_restore_auto_after_force,
                    args=(steam_path, restore_appid),
                    daemon=True,
                ).start()
            else:
                _log(
                    "steam input: restore force dispatch failed "
                    f"for appid {restore_appid} — falling back to /0 + hop"
                )
                ssc.force_appid(0)
                if steam_path:
                    threading.Thread(
                        target=_nudge_after_restore,
                        args=(
                            restore_hwnd,
                            steam_path,
                            self.settings.get("steam_input_nudge", "hop"),
                            self.settings.get("steam_input_nudge_delay", 1.0),
                        ),
                        daemon=True,
                    ).start()
        else:
            # /0 alone does NOT make Steam Input re-evaluate (observed: no
            # config reload until a manual alt-tab). So a daemon thread
            # re-activates the app's window (helper-window foreground hop)
            # after /0 to synthesize the activation change Steam needs.
            ssc.force_appid(0)
            _log("steam input: OSK closed — restored auto config")
            if steam_path:
                threading.Thread(
                    target=_nudge_after_restore,
                    args=(
                        restore_hwnd,
                        steam_path,
                        self.settings.get("steam_input_nudge", "hop"),
                        self.settings.get("steam_input_nudge_delay", 1.0),
                    ),
                    daemon=True,
                ).start()

    def _close_persistent_sc(self):
        """Release the persisted SteamController and its HID handle. Called
        on app exit only — the handle is deliberately kept open for the whole
        session otherwise (see launcher_thread)."""
        if self._persistent_sc is not None:
            with suppress(Exception):
                self._persistent_sc.close()
            self._persistent_sc = None
            self._persistent_sc_passive = None
            self._persistent_sc_exclusive = None

    # background threads ----------------------------------------------------

    def _steam_required_ok(self):
        """Cached "Steam is running" for the per-frame abort check — psutil's
        process scan only runs every 2s, not on every HID report."""
        now = time.monotonic()
        if now - self._steam_ok_at >= 2.0:
            self._steam_ok_at = now
            self._steam_ok_cached = _steam_running()
        return self._steam_ok_cached

    def _should_abort_sc(self):
        # Steam is required — when it exits, tear the watcher down so the
        # launcher waits for it again instead of running standalone.
        return self._stop_event.is_set() or not self._steam_required_ok()

    def _launcher_wait(self, timeout):
        """Backoff sleep for launcher_thread that also wakes early on a stop or
        an open-keyboard request (so the tray Open Keyboard is responsive
        even when no controller is attached and the loop is in its reconnect
        backoff)."""
        self._launcher_wake.wait(timeout)
        self._launcher_wake.clear()

    def _kbd_menu_label(self, item):
        """Dynamic label for the tray's top menu item: shows the action that a
        click will perform given the keyboard's current open/closed state."""
        return "Close Keyboard" if self._kbd_open else "Open Keyboard"

    def open_or_close_keyboard(self, icon, item):
        """Tray menu: open the on-screen keyboard, or close it if it's already
        open (launcher_thread owns the window)."""
        self.toggle_keyboard()

    def toggle_keyboard(self):
        """Open the on-screen keyboard, or close it if it's open. Used by the
        tray menu Open/Close item. Only signals — launcher_thread owns the
        window and actually opens/closes it."""
        if self._kbd_open:
            triton_state.close()
            return
        _log("open keyboard requested: tray")
        # Remember the window the user was in so triton can restore focus after
        # the OSK opens.
        self._pending_restore_hwnd = _foreground_target_hwnd()
        self._open_kbd_event.set()
        self._launcher_wake.set()
        # Break the current sc.run() (if a controller is connected) so the
        # launcher loop proceeds straight to opening the keyboard.
        sc = self._current_sc
        if sc is not None:
            with suppress(Exception):
                sc.addExit()

    def launcher_thread(self):
        """Owner of the OSK window: opens/closes it, watches the controller.

        Wrapped so an unexpected exception in the loop is logged to
        dualtouch.log (a windowed exe hides all prints) instead of silently
        killing the thread — a dead launcher makes the tray's Open Keyboard
        do nothing at all.
        """
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                self._launcher_loop()
                return
            except Exception as e:
                _log(f"launcher loop crashed: {e!r}\n{traceback.format_exc()}")
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _launcher_loop(self):
        # Reconnect backoff: when no controller is present, opening fails fast
        # and we'd otherwise re-enumerate HID every second forever (the common
        # case — tray app running with the controller turned off). Back off up
        # to RECONNECT_WAIT_MAX, resetting the instant a controller appears.
        reconnect_wait = 1.0
        RECONNECT_WAIT_MIN = 1.0
        RECONNECT_WAIT_MAX = 5.0
        while not self._stop_event.is_set():
            # Startup cleanup (once): a previous session may have left the
            # keyboard-layer appid FORCED (crash / killed exe — the force
            # lives in the Steam client, so restarting Steam clears it but
            # restarting DualTouch alone does not). /0 restores auto config
            # so the controller works right away. Idempotent when nothing
            # is forced; no-op when Steam is down (its runtime state
            # cleared with the client anyway).
            if not self._startup_appid_zeroed:
                self._startup_appid_zeroed = True
                try:
                    if ssc.force_appid(0):
                        _log(
                            "steam input: startup — restored auto config (/0)"
                        )
                except Exception:
                    pass
            # Steam is required: nothing runs until the Steam client is up.
            # The keyboard layer, the watcher and the OSK all assume Steam
            # Input is present — no standalone/no-Steam mode. Polls every 5s
            # (and wakes early on tray Exit via _launcher_wake).
            if not _steam_running():
                _log("waiting for Steam — DualTouch requires it to be running")
                while not self._stop_event.is_set():
                    if _steam_running():
                        break
                    # Clear the wake after each wait (like _launcher_wait):
                    # an early open-keyboard request sets it, and without
                    # the clear the wait would return instantly forever — a
                    # busy-wait.
                    self._launcher_wake.wait(5.0)
                    self._launcher_wake.clear()
                if self._stop_event.is_set():
                    return

            # Always passive (firmware lizard-on): the controller keeps working
            # as mouse/kb between Steam+X presses, and the watcher needs no
            # virtual pad to avoid duplicating input.
            # The OSK-open chord (Steam+<button>) comes from settings; the
            # watcher reads the raw HID so it works elevated. Default Steam+X.
            open_chord = _SC_OSK_OPEN_CHORDS.get(
                self.settings.get("sc_osk_open_chord", "X"), SCButtons.X
            )
            watcher = _Watcher(
                self._should_abort_sc, chord=self._chord, open_chord=open_chord
            )
            # block_sc_hid opens the physical Steam Controller HID exclusively
            # so Steam can't read it (no Steam Input / forced lizard while we
            # hold it). Must be enabled before Steam opens the controller.
            use_exclusive = self.settings["block_sc_hid"]
            passive_flag = True
            # Reuse the persisted controller when the open mode is unchanged:
            # its HID handle stays open across keyboard sessions, so Steam
            # Input never sees a detach/re-enumerate (which causes a lizard-
            # mode blip right after closing the keyboard). Rebuild only when
            # the passive/exclusive flags changed or the device dropped.
            need_rebuild = (
                self._persistent_sc is None
                or self._persistent_sc_passive != passive_flag
                or self._persistent_sc_exclusive != use_exclusive
                or not self._persistent_sc.opened
            )
            if need_rebuild:
                if self._persistent_sc is not None:
                    self._persistent_sc.close()
                    self._persistent_sc = None
                sc = SteamController(
                    callback=watcher.on_input,
                    passive=passive_flag,
                    exclusive=use_exclusive,
                    keep_open=True,
                )
                self._persistent_sc = sc
                self._persistent_sc_passive = passive_flag
                self._persistent_sc_exclusive = use_exclusive
            else:
                sc = self._persistent_sc
                assert sc is not None, (
                    "need_rebuild is False only when _persistent_sc is set"
                )
                sc._cb = watcher.on_input
            self._current_sc = sc
            try:
                sc.run()
            except KeyboardInterrupt:
                self._close_persistent_sc()
                return
            finally:
                self._current_sc = None
            _log(
                f"sc.run returned: opened={sc.opened} "
                f"hotkey={self._open_kbd_event.is_set()}"
            )

            if self._stop_event.is_set():
                self._close_persistent_sc()
                return
            # Open the keyboard on the raw-HID Steam+<chord> press
            # (watcher.triggered) OR the tray-menu Open Keyboard request
            # (_open_kbd_event).
            open_kbd = watcher.triggered or self._open_kbd_event.is_set()
            self._open_kbd_event.clear()
            if not open_kbd:
                # sc.run() returned without an open request. Two cases:
                if sc.opened:
                    # It opened and ran, so the device dropped mid-use (or was
                    # powered off via Steam+Y). Brief backoff before retry.
                    reconnect_wait = RECONNECT_WAIT_MIN
                    self._launcher_wait(RECONNECT_WAIT_MIN)
                else:
                    # Open failed — no controller present. Back off so we don't
                    # re-enumerate HID every second while it stays disconnected.
                    self._launcher_wait(reconnect_wait)
                    reconnect_wait = min(
                        reconnect_wait * 2, RECONNECT_WAIT_MAX
                    )
                    # block_sc_hid's exclusive grab only works if we open the
                    # device before Steam does; while Steam holds it, every
                    # open fails. Warn once so it isn't silently dead.
                    if (
                        self.settings["block_sc_hid"]
                        and _steam_running()
                        and not self._block_held_warned
                    ):
                        self._block_held_warned = True
                        _log(
                            "block_sc_hid: exclusive open failing while Steam "
                            "is running — Steam holds the controller; restart "
                            "Steam for the block to take effect"
                        )
                continue

            # Open request — reset the backoff and open the keyboard.
            reconnect_wait = RECONNECT_WAIT_MIN
            # Snapshot the window the user was typing in NOW, before the HID
            # handoff — the watcher sampled it just before the opening press, so
            # triton can restore focus to it once the OSK is up (the controller-
            # open's firmware mouse-click can otherwise leave the field unfocused).
            # Controller open → the tray menu captured _pending_restore_hwnd
            # in the menu action (see toggle_keyboard), where
            # pystray's popup menu (our own process) may still be foreground —
            # that capture then resolves to None and no restore would happen.
            # Fall back to a live capture here: by open time the menu is closed
            # and focus has returned to the user's app.
            restore_hwnd = (
                self._pending_restore_hwnd or _foreground_target_hwnd()
            )
            self._pending_restore_hwnd = None
            _log("opening keyboard")
            # Brief HID-handoff settle, then open the keyboard in-process.
            time.sleep(0.1)
            # Publish Steam's current status: while Steam runs, the OSK's
            # controller source skips its firmware lizard-restore on close so
            # Steam Input doesn't re-assert it (a ~1s lizard blip).
            steam_now = _steam_running()
            triton_state.set_steam_running(steam_now)
            triton_state.reset_session()
            # Fresh open cycle: clear the close-path restore flags so the
            # next close dispatches once (the on_close callback + finally
            # safety net re-arm per open).
            self._steam_layer_active = False
            self._steam_restore_dispatched = False
            triton_state.set_focus_restore_target(restore_hwnd)
            self._kbd_open = True
            self._refresh_menu()  # label → "Close Keyboard"
            # Steam keyboard layer: while the OSK is open the physical
            # controller is consumed by triton, so ask Steam Input to switch
            # to the "DualTouch Keyboard Layer" shortcut's controller config
            # (LB/RB muted, everything else default) via
            # steam://forceinputappid/<appid> — Steam Input switches live,
            # globally (desktop AND games), even while a game has focus. On
            # close, /0 restores auto-switching — but focus is put back on
            # the app FIRST: Steam re-evaluates the focused window on /0
            # and re-initializes Steam Input if the app isn't focused yet,
            # which makes the game unresponsive for a few seconds. The
            # switch is only requested when the Steam client is running;
            # restore always mirrors a successful force, so the muted
            # config can't stay stuck.
            steam_layer_active = False
            try:
                if steam_now and self.settings.get("steam_kbd_layer", True):
                    # Capture the Steam Input config appid active BEFORE we
                    # force the keyboard layer: restoring by forcing THIS appid
                    # back on close is applied instantly (unlike /0-auto),
                    # which removes the close-time helper hop + flash. Read
                    # from controller_ui.txt (the last "OnFocusWindowChanged
                    # ... AppID N" line) — the config Steam was using for the
                    # focused window before the layer took over.
                    steam_path_now = ssc.find_steam_path()
                    self._osk_restore_appid = (
                        _capture_active_appid(steam_path_now)
                        if steam_path_now
                        else None
                    )
                    appid = ssc.cached_appid()
                    if appid is None:
                        # Converge synchronously: one shortcuts.vdf read
                        # (~ms) catches an AppID Steam already assigned but
                        # the 5s background poll hasn't picked up yet (the
                        # typical first OSK open of a session). If the entry
                        # is still inside its placeholder-adoption grace,
                        # cached_appid() stays None and the OSK opens without
                        # the layer — the poll converges it seconds later.
                        with suppress(Exception):
                            ssc.verify_or_register()
                        appid = ssc.cached_appid()
                    if appid is None:
                        _log(
                            "steam input: OSK opened — no keyboard-layer "
                            "appid yet (registration in progress; it "
                            "converges within a few seconds via tray → "
                            "Register Steam Keyboard Layer, or at next "
                            "app start)"
                        )
                    elif not _verify_force_appid(steam_path_now, appid):
                        # Cached appid is stale/forged (attacker-editable
                        # state file) — never pin Steam Input to a dead
                        # config on its word alone (LOW-2).
                        _log(
                            f"steam input: cached appid {appid} not present in "
                            "shortcuts.vdf — skipping force (stale or "
                            "tampered state)"
                        )
                    elif ssc.force_appid(appid):
                        steam_layer_active = True
                        self._steam_layer_active = True
                        _log(
                            f"steam input: OSK opened — forced config "
                            f"appid {appid} (see steam_shortcut log for "
                            f"the dispatched URL)"
                        )
                    else:
                        _log(
                            f"steam input: forceinputappid dispatch failed "
                            f"for appid {appid} — see steam_shortcut log"
                        )
                # Per-app OSK look: make the CURRENT foreground app's
                # remembered size + skin (if it has any) the active ones, so
                # this open shows that app's look. Apps without an entry fall
                # back to the global osk_size / skin. Rebuilds the cached
                # Screen when the per-app size differs (size is baked in at
                # construction).
                self._apply_app_look()
                # Capture restore_hwnd in the closure: triton calls on_close
                # with no args, and the fallback hop path needs the target.
                triton_app.main(
                    cached_screen=self._cached_screen,
                    on_close=lambda _hwnd=restore_hwnd: (
                        self._dispatch_steam_restore(_hwnd)
                    ),
                )
            except Exception as e:
                print(f"triton crashed: {e!r}")
                # A windowed exe swallows the print; log it AND tell the user —
                # an open failure must never look like "nothing happened".
                _log(f"triton crashed: {e!r}\n{traceback.format_exc()}")
                self._notify("Keyboard failed to open", f"{e!r}")
            finally:
                # Restore auto-switching on normal close — but NOT on tray
                # Exit (force_appid(0)/force-back would make Steam Input
                # drop to a config where the trackpad stops working for
                # DualTouch, and the app is quitting anyway). The restore
                # is normally dispatched early by triton's on_close callback
                # (_dispatch_steam_restore, fired at close-detection so it
                # overlaps teardown); this finally is the safety net for
                # the case the callback never fired (e.g. triton crashed
                # before the loop ended).
                if (
                    steam_layer_active
                    and not self._stop_event.is_set()
                    and not self._steam_restore_dispatched
                ):
                    self._dispatch_steam_restore(restore_hwnd)
                self._kbd_open = False
                self._refresh_menu()  # label → "Open Keyboard"
                # A "Size" change was selected while the OSK was open (the
                # cached Screen was busy on this thread) — rebuild it now so
                # the new size takes effect on the next open.
                if self._pending_size_change:
                    self._pending_size_change = False
                    self._rebuild_cached_screen()
            time.sleep(0.1)
