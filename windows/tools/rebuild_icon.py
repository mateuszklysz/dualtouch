"""Regenerate ALL DualTouch image assets from the Satori source.

Pipeline:
  1. render.mjs          — Vercel Satori composes the app icon (HTML/CSS:
     two angled trackpads on a dark rounded-rect body) and resvg-js
     rasterizes it to tools/icon/icon-1024.png.
  2. This script         — downscales the master and writes every asset:

       data/images/app_icon.ico   7-frame ICO (16/24/32/48/64/128/256)
       data/images/icon.png       500x500  Steam grid icon
       data/images/capsule.png    600x900  portrait tile  (#121212 + icon)
       data/images/wide_capsule.png 920x430 landscape tile (#121212 + icon)
       data/images/hero.png       3840x1240 hero banner (#121212 + wireframe)

     hero.png is built from tools/icon/wireframe.png (a build-time source,
     NOT bundled into the exe — it lives outside data/). hero.png itself IS
     bundled with the other tiles.

Run from windows/:  python tools/rebuild_icon.py
Requires: python PIL, node + npm deps installed in tools/icon (see its README).
"""

import os
import shutil
import subprocess

from PIL import Image

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ICON_DIR = os.path.join(_TOOLS, "icon")
_IMAGES = os.path.join(os.path.dirname(_TOOLS), "data", "images")

_ICO_PATH = os.path.join(_IMAGES, "app_icon.ico")
_ICON_PNG = os.path.join(_IMAGES, "icon.png")
_CAPSULE = os.path.join(_IMAGES, "capsule.png")
_WIDE_CAPSULE = os.path.join(_IMAGES, "wide_capsule.png")
_HERO = os.path.join(_IMAGES, "hero.png")
_WIREFRAME = os.path.join(_ICON_DIR, "wireframe.png")
_MASTER = os.path.join(_ICON_DIR, "icon-1024.png")

# ICO sizes must mirror PIL's default set used by the original icon.
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

# Steam tile backgrounds and icon sizes (px).
TILE_BG = (18, 18, 18, 255)  # #121212
CAPSULE_SIZE = (600, 900)
WIDE_CAPSULE_SIZE = (920, 430)
CAPSULE_ICON = 350  # 500 * 0.7 — the icon was too big; 30% smaller
WIDE_CAPSULE_ICON = 240  # centered, matches the original landscape tile

# Hero banner: wireframe sits on the right, 5% margin from the right edge,
# at 85% of the hero height.
HERO_SIZE = (3840, 1240)
HERO_RIGHT_MARGIN = round(3840 * 0.05)  # 192
HERO_WIREFRAME_HEIGHT = round(1240 * 0.85)  # 1054


def _run(cmd, cwd):
    print(f"> {os.path.join(cwd, cmd[0])} {' '.join(cmd[1:])}")
    subprocess.run(cmd, cwd=cwd, check=True)


def _node():
    """Absolute path to node.exe (found via shutil.which, with a fallback for
    the standard winget install location)."""
    found = shutil.which("node")
    if found:
        return found
    candidate = os.path.join(
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        "nodejs",
        "node.exe",
    )
    return candidate if os.path.isfile(candidate) else "node"


def _centered_tile(icon, canvas_size, icon_size):
    """Solid #121212 canvas with the icon resized to icon_size and centered."""
    icon = icon.resize((icon_size, icon_size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", canvas_size, TILE_BG)
    x = (canvas_size[0] - icon_size) // 2
    y = (canvas_size[1] - icon_size) // 2
    canvas.paste(icon, (x, y), icon)
    return canvas.convert("RGB")


def _hero():
    """3840x1240 hero banner: #121212 background with the wireframe drawing
    on the RIGHT side (5% margin from the right edge, 85% height), vertically
    centered. The wireframe is already dark-on-dark line art, so it drops
    straight onto the background."""
    wf = Image.open(_WIREFRAME).convert("RGBA")
    wf = wf.resize(
        (
            round(HERO_WIREFRAME_HEIGHT * wf.width / wf.height),
            HERO_WIREFRAME_HEIGHT,
        ),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGBA", HERO_SIZE, TILE_BG)
    x = HERO_SIZE[0] - HERO_RIGHT_MARGIN - wf.width
    y = (HERO_SIZE[1] - wf.height) // 2
    canvas.paste(wf, (x, y), wf)
    return canvas.convert("RGB")


def main():
    # 1) Satori render -> icon-1024.png
    _run([_node(), "render.mjs"], _ICON_DIR)

    if not os.path.isfile(_MASTER):
        raise SystemExit(f"master icon not found: {_MASTER}")

    master = Image.open(_MASTER).convert("RGBA")

    # app_icon.ico: multi-resolution, base = largest frame, appended sizes
    # keep each frame native (PIL only keeps provided frames of matching size).
    sizes = [(s, s) for s in ICO_SIZES]
    frames = [
        master.resize((s, s), Image.Resampling.LANCZOS) for s in ICO_SIZES
    ]
    frames.sort(key=lambda f: f.size, reverse=True)
    frames[0].save(
        _ICO_PATH,
        format="ICO",
        sizes=sizes,
        append_images=frames[1:],
    )
    print(f"wrote {_ICO_PATH}")

    # icon.png: 500x500 Steam grid art.
    icon = master.resize((500, 500), Image.Resampling.LANCZOS)
    icon.save(_ICON_PNG)
    print(f"wrote {_ICON_PNG}")

    # capsule.png: portrait tile.
    _centered_tile(icon, CAPSULE_SIZE, CAPSULE_ICON).save(_CAPSULE)
    print(f"wrote {_CAPSULE} (icon {CAPSULE_ICON}px)")

    # wide_capsule.png: landscape tile.
    _centered_tile(icon, WIDE_CAPSULE_SIZE, WIDE_CAPSULE_ICON).save(
        _WIDE_CAPSULE
    )
    print(f"wrote {_WIDE_CAPSULE} (icon {WIDE_CAPSULE_ICON}px)")

    # hero.png: banner with the wireframe on the right.
    if os.path.isfile(_WIREFRAME):
        _hero().save(_HERO)
        print(f"wrote {_HERO}")
    else:
        print(f"skipped {_HERO} (no wireframe.png)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
