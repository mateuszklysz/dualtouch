"""Tray icon image loading."""

import ctypes
import os

import pystray
from applog import _bundle_dir
from PIL import Image

# Windows sends these messages to every top-level window during logoff,
# restart, and shutdown. pystray's dispatcher returns 0 for messages it does
# not know, which Windows treats as a veto for WM_QUERYENDSESSION.
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016


class SessionAwareIcon(pystray.Icon):
    """Tray icon that cooperates with Windows session shutdown.

    The project only uses pystray's Windows backend, but keeping the handler
    registration conditional makes this class harmless if another backend is
    selected for headless tests or source inspection.
    """

    def __init__(self, *args, on_session_end=None, **kwargs):
        self._on_session_end_callback = on_session_end
        super().__init__(*args, **kwargs)
        handlers = getattr(self, "_message_handlers", None)
        if isinstance(handlers, dict):
            handlers[WM_QUERYENDSESSION] = self._on_query_end_session
            handlers[WM_ENDSESSION] = self._on_end_session

    def _on_query_end_session(self, wparam, lparam):
        """Approve Windows session termination immediately."""
        return 1

    def _on_end_session(self, wparam, lparam):
        """Start application cleanup after Windows commits to termination."""
        if wparam and self._on_session_end_callback is not None:
            self._on_session_end_callback(self)
        return 0


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
