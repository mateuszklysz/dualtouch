"""Layer 1: Color value type + skins palette machinery (pure parts)."""

import pytest
from triton import color as tcolor
from triton import skins


def _rgb(c):
    """(r, g, b) out of whatever the skins layer hands back."""
    if hasattr(c, "r"):
        return (c.r, c.g, c.b)
    r, g, b = c[0], c[1], c[2]
    return (int(r), int(g), int(b))


def test_color_roundtrip_and_equality():
    c = tcolor.Color(10, 20, 30)
    assert tuple(c) == (10, 20, 30, 255)
    assert c == tcolor.Color(*c)
    assert tcolor.Color(1, 2, 3, 4) != tcolor.Color(1, 2, 3)
    assert "Color(1, 2, 3" in repr(tcolor.Color(1, 2, 3))


def test_color_ints_floats():
    assert tcolor.Color(12.9, 0.2, 255.5).r == 12


def test_available_skins_include_bundled():
    names = skins.available_skins()
    assert isinstance(names, list) and names
    assert any("gruvbox" in n.lower() for n in names)


def test_skin_selection_roundtrip(monkeypatch):
    # Hermetic: pretend Steam carries two extra themes so there is always
    # something to switch to, regardless of the machine's Steam install.
    monkeypatch.setattr(skins, "_steam_theme_names", lambda: {"Alpha", "Beta"})
    names = skins.available_skins()
    assert "Gruvbox" in names and "Alpha" in names  # appended after order
    other = next(
        n for n in names if n.lower() != skins.get_active_skin().lower()
    )
    before = skins.get_generation()
    skins.set_active_skin(other)
    assert skins.get_active_skin() == other
    assert skins.get_generation() == before + 1
    skins.set_active_skin(other)  # same name: no bump
    assert skins.get_generation() == before + 1
    skins.set_active_skin("Gruvbox")


def test_transparency_levels():
    for level in ("off", "low", "medium", "high"):
        skins.set_transparency(level)
        assert skins.get_transparency_scale() > 0.0
    skins.set_transparency("off")
    assert not skins.is_transparent()
    skins.set_transparency("high")
    assert skins.is_transparent()
    assert skins.get_transparency_scale() < 1.0


def test_load_palette_bundled_skin():
    pal = skins.load_palette("Gruvbox")
    assert isinstance(pal, dict) and pal

    def _ok(v):
        return v is None or (
            hasattr(v, "r") and 0 <= v.r <= 255 and 0 <= v.a <= 255
        )

    assert sum(1 for v in pal.values() if _ok(v)) >= 3


def test_press_shade_is_visually_distinct():
    base = tcolor.Color(120, 120, 120)
    shade = skins._press_shade(base, base)
    assert skins._luminance(shade) != skins._luminance(base)


def test_blend_extremes():
    black = tcolor.Color(0, 0, 0)
    target = tcolor.Color(200, 100, 50)
    assert _rgb(skins._blend(black, target, 0.0)) == (0, 0, 0)
    assert _rgb(skins._blend(black, target, 1.0)) == (
        200,
        100,
        50,
    )


def test_clamp8_and_split_top_commas():
    assert skins._clamp8(-5) == 0
    assert skins._clamp8(300) == 255
    assert skins._clamp8(127.7) in (127, 128)
    parts = skins._split_top_commas("rgb(1, 2, 3), #fff, x")
    assert len(parts) == 3 and parts[1].strip() == "#fff"


@pytest.mark.parametrize(
    "text",
    ["#ff8000", "rgb(255,128,0)", "rgba(255, 128, 0, 1.0)"],
)
def test_parse_color_formats(text):
    assert _rgb(skins._parse_color(text)) == (255, 128, 0)


def test_parse_color_short_hex_expands():
    assert _rgb(skins._parse_color("#f80")) == (255, 136, 0)


def test_parse_color_garbage_is_none():
    assert skins._parse_color("not-a-color") is None
