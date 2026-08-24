"""fonts: fallback lookup is pure; TTF rendering attempted only when the
headless SDL stack can init it (skipped otherwise)."""

import os

import pytest
from triton.fonts import _first_existing


def test_first_existing_returns_first_hit(tmp_path):
    miss = tmp_path / "missing.ttf"
    hit = tmp_path / "real.ttf"
    hit.write_bytes(b"x")
    assert _first_existing([str(miss), str(hit)]) == str(hit)
    assert _first_existing([str(miss)]) is None
    assert _first_existing([]) is None


def test_font_opens_bundled_font_when_ttf_available():
    from triton import resources
    from triton.fonts import _Font

    font = resources.find_data_resource(
        os.path.join("fonts", "Selawik-Semibold.ttf")
    )
    if not font or not os.path.isfile(font):
        pytest.skip("bundled font not present")
    try:
        import sdl3w as S

        S.TTF_Init()
        f = _Font(font, 16)
    except Exception as e:
        pytest.skip(f"SDL_ttf unavailable headless: {e!r}")
    col = type("C", (), {"r": 1, "g": 2, "b": 3})()
    surf = f.render_surface("A", col)
    assert surf is not None
