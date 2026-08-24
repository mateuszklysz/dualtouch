"""Keyboard layout registry.

The OSK's key layout is a YAML file under data/cfg/ (the same loader as the
original single QWERTY board). This module maps a user-facing layout name to
its file so the tray can offer a radio list and triton.load_kb_config can
build the matching VirtualKeyboard at every OSK open.

Layouts are POSITIONAL: each YAML places a keycode + label at a board slot,
and symbol production goes through the OS's active keymap (see
keyboard-layout.yaml's header). The bundled boards therefore read best when
the Windows layout matches (AZERTY/QWERTZ), and Dvorak/Colemak/ABC send
US-QWERTY-positioned scancodes so the printed labels always match what lands
in the text field on a stock US system.
"""

import os

from triton import resources

DEFAULT_LAYOUT = "QWERTY"

_PREFIX = "keyboard-layout"
_SUFFIX = ".yaml"

# Display name -> cfg filename. QWERTY keeps the original legacy filename.
_LAYOUT_FILES = {
    "QWERTY": "keyboard-layout.yaml",
    "AZERTY": "keyboard-layout-azerty.yaml",
    "QWERTZ": "keyboard-layout-qwertz.yaml",
    "Dvorak": "keyboard-layout-dvorak.yaml",
    "Colemak": "keyboard-layout-colemak.yaml",
    "ABC": "keyboard-layout-abc.yaml",
}

_BY_FILENAME = {fn: name for name, fn in _LAYOUT_FILES.items()}


def _cfg_dir():
    """Directory holding the bundled layout YAMLs (via the always-present
    default file), or None when the bundle dir is missing."""
    p = resources.find_cfg_resource(_LAYOUT_FILES[DEFAULT_LAYOUT])
    return os.path.dirname(p) if p else None


def _extra_files():
    """User-added keyboard-layout-*.yaml files in the cfg dir as
    {derived display name: filename}, sorted by filename. The derivation
    mirrors available_layouts() so both agree on the names they offer."""
    d = _cfg_dir()
    out = {}
    if d:
        try:
            found = {
                fn
                for fn in os.listdir(d)
                if fn.startswith(_PREFIX) and fn.lower().endswith(_SUFFIX)
            }
        except OSError:
            found = set()
        known = set(_LAYOUT_FILES.values())
        for fn in sorted(found - known):
            stem = fn[: -len(_SUFFIX)]
            extra = stem[len(_PREFIX) :].lstrip("-_")
            out[extra.capitalize() if extra else stem.capitalize()] = fn
    return out


def available_layouts():
    """Layout names with a file present in the cfg dir, display order first;
    any user-added keyboard-layout-*.yaml is appended alphabetically. Falls
    back to [DEFAULT_LAYOUT] so the tray always has an entry."""
    d = _cfg_dir()
    found = set()
    if d:
        try:
            for fn in os.listdir(d):
                if fn.startswith(_PREFIX) and fn.lower().endswith(_SUFFIX):
                    found.add(fn)
        except OSError:
            pass
    out = [n for n, fn in _LAYOUT_FILES.items() if fn in found]
    out.extend(_extra_files())
    return out or [DEFAULT_LAYOUT]


def layout_filename(name):
    """Cfg filename for a layout display name (bundled first, then
    user-added boards by derived name); unknown names (a stale
    settings.json) fall back to the default board's file."""
    fn = _LAYOUT_FILES.get(name)
    if fn is None:
        folded = {n.casefold(): f for n, f in _LAYOUT_FILES.items()}
        fn = folded.get(str(name).casefold())
    if fn is None:
        folded = {n.casefold(): f for n, f in _extra_files().items()}
        fn = folded.get(str(name).casefold())
    return fn or _LAYOUT_FILES[DEFAULT_LAYOUT]


def normalize_layout_name(name):
    """Canonical display name for a setting value (case-insensitive match
    against bundled layouts, then user-added boards), or None when it names
    no file present in the cfg dir."""
    if not isinstance(name, str):
        return None
    folded = str(name).casefold()
    for n in _LAYOUT_FILES:
        if n.casefold() == folded:
            return n
    for n in _extra_files():
        if n.casefold() == folded:
            return n
    return None
