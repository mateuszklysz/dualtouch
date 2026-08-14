# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['tray/__main__.py'],
    pathex=[],
    binaries=[('sdl3w/dll/SDL3.dll', 'sdl3w/dll'), ('sdl3w/dll/SDL3_ttf.dll', 'sdl3w/dll')],
    datas=[('data', 'data')],
    hiddenimports=['pystray._win32', 'pynput.keyboard._win32', 'pynput.mouse._win32', 'PIL._tkinter_finder', 'sdl3w'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DualTouch-windows',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['data/images/app_icon.ico'],
)
