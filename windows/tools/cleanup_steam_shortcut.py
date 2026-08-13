"""Clean up ALL DualTouch artifacts from Steam + appdata, so the non-Steam app
can be tested from scratch. ONLY touches DualTouch-owned data.

Removes:
1. The "DualTouch Keyboard Layer" entry from shortcuts.vdf (binary VDF).
2. Its library art in userdata/<id>/config/grid/ (grid <unsigned-appid>* files).
3. Its controller config folder (Steam Controller Configs/<id>/config/
   dualtouch keyboard layer/).
4. The Guide Button Chord dead-binding patch in config/443510/
   controller_triton.vdf (restored from the pre-patch backup, if one exists).
5. The steam_shortcut.json state file (appid cache, chord backup refs).

USAGE: close Steam first, then:  python tools/cleanup_steam_shortcut.py
"""

import glob
import os
import shutil
import sys

# Make windows/ importable (script lives in windows/tools/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import steam_shortcut as ssc
from applog import user_data_dir

APPNAME = ssc.SHORTCUT_APPNAME
UD_DIR = user_data_dir()

print("=== DualTouch Steam-shortcut cleanup ===")


def remove_shortcut_entry(steam_path):
    """Remove the DualTouch entry from shortcuts.vdf (binary), atomically,
    via the codebase's own write path (backup + validate + replace)."""
    if ssc.remove_shortcut(steam_path):
        print("  shortcuts.vdf: entry removed (Steam must be closed)")
    else:
        print("  shortcuts.vdf: write FAILED — entry NOT removed")


def restore_chord(steam_path):
    """Restore the pre-patch Guide Button Chord config from the state backup."""
    state = ssc.load_state()
    bak = state.get("chord_bak")
    original = state.get("chord_original")
    expected_sha = state.get("chord_sha")
    path = ssc.chord_config_path(steam_path)
    if path is None or not os.path.isfile(path):
        print("  chord config: not present — nothing to restore")
        return
    if bak and os.path.isfile(bak):
        # The .bak is the file as it was BEFORE DualTouch's patch; restore it.
        try:
            shutil.copy2(bak, path)
            print(f"  chord config: restored from {os.path.basename(bak)}")
            return
        except Exception as e:
            print(
                f"  chord config: backup restore failed ({e!r}) — "
                "trying slot restore"
            )
    if original and expected_sha:
        ok = ssc._restore_chord_slot(
            path,
            state.get("chord_blocked", "X"),
            original,
            backup=None,
            expected_sha=expected_sha,
        )
        print(
            "  chord config: slot restore "
            + ("OK" if ok else "FAILED — restore manually")
        )
    else:
        print(
            "  chord config: no backup/original recorded — leaving as-is "
            "(it contains the dead-binding patch; toggle the chord in the "
            "tray to re-patch over it, or restore controller_triton.vdf "
            "manually)"
        )


def _grid_unsigned_appid():
    """The unsigned 32-bit appid used in grid-art filenames, or None."""
    s = ssc.load_state()
    appid = s.get("appid")
    if appid is None:
        return None
    return str(int(appid) & 0xFFFFFFFF)


def cleanup():
    steam = ssc.find_steam_path()
    print(f"Steam at: {steam}")
    if not steam or not os.path.isdir(steam):
        print("Steam path not found — aborting")
        sys.exit(1)

    remove_shortcut_entry(steam)

    # Grid art: remove ONLY files matching the DualTouch unsigned appid.
    grid = ssc._grid_dir(steam)
    unsigned = _grid_unsigned_appid()
    if grid and os.path.isdir(grid) and unsigned:
        removed = 0
        for f in glob.glob(os.path.join(grid, unsigned + "*")):
            os.remove(f)
            removed += 1
            print(f"  grid: removed {os.path.basename(f)}")
        if removed == 0:
            print("  grid: no DualTouch art files")
    else:
        print("  grid: folder not found (or appid unknown)")

    # Controller config folder for the shortcut.
    cfg = ssc.default_controller_config_target(steam)
    if cfg:
        cfg_dir = os.path.dirname(cfg)
        if os.path.isdir(cfg_dir):
            shutil.rmtree(cfg_dir, ignore_errors=True)
            print(f"  controller config: removed {cfg_dir}")
        else:
            print("  controller config: folder not found")

    restore_chord(steam)

    # State file.
    state_path = os.path.join(UD_DIR, "steam_shortcut.json")
    if os.path.isfile(state_path):
        os.remove(state_path)
        print(f"  state file: removed {state_path}")
    else:
        print("  state file: not present")

    print(
        "=== done. Restart Steam and launch DualTouch to register fresh. ==="
    )


if __name__ == "__main__":
    cleanup()
