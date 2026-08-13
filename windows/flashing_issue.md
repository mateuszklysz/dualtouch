# Flashing issue: app window focuses out and in on OSK open/close

Status: **UNRESOLVED** — root cause partially understood, no fix found yet.
Last updated: 2026-08-10.

## What the user sees

When opening or closing the DualTouch OSK overlay, the app window the user is
in visually **loses and regains its active state** (title bar dims, then
brightens — "focusing out and in"). It is **NOT** a taskbar-button flash and
**NOT** a window flicker:

1. **Open**: one dim→brighten right when the OSK appears.
2. **Close**: one dim→brighten ~1 s AFTER the OSK has already hidden.
3. Fullscreen games don't show it; windowed/borderless apps do (their chrome
   repaints the inactive state).

## Environment

- Windows 11 Pro, build 26200.
- Steam running, Steam Input holding the Steam Controller.
- DualTouch tray app (elevated) + SDL3 OSK overlay window:
  `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW`, borderless, topmost,
  `SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN=0`, created hidden, shown with
  `ShowWindow(SW_SHOWNOACTIVATE)`.

## Mechanism (current understanding)

Both flashes are the **same dim→brighten pair** — the app's activation state
changes twice:

- **Open**: around the show the foreground goes transiently **NULL**
  (`fg=none` observed in `dualtouch.log`). With no foreground, the app
  deactivates (dims). Windows then promotes the topmost window into that NULL
  foreground — observed as the OSK itself (`fg_is_osk=True`) despite
  WS_EX_NOACTIVATE. The post-show `_restore_foreground` then hands the
  foreground back to the app (brightens). Net: dim + brighten = visible flash.
- **Close**: `steam://forceinputappid/0` is dispatched to restore Steam Input's
  auto config, but Steam only re-evaluates the active app on a **real
  window-activation change** (observed: no `OnFocusWindowChanged`, no config
  reload until a manual alt-tab). So ~1 s after close a hidden 1x1 helper
  window takes the foreground (app dims), holds it 30 ms, and hands it back
  (app brightens) — the manual alt-tab equivalent. Net: dim + brighten.

## Established facts / evidence

- At open, `GetForegroundWindow()` can return NULL; the topmost window gets
  promoted into that NULL foreground. (Log lines `open fg=... shot_fg=...`.)
- The OSK window itself appears as the foreground around the show despite
  `WS_EX_NOACTIVATE` (log: `fg_is_osk=True`), so the restore exists.
- `_restore_foreground` semantics: `True` = target is foreground; `False` =
  refused / real different app holds focus (caller must STOP — each refused
  SetForegroundWindow flashes); `None` = NULL foreground (retry, nothing
  attempted).
- The `/0` URL IS processed by Steam (`ExecuteSteamURL` in
  `logs/console_log.txt`) but Steam Input does NOT re-evaluate until an
  activation change — the nudge hop is required.
- A hop at the exact `/0` receipt re-evaluates against the still-forced state
  (Steam Input's force-removal lags the console receipt) → appid never returns
  to 0. The hop must be delayed (~1.0 s observed lag).
- Focus restore on close happens BEFORE the OSK window is hidden, so the
  keyboard-layer `/0` finds a focused app.

## What was tried and did NOT work

| # | Change | Result |
|---|--------|--------|
| 1 | Original (v0.1.1 era): close dispatched `/0` but no activation hop at all | appid never returns to 0 until manual alt-tab |
| 2 | Two focus hops 0.4 s apart after `/0` | appid restores, but window "flashes two times" |
| 3 | Single hop right at `/0` console receipt | hop re-evaluates against still-forced state — appid stays forced ("you broke it") |
| 4 | Single hop DELAYED 1.0 s after `/0` receipt | appid restores reliably, one visible dim→brighten ~1 s after close remains |
| 5 | Hand-back verification loop (helper→check→app, ~150 ms unpress window) | lengthened the visible unpress — removed again |
| 6 | Hop register sleep 0.05 s → 0.03 s, no verification | still visible |
| 7 | **Open flash**: sink OSK to `HWND_BOTTOM` before `ShowWindow` (so the NULL-foreground promotion can't pick the OSK) | still flashes — the NULL foreground itself deactivates the app regardless of what gets promoted |
| 8 | **Open flash**: removed `WS_EX_TOPMOST` at window creation so it can sink | still flashes |
| 9 | Force topmost immediately after show (invisible, first frame fully transparent) | still flashes |

Conclusion so far: no amount of *timing/ordering* of the hop removes the close
flash (Steam requires a real activation change, which deactivates the app by
definition), and the z-order sink can't fix the open flash because the NULL
foreground deactivates the app even when nothing visible takes its place.

## Open questions (for external-AI research)

1. Why does showing a WS_EX_NOACTIVATE window make the foreground NULL at all,
   and is there ANY show sequence that leaves the foreground state completely
   untouched (so the app never deactivates at open)?
2. Can Windows promote a WS_EX_NOACTIVATE window into a NULL foreground?
3. How does Steam Input detect the active-window change (hook vs. polling
   interval)? Is there an invisible trigger (refused activation attempt, shell
   event, re-dispatch)?
4. Shortest helper-hold Steam reliably observes — can it be < ~8 ms so DWM
   never presents the inactive frame?
5. Is there a hop target whose takeover does NOT deactivate the app (same-
   process window of the app, another desktop, borderless fullscreen)?
6. Would WS_EX_NOACTIVATE on the helper prevent the app's deactivation while
   still registering a foreground change for Steam?

## Where the code lives

- Open flow / restore / sentinel: `windows/triton/triton.py` —
  `_show_window_noactivate` (~line 469), `_restore_foreground` (~line 643),
  open sequence in `main()` (~lines 900-1037), close-restore (~line 1300).
- Close nudge hop: `windows/win_focus.py` — `_nudge_focus_hop` (~line 122),
  `_nudge_after_restore` (~line 226).
- Close path dispatch: `windows/tray.py` (~lines 925-951).
- Evidence log: `windows/dist/dualtouch.log` (lines `open fg=...`,
  `settled fg=...`, `close-restore target=...`).
