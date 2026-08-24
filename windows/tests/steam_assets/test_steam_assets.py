"""Headless tests for steam_assets: Steam-install OSK themes + glyphs.

Steam discovery is monkeypatched to a tmpdir-shaped install; nothing touches
the real Steam install (same approach as test_win_focus / key_sound)."""

import os

import steam_assets


def _reset():
    """Clear module-level caches so each test resolves Steam fresh."""
    steam_assets._steam_root = None
    steam_assets._steam_root_resolved = False
    steam_assets._theme_bundle_path = None
    steam_assets._theme_bundle_text = None
    steam_assets._theme_bundle_resolved = False
    steam_assets._glyph_cache.clear()


def _write_theme_bundle(root):
    css_dir = os.path.join(root, "steamui", "css")
    os.makedirs(css_dir)
    bundle = os.path.join(css_dir, "chunk~aaaaaaaa.css")
    with open(bundle, "w", encoding="utf-8") as f:
        f.write(
            ".DefaultTheme{--background-color:#0a0a0a;"
            "--key-color:#ffffff;--key-background-color:#111111}"
            ".Digital{--background-color:#010F02;"
            "--key-color:#19E015;--key-background-color:#0C180F}"
        )
    return bundle


def test_glyph_resolved_from_steam(monkeypatch, tmp_path):
    _reset()
    root = str(tmp_path)
    api = os.path.join(root, "controller_base", "images", "api", "knockout")
    os.makedirs(api)
    glyph = os.path.join(api, "shared_button_x_md.png")
    with open(glyph, "wb") as f:
        f.write(b"png")
    monkeypatch.setattr(steam_assets, "find_steam_path", lambda: root)

    assert steam_assets.find_glyph_path("glyph_x.png") == glyph


def test_glyph_path_cached(monkeypatch, tmp_path):
    _reset()
    root = str(tmp_path)
    api = os.path.join(root, "controller_base", "images", "api", "knockout")
    os.makedirs(api)
    glyph = os.path.join(api, "shared_button_x_md.png")
    with open(glyph, "wb") as f:
        f.write(b"png")
    monkeypatch.setattr(steam_assets, "find_steam_path", lambda: root)

    first = steam_assets.find_glyph_path("glyph_x.png")
    # A later failure to stat the file must not invalidate the cache.
    monkeypatch.setattr(steam_assets.os.path, "isfile", lambda p: False)
    assert steam_assets.find_glyph_path("glyph_x.png") == first


def test_glyph_unmapped_or_missing(monkeypatch, tmp_path):
    _reset()
    monkeypatch.setattr(steam_assets, "find_steam_path", lambda: str(tmp_path))

    # Unmapped (no Steam counterpart: stays bundled).
    assert steam_assets.find_glyph_path("glyph_keyboard.png") is None
    # Mapped but the Steam file is absent.
    assert steam_assets.find_glyph_path("glyph_x.png") is None


def test_theme_rule_and_names(monkeypatch, tmp_path):
    _reset()
    root = str(tmp_path)
    _write_theme_bundle(root)
    monkeypatch.setattr(steam_assets, "find_steam_path", lambda: root)

    assert steam_assets.read_theme_rule("Digital") == (
        ".Digital{--background-color:#010F02;"
        "--key-color:#19E015;--key-background-color:#0C180F}"
    )
    assert steam_assets.read_theme_rule("Missing") is None
    assert steam_assets.list_theme_names() == ["DefaultTheme", "Digital"]


def test_no_steam_degrades(monkeypatch, tmp_path):
    _reset()
    monkeypatch.setattr(steam_assets, "find_steam_path", lambda: str(tmp_path))

    assert steam_assets.read_theme_rule("Digital") is None
    assert steam_assets.list_theme_names() == []
    assert steam_assets.find_glyph_path("glyph_x.png") is None
