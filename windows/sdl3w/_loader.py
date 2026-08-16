"""Locate and load the vendored SDL3 DLLs (core + image + ttf).

This is the one place that knows where the binaries live, both when running
from source (windows/sdl3w/dll) and when frozen by PyInstaller (the DLLs are
added to the bundle root / a sdl3w/dll subdir via build.py --add-binary).

Hand-rolled rather than depending on PySDL3 so the shipped onefile carries its
own pinned SDL3 (no runtime download) and we control exactly what's bound.
"""

import ctypes
import os
import sys
from contextlib import suppress

# Pinned SDL3 component versions vendored under dll/. Kept here for reference /
# diagnostics; not used for loading.
SDL3_VERSION = "3.4.10"
SDL3_TTF_VERSION = "3.2.2"
# NOTE: SDL3_image is intentionally NOT vendored — the OSK loads every PNG
# (glyphs/skins) through Pillow and uploads via SDL_CreateSurfaceFrom, so the
# only SDL_image use in the old code (IMG_Init) is dead weight under SDL3.

_CORE_DLL = "SDL3.dll"
_TTF_DLL = "SDL3_ttf.dll"


def _candidate_dirs():
    """Directories to search for the SDL3 DLLs, most-specific first."""
    dirs = []
    # PyInstaller onefile extracts to sys._MEIPASS; build.py drops the DLLs both
    # at the bundle root and under sdl3w/dll, so check both.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(os.path.join(meipass, "sdl3w", "dll"))
        dirs.append(meipass)
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(os.path.join(here, "dll"))
    dirs.append(here)
    # De-dup while preserving order.
    seen = set()
    out = []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _find_dir():
    for d in _candidate_dirs():
        if os.path.exists(os.path.join(d, _CORE_DLL)):
            return d
    raise OSError(
        "SDL3.dll not found. Looked in: " + os.pathsep.join(_candidate_dirs())
    )


def load():
    """Load SDL3 and SDL3_ttf and return (SDL, TTF, dll_dir).

    SDL3_ttf depends on SDL3.dll, so we load SDL3.dll first. Dependency
    resolution is pinned to the loaded DLL's own directory + System32 via
    LoadLibraryExW search flags — never cwd / install dir / the process PATH.
    """
    dll_dir = _find_dir()
    # Scoped, safe helper for the frozen bundle (PyInstaller temp dir); does
    # not touch the process environment. Inert for the LoadLibraryExW flags
    # below, which override the search order explicitly.
    if hasattr(os, "add_dll_directory"):
        with suppress(OSError):
            os.add_dll_directory(dll_dir)

    # LoadLibraryExW with LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | SYSTEM32 restricts
    # resolution of the DLL's own dependencies to the directory it lives in
    # plus System32, so a DLL planted in a writable dir can't be hijacked by
    # name via cwd/PATH.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LoadLibraryExW.restype = ctypes.c_void_p
    kernel32.LoadLibraryExW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR = 0x100
    LOAD_LIBRARY_SEARCH_SYSTEM32 = 0x800
    flags = LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32

    def _load(dll_name):
        path = os.path.join(dll_dir, dll_name)
        handle = kernel32.LoadLibraryExW(path, None, flags)
        if not handle:
            raise OSError(f"Failed to load {path}: {ctypes.WinError()}")
        # Wrap the already-loaded HMODULE via ctypes.CDLL's handle= kwarg.
        # The name must be the REAL dll path, not None: PyInstaller wraps
        # ctypes.CDLL in frozen apps (pyimod03_ctypes), and its wrapper turns
        # any load failure into "Failed to load dynlib/dll %r" using that
        # name — a None name yields the useless "dll None" error. The path is
        # only used as the error/label (_load_library returns the handle
        # directly when handle is given, so no second load happens).
        return ctypes.CDLL(
            path, handle=handle, use_errno=True, use_last_error=True
        )

    SDL = _load(_CORE_DLL)
    TTF = _load(_TTF_DLL)
    return SDL, TTF, dll_dir
