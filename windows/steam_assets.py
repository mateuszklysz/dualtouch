"""Steam-install runtime assets: OSK themes + controller-button glyphs.

Same model as key_sound.py: paths are located from the Steam install at
runtime via steam_shortcut.find_steam_path(), resolved once and cached, and
callers fall back to the bundled copy (or their built-in defaults) when
Steam is missing or the file isn't there.

Themes
------
Steam ships every OSK theme in ONE hashed web bundle under steamui/css/
(e.g. chunk~2dcc5aaf7.css) — the same file Steam's web keyboard loads. The
chunk filename hash changes across Steam versions, so the bundle is found by
content: the first steamui/css/*.css whose text contains the `.DefaultTheme{`
vars rule wins. Each theme's palette is its `.Name{ ... }` rule.

Glyphs
------
Steam ships controller glyphs under controller_base/images/api/ in three
tints (dark / light / knockout). Each app glyph maps to one of those files;
glyphs with no Steam counterpart (the touch-cursor visuals, OSK function
icons) are unmapped and stay bundled.
"""

import glob
import os
import re

from steam_shortcut import find_steam_path

# First CSS rule of Steam's default OSK theme — the marker that identifies
# the theme bundle among the many hashed chunks in steamui/css.
_THEME_BUNDLE_MARKER = ".DefaultTheme{"

# Every OSK theme's vars rule is `.Name{--background-color: ... }` and defines
# --key-background-color (a var no other component uses).
_THEME_NAME_RE = re.compile(
    r"\.([A-Z][A-Za-z0-9]*)\{\s*--background-color:"
    r"[^}]*--key-background-color:"
)

# Repo glyph basename -> Steam glyph path relative to the install root.
_GLYPHS = {
    "glyph_x.png": os.path.join(
        "controller_base",
        "images",
        "api",
        "knockout",
        "shared_button_x_md.png",
    ),
    "glyph_y.png": os.path.join(
        "controller_base",
        "images",
        "api",
        "knockout",
        "shared_button_y_md.png",
    ),
    "glyph_l2.png": os.path.join(
        "controller_base", "images", "api", "knockout", "sc_l2_md.png"
    ),
    "glyph_l3.png": os.path.join(
        "controller_base", "images", "api", "knockout", "shared_l3_md.png"
    ),
    "sc_r2_md.png": os.path.join(
        "controller_base", "images", "api", "light", "sc_r2_md.png"
    ),
    "sd_button_aux_md.png": os.path.join(
        "controller_base", "images", "api", "knockout", "sd_button_aux_md.png"
    ),
}

_steam_root = None
_steam_root_resolved = False
_theme_bundle_path = None
_theme_bundle_text = None
_theme_bundle_resolved = False
_glyph_cache = {}


def _root():
    """Steam install root (cached), or None."""
    global _steam_root, _steam_root_resolved
    if not _steam_root_resolved:
        _steam_root_resolved = True
        try:
            _steam_root = find_steam_path()
        except Exception:
            _steam_root = None
    return _steam_root


def _theme_bundle():
    """(path, text) of Steam's OSK theme CSS bundle, or (None, None)."""
    global _theme_bundle_path, _theme_bundle_text, _theme_bundle_resolved
    if not _theme_bundle_resolved:
        _theme_bundle_resolved = True
        root = _root()
        if root:
            css_dir = os.path.join(root, "steamui", "css")
            # Hashed chunk names first (that's where the OSK themes live);
            # fall back to scanning every steamui css if a future Steam moves
            # them out of the chunk set.
            for pattern in ("chunk~*.css", "*.css"):
                for path in sorted(glob.glob(os.path.join(css_dir, pattern))):
                    try:
                        with open(
                            path, encoding="utf-8", errors="replace"
                        ) as f:
                            text = f.read()
                    except OSError:
                        continue
                    if _THEME_BUNDLE_MARKER in text:
                        _theme_bundle_path = path
                        _theme_bundle_text = text
                        break
                if _theme_bundle_path:
                    break
    return _theme_bundle_path, _theme_bundle_text


def _extract_rule(text, name):
    """`.Name{ ... }` rule text (matched braces) or None."""
    start = text.find("." + name + "{")
    if start < 0:
        return None
    i = start + len(name) + 2
    depth = 1
    n = len(text)
    while depth > 0 and i < n:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    if depth:
        return None
    return text[start:i]


def read_theme_rule(name):
    """The `.Name{ ... }` vars rule from Steam's theme bundle, or None."""
    _, text = _theme_bundle()
    if not text:
        return None
    return _extract_rule(text, name)


def list_theme_names():
    """OSK theme class names found in Steam's bundle, sorted. Empty when
    Steam is missing or the bundle can't be found."""
    _, text = _theme_bundle()
    if not text:
        return []
    return sorted({m.group(1) for m in _THEME_NAME_RE.finditer(text)})


def find_glyph_path(name):
    """Absolute path to the Steam glyph for `name`, or None (unmapped, Steam
    missing, or file absent). Resolved once per name and cached."""
    if name in _glyph_cache:
        return _glyph_cache[name]
    path = None
    rel = _GLYPHS.get(name)
    if rel:
        root = _root()
        if root:
            cand = os.path.join(root, rel)
            if os.path.isfile(cand):
                path = cand
    _glyph_cache[name] = path
    return path
