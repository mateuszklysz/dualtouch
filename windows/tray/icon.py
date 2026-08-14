"""Tray icon image loading."""

import ctypes
import os

from applog import _bundle_dir
from PIL import Image


def _load_icon_image():
    # Prefer the multi-resolution app_icon.ico (hand-tuned per size, so
    # the small tray frame is crisp).
    base = os.path.join(_bundle_dir(), "data", "images")
    try:
        small = ctypes.windll.user32.GetSystemMetrics(49)  # SM_CXSMICON
    except Exception:
        small = 16
    target = max(small * 2, 32)  # 2× for HiDPI headroom

    ico_path = os.path.join(base, "app_icon.ico")
    if os.path.isfile(ico_path):
        ico = Image.open(ico_path)
        # Pick the smallest embedded frame that's >= target so we sharpen
        # by downscaling, not upscaling, then LANCZOS to the exact size.
        sizes = sorted(ico.info.get("sizes", [ico.size]))
        pick = next((s for s in sizes if s[0] >= target), sizes[-1])
        # PIL's ICO plugin lets you pick the embedded frame by setting .size
        # (runtime-valid; the stub types ImageFile.size as read-only).
        ico.size = pick  # type: ignore[reportAttributeAccessIssue]
        return ico.convert("RGBA").resize(
            (target, target), Image.Resampling.LANCZOS
        )

    raise FileNotFoundError("no tray icon found under data/images/")
