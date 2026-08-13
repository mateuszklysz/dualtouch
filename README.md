# DualTouch

**A fast, native on-screen keyboard for the Steam Controller on Windows.**

![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078d6?style=flat-square)
![Version](https://img.shields.io/badge/Version-1.1.0-2ea44f?style=flat-square)
![License](https://img.shields.io/badge/License-LGPL--3.0-blue?style=flat-square)

DualTouch is a Windows-only fork of [SteamlessKeyboard](https://github.com/PietPetGit/SteamlessKeyboard)
(originally from `archshift/triton`) that renders Steam's touch keyboard as a
native SDL3 overlay. The controller is read straight off the HID device — no
web layer, no Steam UI stack — so it opens fast, types immediately, and sits
on top of the desktop, windowed games, and fullscreen apps alike.

> **Built with AI.** This project exists to test what agent-based software
> development can do — without it, this project would never have been
> created. A human drove the direction and did the testing; AI agents wrote
> the implementation.

## Features

- **Global overlay** — draws above every app, not tied to Big Picture or a launcher.
- **Native SDL3 rendering** — low CPU/memory use; skins, transparency, and size selectable from the tray.
- **Steam Controller input** — two-finger trackpad typing with hit-target expansion and debounce, DPAD/stick navigation, auto-repeat on held keys, configurable insert control (bumpers or triggers).
- **Steam Input coexistence** — reads the shared HID alongside Steam Input, switches to a keyboard layer via `steam://forceinputappid`, and restores your config on close.
- **Opens with a chord** — Steam + X/Y/A/B (configurable) opens the keyboard; Steam's own menu is suppressed via a Guide Button Chord dead-binding.
- **Per-app look** — remembers the OSK size, skin, and position per foreground app.
- **Sounds & haptics** — Steam's click sound and rumble feedback, both toggleable.
- **Single instance** — mutex guard prevents two copies from reading the controller.
- **Tray app** — battery status, open/close keyboard, autostart, chord selection, skin menu.
- **Runs elevated** — types into Big Picture and UIPI-protected games.

## Requirements

- Windows 10 or 11 (64-bit)
- [Steam](https://store.steampowered.com) — required; DualTouch waits for it and assumes Steam Input is present
- Python 3.10+ to run from source, or the prebuilt `DualTouch-windows.exe`

## Quick start

**Release build** — run `DualTouch-windows.exe` (requests elevation on start).
Settings live in `%APPDATA%\DualTouch\settings.json` (auto-created with defaults;
an older file next to the exe is migrated automatically). Diagnostics go to
`%APPDATA%\DualTouch\dualtouch.log`.

**From source:**

```sh
pip install -r requirements.txt
cd windows
python -m tray
```

## Configuration

Most settings are live-editable from the tray (Startup and Steam Controller
menus) and hand-editable in `settings.json` for fine tuning. Common keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `sc_osk_open_chord` | `"X"` | Button that opens the keyboard when held with Steam: `"X"`/`"Y"`/`"A"`/`"B"` |
| `sc_click_button` | `"L1/R1"` | Click insert per side: `"L1/R1"` (bumpers) or `"L2/R2"` (triggers) |
| `sc_pad_click_enter` | `false` | Trackpad press-click inserts the key under the pointer |
| `sc_pad_click_engage` | `2500` | Pad force that fires the pad-click insert |
| `sc_left_stick_nav` | `true` | Sticks control the keyboard while it is open |
| `sc_osk_trigger_actuation` | `"default"` | L2/R2 actuation point: `"default"` or `"low"` |
| `skin` | `"Gruvbox"` | Keyboard skin (a `.css` under `data/skins/`) |
| `osk_size` | `"medium"` | `"small"`/`"medium"`/`"full"` |
| `osk_transparency` | `"off"` | `"off"`/`"low"`/`"medium"`/`"high"` |
| `key_sound_enabled_sc` | `true` | Steam keyboard click sound |
| `rumble_enabled_sc` | `true` | Haptics on key clicks and trigger pulls |
| `steam_kbd_layer` | `true` | Dispatch the `forceinputappid` keyboard layer while open |
| `block_sc_hid` | `false` | Open the Steam Controller HID exclusively (Steam can't read it) |
| `start_with_windows` | `false` | Launch at logon (elevated scheduled task, no UAC prompt) |

## Building

```sh
cd windows
python build.py
```

Output: `windows/dist/DualTouch-windows.exe`.

## Troubleshooting

- **Keyboard doesn't open** — enable logging first (tray: Startup → Enable
  Logging), then check `%APPDATA%\DualTouch\dualtouch.log`.
- **Steam Input grabs the controller** — confirm the keyboard-layer shortcut is
  registered (done automatically into `shortcuts.vdf`) and that the keyboard
  opened with Steam running; cursor containment and the injected-input gate
  keep Steam's emulated mouse out of the keyboard.
- **Controller dead after quitting** — quitting from the tray never dispatches
  the `/0` restore; if the appid changed another way, relaunch and open/close
  the keyboard once (or alt-tab) to make Steam re-evaluate.

## License

GNU LGPL v3 — see [LICENSE](LICENSE).

**Third-party assets.** The keyboard skin themes and controller-button glyphs
under `data/skins/` and `data/images/glyphs/` are inherited from the upstream
SteamlessKeyboard fork and are Valve's on-screen-keyboard themes/artwork,
used for their intended purpose with the Steam Controller. The `Gruvbox` skin
is original. Sounds are played from the Steam install at runtime, not bundled.
