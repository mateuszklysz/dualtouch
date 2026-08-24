"""Build script for the DualTouch release binaries (Nuitka-compiled).

Run:
    python build.py               # folder distribution
    python build.py --installer   # + Inno Setup installer (needs ISCC)

Produces `dist/DualTouch-windows/` — a standalone folder distribution
whose entry point is `dist/DualTouch-windows/DualTouch-windows.exe`,
plus a `DualTouch-cursor-helper.exe` copy of the same compiled program.
Task Manager labels processes by their exe's FileDescription, so the
copy carries its own ("DualTouch Cursor Helper") — without that, the
tray and the cursor-helper daemon are indistinguishable there. Ship
the whole folder together (zip it as-is).

All Python is compiled to machine code by Nuitka: the release contains
no .py sources and no .pyc bytecode, only native binaries plus data.

The first build on a machine without MSVC auto-downloads a MinGW
toolchain (--assume-yes-for-downloads) and takes several minutes; later
builds are faster.
"""

import ctypes
import glob
import os
import re
import shutil
import struct
import subprocess
import sys
from ctypes import wintypes

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# Compile the package itself (never tray/__main__.py) in -m package mode,
# so the frozen program runs exactly like the documented `python -m tray`.
ENTRY = "tray"
OUTPUT_NAME = "DualTouch-windows"
HELPER_EXE_NAME = "DualTouch-cursor-helper.exe"
DIST_DIR = os.path.join(PROJECT_DIR, "dist")
OUT_DIR = os.path.join(DIST_DIR, OUTPUT_NAME)
APP_ICON_ICO = os.path.join("data", "images", "app_icon.ico")

MAIN_FILE_DESCRIPTION = "DualTouch Steam Controller Keyboard"
HELPER_FILE_DESCRIPTION = "DualTouch Cursor Helper"

INSTALLER_DIR = os.path.join(PROJECT_DIR, "installer")
ISS_PATH = os.path.join(INSTALLER_DIR, "DualTouch.iss")


def _latest_git_tag():
    """Latest tag reachable from HEAD ('' when unavailable), v-prefix kept."""
    try:
        return subprocess.check_output(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=PROJECT_DIR,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def _app_version():
    """The exe/installer version. Precedence: the DUALTOUCH_VERSION env var
    (CI forwards the pushed tag's ref name there), then the latest reachable
    git tag; "latest" when neither yields a version-looking string (untagged
    local builds)."""
    for candidate in (os.environ.get("DUALTOUCH_VERSION"), _latest_git_tag()):
        m = re.match(r"v?(\d[\w.\-]*)", (candidate or "").strip())
        if m:
            return m.group(1)
    return "latest"


def _preflight():
    try:
        import nuitka  # noqa: F401
    except ImportError as e:
        raise SystemExit(
            "nuitka missing — run: pip install -r requirements.txt"
        ) from e
    if not os.path.isfile(APP_ICON_ICO):
        raise SystemExit(f"app icon not found: {APP_ICON_ICO}")
    sdl_dll_dir = os.path.join("sdl3w", "dll")
    if not glob.glob(os.path.join(sdl_dll_dir, "*.dll")):
        raise SystemExit(f"no SDL3 DLLs found in {sdl_dll_dir}")
    print(f"version: {_app_version()}")
    print(f"exe icon: {APP_ICON_ICO}")


def _clean_previous():
    for stale in (
        [OUT_DIR]
        + glob.glob(os.path.join(DIST_DIR, "*.dist"))
        + glob.glob(os.path.join(DIST_DIR, "*.build"))
    ):
        if os.path.isdir(stale):
            shutil.rmtree(stale, ignore_errors=True)
    # Legacy single-file exe from the old PyInstaller --onefile builds.
    legacy_exe = os.path.join(DIST_DIR, f"{OUTPUT_NAME}.exe")
    if os.path.isfile(legacy_exe):
        os.remove(legacy_exe)


# --- helper-exe identity ----------------------------------------------------
# Task Manager displays a process under its exe's FileDescription, falling
# back to the file name only when none exists. Nuitka stamps every binary
# with the same description, so the cursor-helper copy gets its version
# resource rewritten here (Begin/Update/EndUpdateResource) to keep the two
# processes distinguishable.


def _ver_block(key, value=b"", vtype=0, children=b""):
    """One VS_VERSIONINFO-style block, lengths patched in after layout."""
    buf = bytearray(struct.pack("<HHH", 0, len(value), vtype))
    buf += (key + "\x00").encode("utf-16-le")
    buf += b"\x00" * (-len(buf) % 4)
    buf += value
    buf += children
    buf += b"\x00" * (-len(buf) % 4)
    struct.pack_into("<H", buf, 0, len(buf))
    return bytes(buf)


def _fixed_file_info(version):
    parts = [
        int(p) if p.isdigit() else 0
        for p in (version.split(".") + ["0"] * 4)[:4]
    ]
    ms_hi, ms_lo, ls_hi, ls_lo = parts
    return struct.pack(
        "<IIIIIIIIIIIII",
        0xFEEF04BD,  # dwSignature
        0x00010000,  # dwStrucVersion
        (ms_hi << 16) | ms_lo,
        (ls_hi << 16) | ls_lo,
        (ms_hi << 16) | ms_lo,  # product version mirrors file version
        (ls_hi << 16) | ls_lo,
        0x0000003F,  # dwFileFlagsMask
        0x00000000,  # dwFileFlags
        0x00040004,  # VOS_NT_WINDOWS32
        1,  # VFT_APP
        0,
        0,
        0,
    )


_STANDARD_STRING_KEYS = (
    "CompanyName",
    "FileDescription",
    "FileVersion",
    "InternalName",
    "LegalCopyright",
    "OriginalFilename",
    "ProductName",
    "ProductVersion",
)


def _read_version_strings(path):
    """Existing VERSIONINFO strings + translation pair of an exe, so the
    rewrite only overrides what it must and preserves the rest."""
    ver = ctypes.WinDLL("version")
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}, (0x0409, 0x04B0)
    data = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, data):
        return {}, (0x0409, 0x04B0)

    ptr = ctypes.c_void_p()
    u16 = ctypes.c_uint()
    base = ctypes.addressof(data)
    if (
        not ver.VerQueryValueW(
            data,
            "\\VarFileInfo\\Translation",
            ctypes.byref(ptr),
            ctypes.byref(u16),
        )
        or u16.value < 4
        or ptr.value is None
    ):
        return {}, (0x0409, 0x04B0)
    langid, codepage = struct.unpack_from("<HH", data, ptr.value - base)

    strings = {}
    for key in _STANDARD_STRING_KEYS:
        if not ver.VerQueryValueW(
            data,
            f"\\StringFileInfo\\{langid:04x}{codepage:04x}\\{key}",
            ctypes.byref(ptr),
            ctypes.byref(u16),
        ):
            continue
        if ptr.value:
            strings[key] = ctypes.wstring_at(ptr.value)
    return strings, (langid, codepage)


def _serialize_version_info(strings, translation, version):
    langid, codepage = translation
    table_key = f"{langid:04x}{codepage:04x}"
    strings = {
        **{key: "" for key in _STANDARD_STRING_KEYS},
        **strings,
        "FileVersion": version,
        "ProductVersion": version,
    }
    string_blocks = b"".join(
        _ver_block(key, value=(text + "\x00").encode("utf-16-le"), vtype=1)
        for key, text in strings.items()
    )
    table = _ver_block(table_key, vtype=1, children=string_blocks)
    sfi = _ver_block("StringFileInfo", vtype=1, children=table)
    # Without a Translation entry consumers can't locate the string table
    # and report every string as empty.
    trans = _ver_block(
        "Translation", value=struct.pack("<HH", langid, codepage), vtype=0
    )
    vfi = _ver_block("VarFileInfo", vtype=1, children=trans)
    root = _ver_block(
        "VS_VERSION_INFO",
        value=_fixed_file_info(version),
        vtype=0,
        children=sfi + vfi,
    )
    return root


_ENUMRESNAMEPROCW = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMODULE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_ssize_t,
)
_ENUMRESLANGPROCW = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMODULE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    wintypes.WORD,
    ctypes.c_ssize_t,
)


def _enum_version_resources(path):
    """(name, language) pairs of existing RT_VERSION resources. Names are
    INTRESOURCE atoms (< 0x10000); anything else is skipped."""
    k32 = ctypes.windll.kernel32
    k32.LoadLibraryExW.restype = wintypes.HMODULE
    k32.LoadLibraryExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    k32.EnumResourceNamesW.argtypes = [
        wintypes.HMODULE,
        ctypes.c_void_p,
        _ENUMRESNAMEPROCW,
        ctypes.c_ssize_t,
    ]
    k32.EnumResourceLanguagesW.argtypes = [
        wintypes.HMODULE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        _ENUMRESLANGPROCW,
        ctypes.c_ssize_t,
    ]
    k32.FreeLibrary.argtypes = [wintypes.HMODULE]
    LOAD_LIBRARY_AS_DATAFILE = 0x00000002
    hmod = k32.LoadLibraryExW(path, None, LOAD_LIBRARY_AS_DATAFILE)
    if not hmod:
        raise OSError(
            f"LoadLibraryExW failed on {path} (err {ctypes.GetLastError()})"
        )
    names, targets = [], []

    def on_name(_hmod, _type, name, _lparam):
        if name and name < 0x10000:
            names.append(name)
        return True

    def on_lang(_hmod, _type, name, lang, _lparam):
        if name and name < 0x10000:
            targets.append((name, lang))
        return True

    try:
        cb_name = _ENUMRESNAMEPROCW(on_name)
        if not k32.EnumResourceNamesW(hmod, ctypes.c_void_p(16), cb_name, 0):
            raise OSError(
                f"EnumResourceNamesW failed (err {ctypes.GetLastError()})"
            )
        cb_lang = _ENUMRESLANGPROCW(on_lang)
        for res_id in names:
            k32.EnumResourceLanguagesW(
                hmod, ctypes.c_void_p(16), ctypes.c_void_p(res_id), cb_lang, 0
            )
    finally:
        k32.FreeLibrary(hmod)
    return targets


def _replace_version_resource(path, blob):
    k32 = ctypes.windll.kernel32
    k32.BeginUpdateResourceW.restype = wintypes.HANDLE
    k32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    k32.UpdateResourceW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.WORD,
        ctypes.c_char_p,
        wintypes.DWORD,
    ]
    k32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    targets = _enum_version_resources(path) or [(1, 0x0409)]
    hupd = k32.BeginUpdateResourceW(path, False)
    if not hupd:
        raise OSError(
            f"BeginUpdateResourceW failed on {path} (err {ctypes.GetLastError()})"
        )
    ok = True
    for res_id, lang in targets:
        ok &= bool(
            k32.UpdateResourceW(
                hupd,
                ctypes.c_void_p(16),  # RT_VERSION
                ctypes.c_void_p(res_id),
                lang,
                blob,
                len(blob),
            )
        )
    if not k32.EndUpdateResourceW(hupd, not ok):
        raise OSError(
            f"EndUpdateResourceW failed on {path} (err {ctypes.GetLastError()})"
        )
    if not ok:
        raise OSError(f"UpdateResourceW failed while rewriting {path}")


def _set_file_description(
    exe_path, file_description, original_filename, internal_name
):
    strings, translation = _read_version_strings(exe_path)
    version = strings.get("FileVersion") or _app_version()
    strings.update(
        FileDescription=file_description,
        OriginalFilename=original_filename,
        InternalName=internal_name,
    )
    blob = _serialize_version_info(strings, translation, version)
    _replace_version_resource(exe_path, blob)


def _run_nuitka():
    version = _app_version()
    # data;data-style resources land inside the dist folder next to the exe;
    # sdl3w loads its DLLs from <bundle>/sdl3w/dll (see sdl3w/_loader.py).
    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        # Auto-fetch MinGW when no C compiler exists (first local build).
        "--assume-yes-for-downloads",
        "--windows-console-mode=disable",
        # No UAC manifest on purpose: the same binary must run NON-elevated as
        # the cursor helper (scheduled task); the tray self-elevates at runtime.
        f"--windows-icon-from-ico={APP_ICON_ICO}",
        "--windows-product-name=DualTouch",
        f"--windows-product-version={version}",
        f"--windows-file-version={version}",
        f"--windows-file-description={MAIN_FILE_DESCRIPTION}",
        f"--output-filename={OUTPUT_NAME}.exe",
        f"--output-dir={DIST_DIR}",
        "--include-data-dir=data=data",
        # --include-data-dir silently skips .dll files (Nuitka classifies them
        # as code), so name the vendored SDL3 DLLs with the file-pattern form,
        # which does not suffix-filter.
        "--include-data-files=sdl3w/dll/*.dll=sdl3w/dll/",
        # pystray/pynput pick platform backends at runtime; name them so the
        # static analysis always keeps them (same list the PyInstaller build used).
        "--include-module=pystray._win32",
        "--include-module=pynput.keyboard._win32",
        "--include-module=pynput.mouse._win32",
        "--include-module=PIL._tkinter_finder",
        "--include-package=sdl3w",
        # Our local `triton` package collides with the ML 'triton' PyPI
        # package in anti-bloat's eyes ("undesirable import"); allow it so
        # every import of our own code compiles without warnings.
        "--noinclude-custom-mode=triton:allow",
        "--python-flag=-m",
        ENTRY,
    ]
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=PROJECT_DIR)


def _assemble_output():
    dist_dirs = glob.glob(os.path.join(DIST_DIR, "*.dist"))
    if len(dist_dirs) != 1:
        raise SystemExit(
            f"expected exactly one Nuitka .dist folder, got {dist_dirs}"
        )
    if os.path.exists(OUT_DIR):
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    os.rename(dist_dirs[0], OUT_DIR)
    # Drop Nuitka's intermediate C-code build tree next to the dist folder.
    for build_dir in glob.glob(os.path.join(DIST_DIR, "*.build")):
        shutil.rmtree(build_dir, ignore_errors=True)

    out_exe = os.path.join(OUT_DIR, f"{OUTPUT_NAME}.exe")
    if not os.path.isfile(out_exe):
        raise SystemExit(f"expected output missing: {out_exe}")
    for marker in (
        os.path.join(OUT_DIR, "data", "images"),
        os.path.join(OUT_DIR, "sdl3w", "dll", "SDL3.dll"),
    ):
        if not os.path.exists(marker):
            raise SystemExit(f"bundled resource missing: {marker}")
    # Same compiled program under the cursor-helper name, but with its own
    # version resource so Task Manager shows "DualTouch Cursor Helper"
    # instead of the tray's description for both processes.
    helper_exe = os.path.join(OUT_DIR, HELPER_EXE_NAME)
    shutil.copyfile(out_exe, helper_exe)
    _set_file_description(
        helper_exe,
        file_description=HELPER_FILE_DESCRIPTION,
        original_filename=HELPER_EXE_NAME,
        internal_name=HELPER_FILE_DESCRIPTION,
    )

    print(f"\nbuilt: {out_exe}")
    print(f"helper copy: {helper_exe}")
    print(f"ship the whole folder: dist/{OUTPUT_NAME}/")


def _find_iscc():
    """Locate Inno Setup 6's ISCC.exe without hardcoded paths: PATH first,
    then the standard per-machine and per-user install roots from the
    environment."""
    found = shutil.which("iscc")
    if found:
        return found
    candidates = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            candidates.append(os.path.join(base, "Inno Setup 6", "ISCC.exe"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        # Per-user installs land under %LOCALAPPDATA%\Programs.
        candidates.append(
            os.path.join(local, "Programs", "Inno Setup 6", "ISCC.exe")
        )
        candidates.append(os.path.join(local, "Inno Setup 6", "ISCC.exe"))
    return next((c for c in candidates if os.path.isfile(c)), None)


def _build_installer():
    """Compile installer/DualTouch.iss with Inno Setup 6 (ISCC), passing the
    git-tag version through /DAPP_VERSION for the setup's version metadata."""
    iscc = _find_iscc()
    if not iscc:
        raise SystemExit(
            "--installer: Inno Setup 6 not found — install it from "
            "https://jrsoftware.org/isdl.php (or add ISCC.exe to PATH)"
        )
    version = _app_version()
    subprocess.run(
        [iscc, f"/DAPP_VERSION={version}", "/Q", ISS_PATH],
        cwd=PROJECT_DIR,
        check=True,
    )
    out = os.path.join(DIST_DIR, f"DualTouch-windows-setup-{version}.exe")
    if not os.path.isfile(out):
        raise SystemExit(f"installer missing after ISCC run: {out}")
    print(f"installer: {out}")


def main(argv=None):
    want_installer = "--installer" in (sys.argv[1:] if argv is None else argv)
    _preflight()
    _clean_previous()
    _run_nuitka()
    _assemble_output()
    if want_installer:
        _build_installer()


if __name__ == "__main__":
    main()
