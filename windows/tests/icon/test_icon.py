"""Headless tests for the tray icon loader (tray/icon.py).

The frame-pick logic is the one seam this diff actually feeds: it selects the
smallest embedded ICO frame at-or-above the target (sharpening by downscale)
and raises when no asset exists. A bad regeneration that collapses the ico to
a single small frame must be caught here, not silently shipped.
"""

import pytest
from PIL import Image
from tray import icon


@pytest.fixture
def assets(tmpdir, monkeypatch):
    """Point _bundle_dir at a tmp tree mirroring data/images/ and expose it."""
    base = tmpdir.join("data", "images")
    base.ensure_dir()
    monkeypatch.setattr(icon, "_bundle_dir", lambda: str(tmpdir))
    return tmpdir, base


def _make_ico(path, sizes, color):
    im = Image.new("RGBA", (256, 256), color)
    im.save(str(path), format="ICO", sizes=sizes)


def test_load_icon_picks_smallest_frame_at_or_above_target(assets):
    _, base = assets
    # target = max(16*2, 32) = 32 (windll probe fails headlessly -> small=16).
    _make_ico(
        base.join("app_icon.ico"),
        [(16, 16), (24, 24), (48, 48), (256, 256)],
        (10, 20, 30, 255),
    )
    img = icon._load_icon_image()
    assert img.size == (32, 32)
    assert img.mode == "RGBA"
    # 48 is the smallest frame >= 32; LANCZOS of a solid frame stays solid.
    assert img.getpixel((16, 16)) == (10, 20, 30, 255)


def test_load_icon_picks_largest_when_target_exceeds_all_frames(assets):
    _, base = assets
    _make_ico(base.join("app_icon.ico"), [(16, 16), (24, 24)], (1, 2, 3, 255))
    img = icon._load_icon_image()
    assert img.size == (32, 32)
    assert img.getpixel((16, 16)) == (1, 2, 3, 255)  # sizes[-1] fallback


def test_load_icon_raises_when_no_assets(assets):
    with pytest.raises(FileNotFoundError):
        icon._load_icon_image()
