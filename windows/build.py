"""Build script for the portable DualTouch Steam Controller Keyboard EXE.

Run:
    python build.py

Produces `dist/DualTouch-windows.exe`. Uses the prebuilt
`data/images/app_icon.ico` (multi-resolution) directly as the EXE icon
and bundles the data/ folder as PyInstaller datas. The output is a
single-file, no-console exe suitable for dropping anywhere.
"""

import glob
import os
import shutil
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ENTRY = os.path.join("tray", "__main__.py")
OUTPUT_NAME = "DualTouch-windows"
# Paths are relative to PROJECT_DIR (the cwd PyInstaller runs in), so the
# regenerated .spec keeps relative paths and the build works from any checkout.
APP_ICON_ICO = os.path.join("data", "images", "app_icon.ico")
DATA_DIR = os.path.join("data")


def _check_icon():
    if not os.path.isfile(APP_ICON_ICO):
        raise SystemExit(f"app icon not found: {APP_ICON_ICO}")
    print(f"exe icon: {APP_ICON_ICO}")


def _run_pyinstaller():
    # `data;data` tells PyInstaller to drop the data/ folder into the bundle
    # rooted at "data". On Windows the separator in --add-data is ";".
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        # NOT --uac-admin: the exe must be able to run NON-elevated too, so the
        # same binary can act as the cursor helper (launched by a scheduled task
        # for the session-cursor hide/show, which requires normal integrity).
        # The tray self-elevates at runtime (ShellExecute runas) only for the
        # main UI; --cursor-helper mode stays de-elevated. See tray.py __main__
        # and cursor_ctrl.py.
        "--name",
        OUTPUT_NAME,
        "--icon",
        APP_ICON_ICO,
        "--add-data",
        f"{DATA_DIR};data",
        # pystray uses platform-specific backends loaded at runtime; pyinstaller
        # doesn't always pick them up unless we name them explicitly.
        "--hidden-import",
        "pystray._win32",
        "--hidden-import",
        "pynput.keyboard._win32",
        "--hidden-import",
        "pynput.mouse._win32",
        "--hidden-import",
        "PIL._tkinter_finder",
        # Our hand-rolled SDL3 binding is imported transitively (triton.screen
        # -> sdl3w); name it explicitly so PyInstaller always bundles it.
        "--hidden-import",
        "sdl3w",
    ]

    # sdl3w loads the vendored SDL3 DLLs at import time, searching
    # <bundle>/sdl3w/dll first (see sdl3w/_loader.py). Ship the pinned SDL3
    # family (SDL3.dll + SDL3_ttf.dll) into that same path inside the EXE.
    sdl_dll_dir = os.path.join("sdl3w", "dll")
    sdl_dlls = glob.glob(os.path.join(sdl_dll_dir, "*.dll"))
    if not sdl_dlls:
        raise SystemExit(f"no SDL3 DLLs found in {sdl_dll_dir}")
    for dll in sdl_dlls:
        cmd += ["--add-binary", f"{dll};sdl3w/dll"]

    cmd.append(ENTRY)
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


def _cleanup():
    # Trim PyInstaller's intermediate artifacts; keep dist/ and the .spec.
    build_dir = os.path.join(PROJECT_DIR, "build")
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir, ignore_errors=True)


def main(argv=None):
    _check_icon()
    _run_pyinstaller()
    _cleanup()
    out = os.path.join(PROJECT_DIR, "dist", f"{OUTPUT_NAME}.exe")
    print(f"\nbuilt: {out}")


if __name__ == "__main__":
    main()
