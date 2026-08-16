#!/usr/env/python3


import ctypes
import time
from collections import deque
from contextlib import suppress
from threading import Thread

import appsettings
import cursor_ctrl  # noqa: E402  (non-elevated cursor hide/show helper)
import sdl3w as S
import steamcontroller.uinput as sui

from triton import (
    config,
    controller,
    diacritics,
    screen,
    state,
    utils,
    vkb,
    vptr,
)
from triton.mousehook import (
    _mouse_highlight_allowed,
    _mouse_swallow,
    _recent_controller_input,
)
from triton.screen import CoordFraction, set_dims
from triton.win32 import (
    _IS_WINDOWS,
    _fg_desc,
    _focus_log,
    _foreground_exe_name,
    _hwnd_of,
    _is_start_menu_open,
    _make_window_non_activating,
    _reassert_topmost,
    _restore_foreground,
    _set_click_through,
    _show_window_noactivate,
    _user32,
)
from triton.windowpos import (
    _POS_UP_RIGHT,
    _apply_window_position,
    _cycle_window_position,
    _position_for_app,
    _position_index,
    _save_position_for_app,
)

# Main-loop pacing. The loop runs fast so SDL-pad input (polled/published here)
# and the resulting cursor steps / key presses are drained with minimal latency
# — matching the Steam Controller, which the input thread reads directly. The
# expensive render is throttled separately to the display rate.
_LOOP_SLEEP = 0.002  # ~500 Hz loop (cheap input work each iteration)
_RENDER_INTERVAL = 1.0 / 120  # render/hover-haptic cadence
# How often to re-check _is_start_menu_open() while the OSK is already visible,
# to live-reposition if the Start menu opens/closes underneath it. Cheap
# (GetForegroundWindow + a process-name lookup), but not free, so this is
# throttled well below _RENDER_INTERVAL — Start opening/closing is a
# human-timescale event.
_START_MENU_POLL_INTERVAL = 0.2

# OSK OPEN animation (see screen.render_open_anim + main's render loop), driven
# by a SINGLE underdamped spring (utils.spring_p) so the fade, bottom-cut reveal
# and downward settle all share one bouncy, frame-rate-independent curve:
#   • fade   = clamp(p,0,1)          — alpha never overshoots (max 100%);
#   • cut    = CUT_PX·(1−clamp(p,0,1)) — bottom reveals as the spring rises;
#   • settle = DROP_PX·(1−p)        — window, raised DROP_PX at start, drops
#     in and springs ≈1.6px past rest before settling (ζ<1) — the iOS feel.
# ζ=0.70 → ~4.6% overshoot (subtle, reads as a springy approach); ω0=20 → settles
# ~0.30s; total capped at _OPEN_ANIM_SECS.
_OPEN_ANIM_SECS = 0.34
_OPEN_ANIM_CUT_PX = 140
_OPEN_ANIM_DROP_PX = 35
_OPEN_ANIM_ZETA = 0.70
_OPEN_ANIM_OMEGA0 = 20.0

# OSK CLOSE animation: a quick spring reverse (fade 1->0 + slight scale-down),
# over _CLOSE_ANIM_SECS. Research: closes are faster and snappier than opens,
# and should NOT bounce (an overshooting exit reads as a glitch). The scale and
# fade follow a single eased curve. NO window movement during the close: moving
# the layered per-pixel-alpha window mid-fade makes DWM re-composite stale
# opaque content, which showed up as a flash on the bottom at the end.
_CLOSE_ANIM_SECS = 0.16


# Parking spot for the always-visible OSK window while "closed" (must match
# screen._OFFSCREEN_* — the window is created there and moved here on close).
_OFFSCREEN_X = -32000
_OFFSCREEN_Y = -32000


# How often the always-on foreground sentinel polls whether OUR OWN OSK
# window has become the foreground (see the main loop). Displacing our own
# window is never refused, so this can run at any time without flashing.
_OSK_SENTINEL_INTERVAL = 0.25


def _begin_open_anim(scr, virtual_kb, controller_state, rest):
    """Prime the OSK open animation: pre-render the invisible first frame (so the
    just-shown window never flashes its background), then raise the window by the
    settle distance so the animation can drop it back into place. Returns the
    monotonic start time, or None if the animation can't run (no display bounds
    or no GPU render target) — the caller then just shows the keyboard normally.

    `rest` is the resting (x, y) from _apply_window_position."""
    if rest is None:
        return None
    # Force the window into per-pixel-alpha (non-click-through) mode for the
    # animation: the fade/reveal composite shows the desktop through the
    # transparent/cut pixels, which a uniform layered-window alpha (set by
    # click-through) would override. The real click-through state is re-applied
    # the moment the animation ends (the caller resets clickthrough_on).
    _set_click_through(scr.window, False)
    pointers = controller_state.get_pointers()
    assert pointers is not None, "open anim runs after set_pointers publishes"
    # fade=0 + full cut => a fully transparent frame: the window is invisible the
    # instant it's shown, then the loop fades/reveals it in.
    if not scr.render_open_anim(virtual_kb, pointers, 0.0, _OPEN_ANIM_CUT_PX):
        return None
    rx, ry = rest
    S.SDL_SetWindowPosition(scr.window, rx, ry - _OPEN_ANIM_DROP_PX)
    return time.monotonic()


def load_kb_config():
    kb_config = vkb.VirtualKeyboardConfig()
    kb_layout_file = config.YamlFile("keyboard-layout.yaml")
    kb_layout_file.read()
    kb_layout_file.add_to_config("keys", kb_config)
    return kb_config


def _apply_remembered_position(exe_name):
    """Make the current foreground app's remembered OSK position (if any) the
    current one (_position_index[0]), so the open applies it and a subsequent
    Move cycles onward from it. For non-positionable foregrounds (None) and
    apps with no stored index the fallback rule is "keep the current spot"
    (down-mid on a fresh program start) — routed through _position_for_app,
    the single source of truth for that decision."""
    _position_index[0] = _position_for_app(
        exe_name, state.get_window_position_per_app()
    )


# Debounce the settings.json write from Move-cycle / jump persists (both run on
# the ~500 Hz main loop). The in-memory state is updated IMMEDIATELY so a
# re-open within the session picks the new position up; only the disk flush is
# throttled to at most once per _POSITION_PERSIST_INTERVAL, with a guaranteed
# final flush at OSK close (see _flush_position_persist).
_POSITION_PERSIST_INTERVAL = 1.0
_position_persist_dirty = False
_position_persist_at = 0.0


def _persist_position_for_app(index):
    """Record `index` (0-5) as the current foreground app's remembered OSK
    position - into the shared state immediately, and schedule the settings.json
    write (debounced, flushed at OSK close) so it survives restarts even if the
    tray's startup publish is stale. No-op for non-positionable foregrounds
    (None)."""
    global _position_persist_dirty
    exe_name = _foreground_exe_name()
    if exe_name is None:
        return
    idx = int(index) % 6
    per_app = state.get_window_position_per_app()
    if per_app.get(exe_name) == idx:
        return
    per_app = _save_position_for_app(exe_name, idx, per_app)
    state.set_window_position_per_app(per_app)
    _position_persist_dirty = True
    now = time.monotonic()
    if now - _position_persist_at >= _POSITION_PERSIST_INTERVAL:
        _flush_position_persist(now)


def _flush_position_persist(now=None):
    """Write the pending per-app position map to settings.json if it changed
    since the last flush. Called by _persist_position_for_app when the debounce
    interval elapses, and unconditionally at OSK close so no position is ever
    lost to the throttle. No-op when nothing is dirty."""
    global _position_persist_dirty, _position_persist_at
    if not _position_persist_dirty:
        return
    _position_persist_dirty = False
    _position_persist_at = now if now is not None else time.monotonic()
    try:
        settings = appsettings._load_settings()
        settings["window_position_per_app"] = dict(
            state.get_window_position_per_app()
        )
        appsettings._save_settings(settings)
    except Exception:
        pass


def main(cached_screen=None, on_close=None):
    """Run the OSK until it closes.

    `on_close` (optional callable) is invoked once, at the moment the
    OSK starts closing (right after the main loop exits, BEFORE the ~1.5s
    teardown: mouse-hook disarm, controller-thread join, focus restore).
    The tray uses it to dispatch the Steam Input force-back immediately,
    so the explorer.exe shell hop overlaps the teardown instead of running
    after it — the close-to-restore latency is cut by roughly the whole
    teardown length. Must not block (Popen dispatch only)."""
    # NOTE: _position_index is NOT reset here — the Move-key window position is
    # remembered across OSK opens within a session and only resets to down-mid
    # on a program restart (when this module is freshly imported). It's restored
    # below, after the window is shown.

    controller_state = controller.ControllerState()
    # click_queue is a CLASS attribute on ControllerState, so it survives OSK
    # reopen as one shared deque. Give this session a fresh one — a stale item
    # from a previous session (e.g. a dropped variant commit) must never leak
    # into the next open and interfere with typing.
    controller_state.click_queue = deque()
    controller_state.set_pointers(
        vptr.VirtualPointer(
            state.InputState.INACTIVE, CoordFraction(1 / 4, 1 / 2)
        ),
        vptr.VirtualPointer(
            state.InputState.INACTIVE, CoordFraction(3 / 4, 1 / 2)
        ),
    )

    virtual_kb = load_kb_config().construct()
    # Force OS Caps Lock OFF so the OSK types lowercase by default and Shift
    # alone controls capitalization. The OSK Caps key / L3 send KEY_CAPSLOCK,
    # which PowerToys (Caps->Esc remap) eats — so if caps is ON the OSK could
    # never turn it off and every key came out uppercase. force_caps_off sends
    # the raw Caps scancode (bypasses the remap) only when caps is currently ON.
    with suppress(Exception):
        vkb.kb.force_caps_off()
    # Repair: a throttled/dropped modifier key-up from a previous session can
    # leave Ctrl/Alt physically stuck on the OS, turning every keypress into a
    # shortcut (nothing types) — and it survives OSK reopen because it's OS
    # state, not app state. Force-release any stuck modifiers at open so a
    # fresh keyboard starts clean. (Shift is skipped: the user may legitimately
    # hold Shift via L2/latched state while typing.)
    with suppress(Exception):
        vkb.kb._release_stuck_ctrl()
    # Repair: the module-global pynput Keyboard (vkb.kb) survives OSK reopen,
    # and the OSK's raw SendInput paths (variant paste Ctrl+V, AltGr) don't
    # update pynput's INTERNAL modifier set. A stale internal Ctrl/Alt entry
    # makes every subsequent NORMAL letter (via pynput) type as if a modifier
    # were held — "nothing shows up", and physical keys can't clear internal
    # state. Wipe it at open so a fresh session starts clean.
    with suppress(Exception):
        vkb.kb.reset_modifier_state()
    # Publish the layout itself so the controller thread can resolve a pixel
    # position to a key cell (the pad press-lock glides to the key center).
    state.set_virtual_kb(virtual_kb)

    # SDL3: bring up video+events (rendering) and TTF (key labels) BEFORE
    # starting the input thread. We use SDL_InitSubSystem / SDL_QuitSubSystem
    # (not SDL_Init/SDL_Quit) because the tray's cached-screen path may share
    # this process — a full SDL_Quit on OSK close would tear the tray's SDL
    # down. Refcounted, so this balances the SDL_QuitSubSystem at teardown.
    # The custom Steam Controller HID driver runs on its own thread and is
    # independent of SDL.
    _owns_sdl = cached_screen is None
    if _owns_sdl:
        if not S.SDL_InitSubSystem(S.SDL_INIT_VIDEO | S.SDL_INIT_EVENTS):
            raise RuntimeError("SDL_InitSubSystem failed: " + S.get_error())
        if not S.TTF_Init():
            raise RuntimeError("TTF_Init failed: " + S.get_error())

    sc_thread = Thread(
        target=controller.input_thread, args=(controller_state,), daemon=True
    )
    sc_thread.start()
    # Steam keyboard sounds (key click / open chime / close chime) are owned
    # by THIS (main) thread — they are fired from vkb.dispatch_key and the
    # open/close teardown here. Registered on the main thread so the close
    # chime still has its hook when the controller thread exits (its finally
    # only clears the haptic hooks, not these). Cleared at the end of main().
    import key_sound

    state.set_key_sound(key_sound.play_key_sound)
    state.set_key_sound_open(key_sound.play_open_sound)
    state.set_key_sound_close(key_sound.play_close_sound)

    if cached_screen is not None:
        scr = cached_screen
        # Re-check the skin in case it changed while the OSK was closed.
        scr.maybe_reload_skin()
    else:
        scr = screen.Screen()
        _make_window_non_activating(scr.window)
    # Rebuild key geometry against the ACTUAL window dims BEFORE the first
    # render: virtual_kb was constructed above from the then-current module
    # screen.width/height (default 1286x369 on the very first open, or a
    # stale value on later opens), which may differ from the size Screen()
    # just resolved (fresh path) or the persisted size (cached path). Without
    # this, the open-animation's first frame (and any frame rendered before
    # the main loop's _resize_dirty gate) would draw keys at the wrong size —
    # a smaller keyboard with a black background that only a close/reopen
    # fixes.
    virtual_kb.update_dimensions()
    # Restore the remembered Move-key position (persists across opens within a
    # session, resets on program restart). At index 0 this is the default
    # down-mid spot; also re-applies after a display-layout change. Done BEFORE
    # showing so the open animation knows its resting target and the window
    # never flashes at the wrong spot. If the Start menu is open, force
    # up-right instead (without touching _position_index) so its
    # search-results panel doesn't cover the keyboard.
    start_menu_open = _is_start_menu_open()
    # Feature A: restore the CURRENT foreground app's remembered position (if
    # it has one) into _position_index, so the open applies it and a Move
    # cycles onward from it. Apps with no stored index keep the global default.
    _last_pos_app = _foreground_exe_name()
    _apply_remembered_position(_last_pos_app)
    # Always-visible mode: compute the resting spot WITHOUT moving the parked
    # off-screen window on-screen — _begin_open_anim renders the transparent
    # first frame while it's still off-screen, then moves it to the raised
    # spot. Moving the stale (closed) frame on-screen first would flash it.
    open_anim_rest = _apply_window_position(
        scr.window,
        _POS_UP_RIGHT if start_menu_open else None,
        move=not state.is_osk_always_visible(),
    )
    # Prime the open animation (pre-renders an invisible frame + raises the
    # window) before showing it, so the keyboard fades/rises in instead of
    # popping. open_anim_start is None if it can't run → plain instant show.
    open_anim_start = _begin_open_anim(
        scr, virtual_kb, controller_state, open_anim_rest
    )
    _show_window_noactivate(scr.window)
    # Steam keyboard open chime (deck_ui_show_modal.wav). Fires once per OSK
    # open; gated by the key-sound setting in state.key_sound_open(). The
    # in-loop re-show path (state.is_visible() -> _show_window_noactivate in
    # the main loop) is currently unreachable and intentionally does not
    # re-chime.
    state.key_sound_open()
    # Hide the interactive session's system cursors while the OSK is
    # visible (Steam Big Picture hides it by default; a plain SDL window
    # would show the arrow cursor as a flash). Done via the non-elevated
    # helper because this elevated process can't touch the session cursors.
    cursor_ctrl.set_osk_cursor_visible(False)
    # The OSK window is up and NOACTIVATE, but focus can still have moved on
    # the way in — restore it so typed keys land where the user was typing.
    # One-shot attempt right at open. If it fails, the re-assert loop below
    # stays disabled: every refused SetForegroundWindow flashes the target's
    # taskbar button, so we must never retry against a rejected activation.
    # The restore is self-guarding, so it runs UNCONDITIONALLY (even while
    # the Steam client runs): it refuses to fight a real foreground app
    # (incl. Steam's own windows) — no refused-activation flashes — and it
    # EXEMPTS our own process. That exemption matters: Windows reports the
    # OSK window as the foreground hwnd around the show despite
    # WS_EX_NOACTIVATE (observed: fg=our pid, class SDL_app, empty title —
    # the window recreated per open, so the hwnd changes every chord). When
    # that happens the restore is the only thing that hands the user's app
    # the keyboard back — without it, typed keys land on the OSK window and
    # vanish. (The mouse hook swallows Steam Input's injected clicks while
    # the OSK is up, and firmware lizard is suppressed during the opening
    # Steam hold — those can't steal focus at open; only the
    # OSK-as-foreground case needs the restore.)
    # Diagnostic: the foreground AT the one-shot restore (the open log below
    # samples ~1ms later — the two can differ, which is how the transient
    # fg=none state was first spotted).
    shot_fg = 0
    try:
        if _IS_WINDOWS:
            shot_fg = int(_user32().GetForegroundWindow())
    except Exception:
        pass
    focus_restore_failed = (
        _restore_foreground(state.get_focus_restore_target()) is False
    )
    # A late click can land AFTER the one-shot restore above: the firmware
    # lizard's mouse action on the opening press, Steam Input's injected
    # desktop click, or the tray-menu popup's focus return all race the OSK
    # opening. Re-assert the saved window briefly so the caret stays in the
    # user's text field while they start typing. Stops on the first refused
    # activation (see focus_restore_failed) or once the target is foreground
    # again; bounded so a deliberate post-open click can't be fought for long.
    # 1.0 s window: Steam Input's injected desktop click (and a firmware
    # lizard click when Steam is NOT running) can land up to ~0.7 s after the
    # open — the re-assert must outlive that. A refused activation against a
    # real app (incl. Steam's own windows) stops the loop at once — no
    # taskbar flashes.
    focus_restore_until = time.monotonic() + 1.0
    focus_restore_at = 0.0
    _focus_log(
        f"open fg={_fg_desc(_hwnd_of(scr.window) if _IS_WINDOWS else None)} shot_fg=0x{shot_fg:x} target={hex(state.get_focus_restore_target() or 0)}"
    )
    focus_diag_done = False
    was_visible = True
    # Last key under each touchpad pointer, for haptic "switched key" ticks.
    last_hover = [None, None]
    # Next time the held left mouse button re-fires its key (inf = not armed).
    # Holding the button over a repeatable key (Backspace / arrows) rubs out /
    # steps on the same cadence as the controller.
    mouse_repeat_at = float("inf")
    # Defer model (Feature B): the (row, col) of a variant-capable letter the
    # mouse pressed but did NOT type yet — the hold opens its variant row and
    # the mouse-up types the picked variant, or this deferred base letter if
    # none was picked (quick click). None = no deferred mouse press.
    mouse_deferred_cell = None
    # The mouse highlight only follows a REAL move: the first motion event after
    # the OSK opens is usually SDL reporting the cursor's position because the
    # window appeared under it — we record that as this anchor WITHOUT jumping
    # the highlight there. None = re-prime on the next open/show.
    mouse_anchor = None
    # Whether the OSK is currently click-through (mouse falls through to the app
    # behind). Mirrors the "Sticks Control Keyboard" setting inverted: when the
    # sticks/mouse should drive the DESKTOP (setting off), the OSK goes
    # click-through. None = unknown, force a (re)apply on the next visible frame.
    clickthrough_on = None
    # Select-mode via the MOUSE: holding the left button down on the on-screen
    # Select key holds Shift, and dragging the mouse left/right fires
    # Shift+Left/Right (text selection, like iOS hold-space). The anchor is the
    # window-relative x where the drag is measured from; each SELECT_DRAG_STEP
    # px of horizontal travel fires one arrow.
    mouse_select_active = False
    mouse_select_anchor = 0.0
    # Controller-mouse gate (Windows): the HWND of the OSK window, or None
    # off-Windows / if the lookup failed. While the OSK is visible, a low-level
    # hook swallows Steam Input's emulated mouse (the touchpad moving the real
    # cursor) so the cursor freezes where it was when the keyboard opened.
    # False = hook currently disarmed.
    osk_hwnd = _hwnd_of(scr.window) if _IS_WINDOWS else None
    hook_armed = False
    # Next time the (expensive) render + trackpad-hover-haptic pass runs. 0 =
    # render immediately on the first iteration. The loop itself runs faster so
    # input is processed with low latency; only rendering is throttled.
    next_render = 0.0
    # Next time to re-poll _is_start_menu_open() while visible (see
    # _START_MENU_POLL_INTERVAL). 0 = check on the first iteration too, though
    # start_menu_open above already matches reality so that check is a no-op.
    next_start_check = 0.0
    # Next time the always-on foreground sentinel polls (0 = immediately).
    osk_sentinel_at = 0.0

    # One reusable event struct polled each frame (SDL3 SDL_PollEvent fills it).
    ev = S.SDL_Event()

    # "Sticks Control Keyboard" (sc_left_stick_nav) is published by the tray
    # once at startup and never re-published mid-process, so it's static per
    # OSK session — cache it instead of a lock read every visible iteration.
    stick_nav_enabled = state.is_sc_kbd_stick_nav_enabled()
    # Split layout (tray "Steam Controller -> Split Keyboard") is published
    # live; the loop watches it and re-derives key geometry on change (see the
    # update_dimensions gate below).
    split_layout_was = state.is_split_layout_enabled()

    while not state.should_close():
        now = time.monotonic()
        # Bounded re-assert of the pre-open window (see the comment at
        # focus_restore_until): covers clicks that land after the one-shot
        # restore at open. No-op while the saved window already has focus;
        # a refused activation disables the loop — retrying would just keep
        # flashing the target's taskbar button.
        if (
            not focus_restore_failed
            and now < focus_restore_until
            and now >= focus_restore_at
        ):
            focus_restore_at = now + 0.12
            target = state.get_focus_restore_target()
            # Stop the loop only on a genuine refusal (False); None (no
            # foreground) means nothing was attempted and the OSK can still
            # become the foreground a moment later — keep retrying for it.
            if target is None or _restore_foreground(target) is False:
                focus_restore_failed = True  # stop the loop; no more flashes
        # Always-on sentinel: our own OSK window can become the foreground at
        # ANY time — Windows promotes the topmost window into a transiently-
        # NULL foreground (see the log's fg=none at open), or SDL3 calls
        # SetForegroundWindow on a mouse-down — possibly long after the
        # bounded re-assert above gave up. If the foreground IS our own
        # window, hand the keyboard back to the user's app. Displacing our
        # own window is never refused, so this never flashes; and
        # _restore_foreground's real-app refusal means it never fights a
        # deliberate click elsewhere. Cheap: one GetForegroundWindow per
        # poll, no-op unless the OSK itself holds the foreground.
        if osk_hwnd and now >= osk_sentinel_at:
            osk_sentinel_at = now + _OSK_SENTINEL_INTERVAL
            try:
                if int(_user32().GetForegroundWindow()) == osk_hwnd:
                    _restore_foreground(state.get_focus_restore_target())
            except Exception:
                pass
        if now >= focus_restore_until and not focus_diag_done:
            focus_diag_done = True
            _focus_log(
                f"settled fg={_fg_desc(_hwnd_of(scr.window) if _IS_WINDOWS else None)} restore_failed={focus_restore_failed}"
            )
        while S.SDL_PollEvent(ctypes.byref(ev)):
            et = ev.type
            if et == S.SDL_EVENT_QUIT:
                state.close()
                break
            if et == S.SDL_EVENT_WINDOW_RESIZED:
                screen.width = ev.window.data1
                screen.height = ev.window.data2
                set_dims(
                    screen.width, screen.height
                )  # keep geometry.py in sync
                # New key geometry must repaint even if the content signature
                # is otherwise unchanged (dirty-frame gate in Screen).
                scr._resize_dirty = True
            # Mouse control: hovering highlights the key under the pointer,
            # left-click presses it (the Shift key toggles latched Shift), and
            # the standard side buttons handle the keys you can't otherwise
            # reach mouse-only. Right-click = Shift, X1 (back) = Backspace,
            # X2 (forward) = Space. The OSK window is WS_EX_NOACTIVATE, so
            # clicking it never steals focus from the app being typed into.
            # (SDL3 reports mouse x/y as floats; find_key* handles that.)
            if et == S.SDL_EVENT_MOUSE_MOTION and state.is_visible():
                # Only move the highlight on a genuine mouse move (position
                # differs from the anchor). The first event after open just
                # records the anchor, so the OSK doesn't snap to the mouse.
                mpos = (ev.motion.x, ev.motion.y)
                if open_anim_start is not None:
                    # The open animation's settle phase repositions the window
                    # every frame, which makes a STATIONARY mouse's
                    # window-relative coords drift too — track the anchor but
                    # don't move the highlight, or it visibly chases the
                    # cursor while the keyboard slides into place.
                    pass
                elif (
                    mouse_anchor is not None
                    and mpos != mouse_anchor
                    and _mouse_highlight_allowed()
                ):
                    rc = virtual_kb.find_key_rc(*mpos)
                    if rc is not None:
                        state.set_cursor(*rc)
                mouse_anchor = mpos
                # Variant row (Feature B): the mouse over the open row
                # highlights the candidate under it. Only the real mouse drives
                # it (source "mouse"); a controller-held row is gated out by
                # _mouse_highlight_allowed (Steam's injected mouse would fight
                # the pad's highlight otherwise).
                if (
                    state.is_diacritic_open()
                    and state.get_diacritic_source() == "mouse"
                    and _mouse_highlight_allowed()
                ):
                    rect = state.get_diacritic_rect()
                    if rect is not None:
                        state.set_diacritic_index(
                            diacritics.variant_index_at_point(
                                rect,
                                mpos[0],
                                mpos[1],
                                state.get_diacritic_variant_count(),
                            )
                        )
                # While the mouse is in Select mode (left button held on the
                # Select key), horizontal drag fires Shift+Left/Right arrow taps
                # - text selection, like iOS hold-space. Each SELECT_DRAG_STEP px
                # of travel in one direction fires one arrow, then the anchor
                # moves with it so continued drag keeps selecting.
                if mouse_select_active and (
                    int(ev.motion.state) & S.SDL_BUTTON_LMASK
                ):
                    step = vkb.SELECT_MOUSE_DRAG_STEP
                    while abs(ev.motion.x - mouse_select_anchor) >= step:
                        if ev.motion.x > mouse_select_anchor:
                            vkb.kb.pressEvent([sui.Keys.KEY_RIGHT])
                            vkb.kb.releaseEvent([sui.Keys.KEY_RIGHT])
                            mouse_select_anchor += step
                        else:
                            vkb.kb.pressEvent([sui.Keys.KEY_LEFT])
                            vkb.kb.releaseEvent([sui.Keys.KEY_LEFT])
                            mouse_select_anchor -= step
                # If the left button isn't held anymore (e.g. it was released
                # off-window), drop any lingering press highlight and stop the
                # hold-to-repeat.
                if not (int(ev.motion.state) & S.SDL_BUTTON_LMASK):
                    state.set_mouse_press_cell(None)
                    mouse_repeat_at = float("inf")
                    if mouse_select_active:
                        mouse_select_active = False
                        vkb.kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
                        state.set_select_active(False)
                    # The button was released (possibly off-window): commit an
                    # open mouse-driven variant row, or type the deferred base
                    # (defer model), exactly like a normal mouse-up.
                    deferred = mouse_deferred_cell
                    mouse_deferred_cell = None
                    if (
                        state.is_diacritic_open()
                        and state.get_diacritic_source() == "mouse"
                    ):
                        char = state.get_diacritic_selected_char()
                        if char is not None:
                            vkb.commit_diacritic(char)
                        else:
                            # Same model as the pad/A button: while the row is
                            # open, only special letters are selectable — no
                            # pick defaults to the first variant, never the
                            # base letter.
                            variants = state.get_diacritic_variants_list()
                            if variants:
                                vkb.commit_diacritic(variants[0])
                            else:
                                state.close_diacritic()
                                if deferred is not None:
                                    # Deferred release: press edge already
                                    # clicked, so type the base SILENTLY.
                                    state.queue_key_press(
                                        *deferred, silent=True
                                    )
                    elif deferred is not None:
                        state.queue_key_press(*deferred, silent=True)
            if (
                et == S.SDL_EVENT_MOUSE_BUTTON_DOWN
                and state.is_visible()
                # While the controller is being used, clicks landing on the
                # OSK are Steam Input's injected duplicates of the pad click
                # / button press — ignore them so they can't press a random
                # key. (A real mouse click only comes when the controller is
                # idle, when this gate is open.)
                and not _recent_controller_input()
            ):
                btn = ev.button.button
                mouse_anchor = (
                    ev.button.x,
                    ev.button.y,
                )  # keep anchor in sync
                if btn == S.SDL_BUTTON_LEFT:
                    clicked_variant = None
                    if (
                        state.is_diacritic_open()
                        and state.get_diacritic_source() == "mouse"
                    ):
                        rect = state.get_diacritic_rect()
                        if rect is not None:
                            idx = diacritics.variant_index_at_point(
                                rect,
                                ev.button.x,
                                ev.button.y,
                                state.get_diacritic_variant_count(),
                            )
                            if idx >= 0:
                                clicked_variant = (
                                    state.get_diacritic_variants_list()[idx]
                                )
                    if clicked_variant is not None:
                        # A click over an open variant row commits the clicked
                        # candidate directly (and closes the row — the mouse-up
                        # that follows is then a no-op).
                        vkb.commit_diacritic(clicked_variant)
                    else:
                        rc = virtual_kb.find_key_rc(ev.button.x, ev.button.y)
                        if rc is not None:
                            key = virtual_kb.keys[rc[0]][rc[1]]
                            if key.is_select:
                                # Select key: enter select mode while the button
                                # is held — drag left/right to select text. Hold
                                # Shift now so the arrow taps select. No key
                                # press is queued (Select has no insert of its
                                # own).
                                mouse_select_active = True
                                mouse_select_anchor = ev.button.x
                                vkb.kb.pressEvent([sui.Keys.KEY_LEFTSHIFT])
                                state.set_select_active(True)
                            else:
                                state.set_cursor(*rc)
                                state.set_mouse_press_cell(rc)  # flash blue
                                # Defer model (Feature B): a press on a
                                # variant-capable letter types NOTHING here —
                                # the hold opens its variant row and the
                                # mouse-up picks base vs variant. Non-variant
                                # keys type at the press edge as before.
                                if vkb.diacritic_variants_for_key(key):
                                    mouse_deferred_cell = rc
                                    # Click sound at the PRESS edge like any
                                    # other key; the release types the base
                                    # silently (see the silent=True dispatch).
                                    state.key_sound_tick()
                                else:
                                    state.queue_key_press(*rc)
                                # Arm hold-to-repeat; the cur_visible block below
                                # only actually repeats it over a repeatable key.
                                mouse_repeat_at = now + vkb.KEY_REPEAT_DELAY
                elif btn == S.SDL_BUTTON_RIGHT:
                    vkb.toggle_shift()
                elif btn == S.SDL_BUTTON_X1:
                    vkb.tap_keycode(sui.Keys.KEY_BACKSPACE)
                elif btn == S.SDL_BUTTON_X2:
                    vkb.tap_keycode(sui.Keys.KEY_SPACE)
            if (
                et == S.SDL_EVENT_MOUSE_BUTTON_UP
                and ev.button.button == S.SDL_BUTTON_LEFT
            ):
                state.set_mouse_press_cell(None)
                mouse_repeat_at = float("inf")
                if mouse_select_active:
                    mouse_select_active = False
                    vkb.kb.releaseEvent([sui.Keys.KEY_LEFTSHIFT])
                    state.set_select_active(False)
                # Release: defer model (Feature B). If the hold opened a
                # variant row, commit the highlighted variant (the base was
                # never typed, so the commit types the variant directly). If
                # no variant was picked — or no row ever opened (quick click)
                # — type the deferred base letter instead.
                deferred = mouse_deferred_cell
                mouse_deferred_cell = None
                if (
                    state.is_diacritic_open()
                    and state.get_diacritic_source() == "mouse"
                ):
                    char = state.get_diacritic_selected_char()
                    if char is not None:
                        vkb.commit_diacritic(char)
                    else:
                        # Same model as the pad/A button: while the row is
                        # open, only special letters are selectable — no
                        # pick defaults to the first variant, never the
                        # base letter.
                        variants = state.get_diacritic_variants_list()
                        if variants:
                            vkb.commit_diacritic(variants[0])
                        else:
                            state.close_diacritic()
                            if deferred is not None:
                                # Deferred release: press edge already
                                # clicked, so type the base SILENTLY.
                                state.queue_key_press(*deferred, silent=True)
                elif deferred is not None:
                    state.queue_key_press(*deferred, silent=True)

        cur_visible = state.is_visible()
        if cur_visible != was_visible:
            if cur_visible:
                # Re-prime the open animation (position + invisible first frame +
                # raise) BEFORE showing, so a re-open fades/rises in like the first.
                # Same Start-menu up-right override as the initial open.
                start_menu_open = _is_start_menu_open()
                _last_pos_app = _foreground_exe_name()
                _apply_remembered_position(_last_pos_app)
                open_anim_rest = _apply_window_position(
                    scr.window,
                    _POS_UP_RIGHT if start_menu_open else None,
                    move=not state.is_osk_always_visible(),
                )
                open_anim_start = _begin_open_anim(
                    scr, virtual_kb, controller_state, open_anim_rest
                )
                _show_window_noactivate(scr.window)
                cursor_ctrl.set_osk_cursor_visible(False)
                # Re-prime so the open's spurious motion doesn't jump the cursor.
                mouse_anchor = None
                # Force the first visible frame through the dirty gate — a
                # re-open with an unchanged signature must still paint once.
                scr._last_sig = None
                # Showing resets the HWND ex-style baseline; re-apply below.
                clickthrough_on = None
            else:
                # Don't leave a mouse-latched Shift stuck down on the OS.
                vkb.release_shift()
                vkb.release_ctrl()
                vkb.release_alt()
                if state.is_osk_always_visible():
                    # Park the window off-screen instead of hiding it: hiding
                    # would force a ShowWindow transition (and its
                    # transiently-NULL foreground flash) on the next open.
                    S.SDL_SetWindowPosition(
                        scr.window, _OFFSCREEN_X, _OFFSCREEN_Y
                    )
                else:
                    S.SDL_HideWindow(scr.window)
                open_anim_start = None  # abort any in-flight open animation
            was_visible = cur_visible
        # Controller-mouse gate: while the OSK is visible, the low-level hook
        # swallows Steam Input's emulated mouse (the touchpad moving the real
        # cursor), freezing the cursor exactly where it was when the keyboard
        # opened. Deliberately NOT using ClipCursor: clipping a cursor that
        # sits outside the window rect snaps it INTO the rect — it would
        # "teleport to the keyboard", then freeze. The hook freezes it in
        # place instead. Armed the moment the window is visible (no wait for
        # the open animation), disarmed on hide/close and at teardown below.
        if cur_visible and not hook_armed:
            hook_armed = True
            if _IS_WINDOWS:
                _mouse_swallow.start()
        elif not cur_visible and hook_armed:
            hook_armed = False
            _mouse_swallow.stop()

        if cur_visible:
            # "Sticks Control Keyboard" OFF (sc_left_stick_nav) → the sticks/
            # mouse drive the desktop: make the OSK click-through so the mouse
            # falls through to the app behind. Re-checked every frame so a live
            # setting change takes effect at once. DEFERRED while the open
            # animation plays: click-through forces a uniform layered-window
            # alpha that overrides the per-pixel alpha the fade/reveal composite
            # relies on (it's applied the moment the animation ends,
            # clickthrough_on still None).
            want_clickthrough = not stick_nav_enabled
            if (
                open_anim_start is None
                and want_clickthrough != clickthrough_on
            ):
                _set_click_through(scr.window, want_clickthrough)
                clickthrough_on = want_clickthrough
            # Apply a tray-side skin change live (no-op unless it changed).
            scr.maybe_reload_skin()
            # Recompute key geometry ONLY when the window actually resized
            # (SDL_EVENT_WINDOW_RESIZED sets _resize_dirty; render clears it
            # after the frame). The layout math depends solely on
            # screen.width/height, which change nowhere else, so the ~500 Hz
            # per-iteration call was rebuilding ~90 identical key widths.
            if scr._resize_dirty:
                virtual_kb.update_dimensions()
            # Split layout toggled live from the tray: the window itself must
            # change width (split = full display width, plain = the size
            # submenu), so recompute the module dims, resize the SDL window,
            # re-settle it at its position spot, and re-derive the key
            # geometry. Same cache invalidation + render gate as a resize.
            if state.is_split_layout_enabled() != split_layout_was:
                split_layout_was = state.is_split_layout_enabled()
                w, h = screen.resize_for_layout()
                S.SDL_SetWindowSize(scr.window, w, h)
                _apply_window_position(scr.window)
                # The open/close animation renders to a cached offscreen
                # texture sized for the old dimensions — drop it so the next
                # anim target is built at the new size.
                scr._anim_target = None
                virtual_kb.update_dimensions()
                scr._resize_dirty = True
            # --- Input-driven work runs EVERY loop iteration (NOT gated by the
            # render rate), so cursor steps and key presses drain at low latency
            # (the SC's frames go straight to the input thread).
            # DPAD: while a variant row is open, left/right moves the
            # highlighted variant (the row owns the DPAD then); otherwise step
            # the cursor using the actual layout pixel positions.
            for direction, haptic in state.drain_dpad_queue():
                if state.is_diacritic_open():
                    if direction in ("LEFT", "RIGHT"):
                        state.set_diacritic_index(
                            diacritics.step_variant_index(
                                state.get_diacritic_index(),
                                1 if direction == "RIGHT" else -1,
                                state.get_diacritic_variant_count(),
                            )
                        )
                    continue
                vkb.step_cursor(virtual_kb, direction, haptic=haptic)
            vkb.process_click_queue(virtual_kb, controller_state.click_queue)
            # Mouse left-button hold-to-repeat: while held over a repeatable
            # key (Backspace / arrows), re-queue it on the shared cadence.
            # Queued before the drain so it dispatches this same frame.
            press_cell = state.get_mouse_press_cell()
            if press_cell is not None and now >= mouse_repeat_at:
                pr, pc = press_cell
                if not (
                    0 <= pr < len(virtual_kb.keys)
                    and 0 <= pc < len(virtual_kb.keys[pr])
                ):
                    mouse_repeat_at = float("inf")
                elif vkb.is_repeatable(virtual_kb.keys[pr][pc]):
                    state.queue_key_press(pr, pc, repeat=True)
                    mouse_repeat_at = now + vkb.KEY_REPEAT_INTERVAL
                elif vkb.diacritic_variants_for_key(virtual_kb.keys[pr][pc]):
                    # Hold-to-extend (Feature B): held over a letter with
                    # variants — open the row once; it stays until the button
                    # releases (mouse-up commits). The base already fired on
                    # the press edge.
                    vkb.open_diacritic_rc(virtual_kb, pr, pc, "mouse")
                    mouse_repeat_at = float("inf")
                else:
                    mouse_repeat_at = float("inf")
            # Key presses: fire the callback of the queued key. A repeat hit
            # (something held) only fires over a repeatable key (Backspace /
            # arrows), so holding rubs out / steps without machine-gunning
            # ordinary keys.
            for (
                row,
                col,
                is_repeat,
                is_silent,
            ) in state.drain_key_press_queue():
                if 0 <= row < len(virtual_kb.keys) and 0 <= col < len(
                    virtual_kb.keys[row]
                ):
                    key = virtual_kb.keys[row][col]
                    if is_repeat and not vkb.is_repeatable(key):
                        # Hold-to-extend (Feature B): a held A over a letter
                        # opens its variant row on the first repeat (the base
                        # already fired on the press edge); A-release commits.
                        if vkb.diacritic_variants_for_key(key):
                            vkb.open_diacritic_rc(virtual_kb, row, col, "a")
                        continue
                    # Tell dispatch_key this is an auto-repeat so the key-press
                    # sound doesn't machine-gun on held keys; a deferred
                    # release (base of a variant key typed on release) is
                    # silent — its press edge already clicked.
                    vkb._dispatch_is_repeat = is_repeat
                    vkb._dispatch_silent = is_silent
                    try:
                        vkb.dispatch_key(virtual_kb, key)
                    finally:
                        vkb._dispatch_is_repeat = False
                        vkb._dispatch_silent = False
            if state.take_position_cycle_request():
                _cycle_window_position(scr.window)
                _persist_position_for_app(_position_index[0])
                # Moving the window slides it under a stationary mouse, which
                # fires a spurious motion (new window-relative coords) — re-prime
                # so the highlight doesn't jump to the mouse after a Move.
                mouse_anchor = None
            req = state.take_window_position_request()
            if req is not None:
                _position_index[0] = req % 6
                _apply_window_position(scr.window)
                _persist_position_for_app(_position_index[0])
                mouse_anchor = None
            # The open-time check above only forces _POS_UP_RIGHT at the moment
            # the OSK becomes visible — if the Start menu opens or closes while
            # the OSK is ALREADY showing, live-reposition in response (instant
            # snap, like the Move key above) instead of leaving it stuck.
            if now >= next_start_check:
                next_start_check = now + _START_MENU_POLL_INTERVAL
                now_start_open = _is_start_menu_open()
                # Foreground app changed while the OSK is open: re-apply that
                # app's remembered position, if it has one (apps with no stored
                # index keep the current spot - the fallback rule). Skipped
                # while the Start menu is up; its close re-applies below.
                pos_exe = _foreground_exe_name()
                if pos_exe is not None and pos_exe != _last_pos_app:
                    _last_pos_app = pos_exe
                    if pos_exe in state.get_window_position_per_app():
                        _apply_remembered_position(pos_exe)
                        if not now_start_open:
                            _apply_window_position(scr.window)
                            mouse_anchor = None
                if now_start_open != start_menu_open:
                    start_menu_open = now_start_open
                    _apply_window_position(
                        scr.window, _POS_UP_RIGHT if start_menu_open else None
                    )
                    mouse_anchor = None
            # Render + trackpad-hover haptic are throttled to the display rate;
            # the cheap input work above already ran this iteration.
            if now >= next_render:
                next_render = now + _RENDER_INTERVAL
                pointers = controller_state.get_pointers()
                assert pointers is not None, (
                    "render loop only runs after set_pointers publishes"
                )
                if open_anim_start is not None:
                    # --- OSK OPEN animation frame ---
                    p = (now - open_anim_start) / _OPEN_ANIM_SECS
                    if p >= 1.0:
                        # Done: settle exactly at rest, then resume normal render.
                        if open_anim_rest is not None:
                            S.SDL_SetWindowPosition(
                                scr.window,
                                open_anim_rest[0],
                                open_anim_rest[1],
                            )
                        # The settle phase's burst of SDL_SetWindowPosition calls
                        # can cost the OSK its topmost z-order (see
                        # _reassert_topmost) — left unfixed, the window stays
                        # visible but stops receiving mouse input. Re-prime the
                        # anchor too, so any spurious motion the repositioning
                        # caused doesn't leave the highlight stuck mid-jump.
                        _reassert_topmost(scr.window)
                        mouse_anchor = None
                        open_anim_start = None
                        scr.render(virtual_kb, pointers)
                    else:
                        # One underdamped spring drives fade + reveal + settle
                        # together (frame-rate independent, time-based).
                        p_spring = utils.spring_p(
                            p * _OPEN_ANIM_SECS,
                            _OPEN_ANIM_ZETA,
                            _OPEN_ANIM_OMEGA0,
                        )
                        fade = utils.clamp(p_spring, 0.0, 1.0)
                        cut = _OPEN_ANIM_CUT_PX * (
                            1.0 - utils.clamp(p_spring, 0.0, 1.0)
                        )
                        # The window starts raised (set once in _begin_open_anim)
                        # and drops in with the spring — overshooting ~1.6px past
                        # rest before settling back (ζ<1) — the subtle bounce.
                        if open_anim_rest is not None:
                            rx, ry = open_anim_rest
                            S.SDL_SetWindowPosition(
                                scr.window,
                                rx,
                                int(
                                    round(
                                        ry
                                        - _OPEN_ANIM_DROP_PX * (1.0 - p_spring)
                                    )
                                ),
                            )
                        if not scr.render_open_anim(
                            virtual_kb, pointers, fade, cut
                        ):
                            open_anim_start = (
                                None  # target gone → stop animating
                            )
                else:
                    # Haptic tick when a touchpad pointer moves onto a different key
                    # (touchpad mode only — pointer is INACTIVE when not touching).
                    for i in (0, 1):
                        ptr = pointers[i]
                        if ptr.state != state.InputState.INACTIVE:
                            px, py = ptr.coord_frac.to_absolute()
                            # Expanded hit-target so the "switched key" tick
                            # matches what a click there would actually type.
                            hovered = virtual_kb.find_key_expanded(px, py)
                            if (
                                hovered is not None
                                and hovered is not last_hover[i]
                            ):
                                state.haptic_tick()
                                last_hover[i] = hovered
                        else:
                            last_hover[i] = None
                    # Dirty-frame gate: render+present only when the content
                    # would differ from the last presented frame. An idle
                    # keyboard therefore costs ~0 CPU/GPU inside a game (the
                    # old path full-redrew at 120 fps unconditionally).
                    if scr.content_changed(virtual_kb, pointers):
                        scr.render(virtual_kb, pointers)
        else:
            # Drain any clicks that fired while hidden so they don't pile up.
            controller_state.click_queue.clear()
            state.drain_key_press_queue()
            state.drain_dpad_queue()
        # Pace the loop fast (low input latency) without busy-spinning; rendering
        # is throttled separately above.
        time.sleep(_LOOP_SLEEP)

    # --- OSK CLOSE animation: quick spring reverse (fade + scale) ---
    # Played while the window is still visible, right before the hide/park in
    # the teardown. Fades the keyboard out over _CLOSE_ANIM_SECS with a gentle
    # scale-down, so closing feels as polished as opening. Falls back to an
    # instant hide if the animation can't render. The window is NOT moved
    # during the close (see the _CLOSE_ANIM_SECS comment).
    if scr.render_close_anim(
        virtual_kb, controller_state.get_pointers(), 1.0, 1.0
    ):
        _close_anim_start = time.monotonic()
        _close_next = _close_anim_start
        while True:
            _now = time.monotonic()
            _p = (_now - _close_anim_start) / _CLOSE_ANIM_SECS
            if _p >= 1.0:
                break
            # One eased curve drives fade + scale ("recede into the plate").
            _e = _p * _p * (3.0 - 2.0 * _p)  # smoothstep
            _fade = 1.0 - _e
            _scale = 1.0 - 0.08 * _e
            scr.render_close_anim(
                virtual_kb, controller_state.get_pointers(), _fade, _scale
            )
            # Pace at ~120 fps (same as the open render cadence), not a busy
            # loop — the close is 0.16s of rendered motion, not a spin.
            _close_next += 1.0 / 120.0
            _sleep = _close_next - time.monotonic()
            if _sleep > 0:
                time.sleep(_sleep)
        # Commit one fully-transparent frame so the hide/park below moves an
        # already alpha-0 window (a moved opaque-backed layered window would
        # flash during DWM re-composite).
        scr.render_close_anim(
            virtual_kb, controller_state.get_pointers(), 0.0, 0.92
        )

    try:
        # Fire the close callback BEFORE the teardown below: the tray's
        # Steam Input force-back dispatch (explorer.exe shell hop, ~1s) then
        # overlaps the teardown instead of waiting for it, cutting the
        # close-to-restore latency by roughly the teardown length.
        if on_close is not None:
            with suppress(Exception):
                on_close()
        # Guaranteed final write of any debounced per-app position change —
        # the interval may not have elapsed since the last Move, but the
        # position must survive the close.
        _flush_position_persist()
        # Disarm the controller-mouse hook before tearing down — the OSK is
        # closing and the cursor belongs to the desktop again.
        _mouse_swallow.stop()
        # Release a latched Shift before tearing down, so closing the keyboard
        # never leaves the OS with KEY_LEFTSHIFT held.
        vkb.release_shift()
        vkb.release_ctrl()
        vkb.release_alt()
        # Steam keyboard close chime (deck_ui_hide_modal.wav). Fired HERE, before
        # sc_thread.join, so it plays while the OSK is still visible and the
        # main-thread hook is registered. Gated by the key-sound setting in
        # state.key_sound_close(); hooks are cleared in the finally below.
        state.key_sound_close()
        # Give the controller thread up to 1 second to run its cleanup (sends
        # the enable-lizard packet before closing the HID handle). Without this
        # wait the daemon thread is killed before it can re-enable lizard mode.
        sc_thread.join(timeout=1.0)
        # Restore focus to the app's window BEFORE the OSK window goes away.
        # The OSK is WS_EX_NOACTIVATE so it never takes focus itself, yet its
        # presence still upsets Windows' foreground bookkeeping (the open-time
        # restore exists for the same reason): once the OSK window is hidden,
        # the foreground can fall into a NULL vacuum that nothing refills, and
        # the keyboard-layer /0 force then finds no focused app — Steam can't
        # auto-switch back and the game keeps the wrong config until the user
        # alt-tabs to re-activate it. Restoring while the window is still
        # visible uses the same conditions where the open-time restore succeeds;
        # retry while the foreground is NULL (transient), stop on refusal (a
        # deliberate alt-tab focus is respected) or success.
        restore_target = state.get_focus_restore_target()
        if restore_target:
            restore_result = None
            restore_deadline = time.monotonic() + 0.5
            while time.monotonic() < restore_deadline:
                restore_result = _restore_foreground(restore_target)
                if restore_result is not None:
                    break
                time.sleep(0.05)
            _focus_log(
                f"close-restore target=0x{restore_target:x} result={restore_result}"
            )
        if _owns_sdl:
            # First-session path: we own the SDL subsystems, so tear them down.
            # VIDEO/EVENTS' refcount hits zero here so the window is released.
            try:
                scr.destroy_textures()
                S.SDL_DestroyRenderer(scr.renderer)
                S.SDL_DestroyWindow(scr.window)
            except Exception:
                pass
            S.TTF_Quit()
            S.SDL_QuitSubSystem(S.SDL_INIT_VIDEO | S.SDL_INIT_EVENTS)
        else:
            # Cached-screen path: keep the window alive for the next open — SDL
            # subsystems remain up (tray owns them). Always-visible mode parks it
            # off-screen (never hide: a later ShowWindow would re-trigger the
            # transiently-NULL foreground flash); hidden mode just hides it.
            try:
                if state.is_osk_always_visible():
                    S.SDL_SetWindowPosition(
                        scr.window, _OFFSCREEN_X, _OFFSCREEN_Y
                    )
                else:
                    S.SDL_HideWindow(scr.window)
            except Exception:
                pass

    finally:
        # Drop the main-thread sound hooks so a stale callback is never
        # invoked after this session (the controller thread clears its own
        # haptic hooks separately).
        state.set_key_sound(None)
        state.set_key_sound_open(None)
        state.set_key_sound_close(None)
        # Restore the system cursors no matter how we got here (normal
        # close, exception in the loop, teardown failure). Done via the
        # non-elevated helper; idempotent so calling it when the cursor
        # was never hidden (or already restored) is a safe no-op.
        with suppress(Exception):
            cursor_ctrl.set_osk_cursor_visible(True)


if __name__ == "__main__":
    main()
