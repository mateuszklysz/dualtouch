# DualTouch

<p align="center">
  <img src="windows/data/images/icon.png" alt="DualTouch" width="220">
</p>

**A fast, native on-screen keyboard for the Steam Controller on Windows.**

![Platform](https://img.shields.io/badge/Platform-Windows%2010%2F11-0078d6?style=flat-square)
![License](https://img.shields.io/badge/License-LGPL--3.0-blue?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square)

DualTouch is a native SDL3 on-screen keyboard built around the idea of
Steam's touch keyboard, but with a lot more behind it. The controller is
read directly from the HID device — no web layer, no Steam UI stack — so it
opens fast, types immediately, and sits on top of the desktop, windowed
games, and fullscreen apps alike.

It is a Windows-only fork of [SteamlessKeyboard](https://github.com/PietPetGit/SteamlessKeyboard)
(originally from `archshift/triton`).

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Building](#building)
- [Troubleshooting](#troubleshooting)
- [Development Notes](#development-notes)
- [License](#license)

## Features

- **Global overlay** — draws above every app, not tied to Big Picture or a launcher.
- **Native SDL3 rendering** — low CPU/memory use; skins, transparency, and size selectable from the tray.
- **Steam Controller input** — two-finger trackpad typing with hit-target expansion and debounce, DPAD/stick navigation, auto-repeat on held keys, configurable insert control (bumpers or triggers), Lift-Off Typing (insert on finger release), hover-to-open accented variants while Lift-Off Typing is on (rest a finger on a key for ~1.2 s — the key fills in, then slide to an accent), six selectable key layouts (language boards inject their symbols as characters, so they type true on any Windows input language).
- **Steam Input coexistence** — reads the shared HID alongside Steam Input, switches to a keyboard layer via `steam://forceinputappid`, and restores your config on close.
- **Opens with a chord** — Steam + X/Y/A/B (configurable) opens the keyboard; Steam's own menu is suppressed via a Guide Button Chord dead-binding.
- **Per-app memory** — remembers the OSK size, skin, and position per foreground app.
- **Sounds & haptics** — Steam's click sound and rumble feedback, both toggleable.
- **Tray app** — battery status, open/close keyboard, autostart, chord selection, skin menu.
- **Elevated by design** — runs elevated so it can type into Big Picture and UIPI-protected games.

<p align="center">
  <img src="docs/preview.png" alt="DualTouch on-screen keyboard" width="90%">
</p>

## Requirements

- Windows 10 or 11 (64-bit)
- [Steam](https://store.steampowered.com), running, with Steam Input available — DualTouch waits for it on launch
- Python 3.10+ if running from source (not needed for the prebuilt exe)

## Quick Start

**Release build**

Run `DualTouch-windows.exe` from the extracted `DualTouch-windows`
folder (it will request elevation on start) — the whole folder ships
together, so keep all of its files side by side. The
`DualTouch-cursor-helper.exe` next to it is the same binary under its
cursor-helper name (so the two processes are distinguishable in Task
Manager); keep it too.

- Settings: `%APPDATA%\DualTouch\settings.json` (auto-created with defaults; a legacy file next to the exe is migrated automatically)
- Logs: `%APPDATA%\DualTouch\dualtouch.log`

**From source**

```sh
pip install -r requirements.txt
cd windows
python -m tray
```

## Configuration

Most settings are live-editable from the tray (Startup and Steam Controller
menus), or by hand-editing `settings.json` for finer control.

| Key | Default | Meaning |
| --- | --- | --- |
| `sc_osk_open_chord` | `"X"` | Button that opens the keyboard when held with Steam: `"X"`/`"Y"`/`"A"`/`"B"` |
| `sc_click_button` | `"L1/R1"` | Click insert per side: `"L1/R1"` (bumpers) or `"L2/R2"` (triggers) |
| `sc_pad_click_enter` | `false` | Trackpad press-click inserts the key under the pointer |
| `sc_pad_click_engage` | `2500` | Pad force that fires the pad-click insert |
| `sc_liftoff_enter` | `false` | Lift-Off Typing: insert the key under the pointer when the finger lifts off the pad (no click needed); also enables hover-to-open accented variants |
| `osk_layout` | `"QWERTY"` | Key layout: `"QWERTY"`/`"AZERTY"`/`"QWERTZ"`/`"Dvorak"`/`"Colemak"`/`"ABC"` (tray "Keyboard Layout"; applied at the next open) |
| `sc_left_stick_nav` | `true` | Sticks control the keyboard while it is open |
| `sc_osk_trigger_actuation` | `"default"` | L2/R2 actuation point: `"default"` or `"low"` |
| `skin` | `"Gruvbox"` | Keyboard skin: Steam OSK themes load from the Steam install; Gruvbox is the bundled original |
| `osk_size` | `"medium"` | `"small"`/`"medium"`/`"full"` |
| `osk_transparency` | `"off"` | `"off"`/`"low"`/`"medium"`/`"high"` |
| `osk_split_layout` | `false` | Split the keyboard into left/right halves with a middle gap; each touchpad covers its own half |
| `key_sound_enabled_sc` | `true` | Steam keyboard click sound |
| `rumble_enabled_sc` | `true` | Haptics on key clicks and trigger pulls |
| `steam_kbd_layer` | `true` | Dispatch the `forceinputappid` keyboard layer while open |
| `block_sc_hid` | `false` | Open the Steam Controller HID exclusively (Steam can't read it) |
| `start_with_windows` | `false` | Launch at logon (elevated scheduled task, no UAC prompt) |

## Building

```sh
cd windows
python build.py               # folder distribution
python build.py --installer   # + Windows installer (needs Inno Setup 6)
```

Output: `windows/dist/DualTouch-windows/` (a standalone folder
distribution — `DualTouch-windows.exe`, the `DualTouch-cursor-helper.exe`
copy, and everything they need; ship the whole folder together).

With `--installer`, an Inno Setup 6 setup is compiled to
`windows/dist/DualTouch-windows-setup-<version>.exe`
([Inno Setup](https://jrsoftware.org/isdl.php) must be installed or
`ISCC.exe` on PATH).

Releases are compiled with [Nuitka](https://nuitka.net): all Python is
turned into machine code, so the bundle contains no Python sources or
bytecode. The first build on a machine without a C compiler auto-downloads
MinGW and takes several minutes; later builds are faster.

## Troubleshooting

**Keyboard doesn't open**
Enable logging first (tray → Startup → Enable Logging), then check
`%APPDATA%\DualTouch\dualtouch.log`.

**Steam Input grabs the controller**
Confirm the keyboard-layer shortcut is registered (done automatically into
`shortcuts.vdf`) and that the keyboard opened with Steam running; cursor
containment and the injected-input gate keep Steam's emulated mouse out of
the keyboard.

**Controller dead after quitting**
Quitting from the tray never dispatches the `/0` restore; if the appid
changed another way, relaunch and open/close the keyboard once (or alt-tab)
to make Steam re-evaluate.

## Testing

The OSK has a headless UI-test harness (an input-pipeline simulator (OskSim)) that
drives the real input pipeline — controller frame parsing, the pad/click
state machines, hold-to-repeat cadence and the variant-row defer model —
with a virtual clock, recording every key/mouse injection at the OS
boundary. No display, Steam, or controller required:

```sh
cd windows
python -m pytest tests -q                 # whole suite (fast, headless)
python -m pytest tests/test_diacritic_hold.py -q   # one layer-2 module
```

Layers: pure state-machine tests (`test_nav_cursor`), synthetic-input
injection through `ControllerManager` + the shared drain loop
(`tests/osk_sim.py`, used by `test_typing_flow`, `test_diacritic_hold`,
`test_pad_pointer`). CI runs the same suite on every push via
`.github/workflows/test.yml` (JUnit XML artifact). Known OS-level
injection quirks on real hardware (pynput/SendInput desync) are out of
scope for this harness — those need a physical device.

## Development Notes

This project was built through AI-assisted development: a human set the
direction, reviewed changes, and handled testing, while AI coding agents
wrote the implementation. It's shared partly as a working keyboard overlay,
and partly as a real-world example of what that workflow can produce.

## License

GNU LGPL v3 — see [LICENSE](LICENSE).

**Assets.** Valve's OSK themes and controller glyphs load from the Steam
install at runtime; bundled graphics are DualTouch's own.
