import os

import sdl3w as S

# FALLBACK font lookup only. The OSK normally uses the BUNDLED Selawik Semibold
# (an open SIL-OFL, Segoe-UI-metric-compatible font ~ Steam Big Picture's
# keyboard look) — see Screen.__init__. These per-platform system fonts (Segoe
# UI / common Linux fonts) are tried only if the bundled font is somehow missing.
_FONT_CANDIDATES_WIN = [r"C:\Windows\Fonts\seguisb.ttf"]
_SYM_CANDIDATES_WIN = [
    r"C:\Windows\Fonts\seguisym.ttf",
    r"C:\Windows\Fonts\NotoSansSymbols2-Regular.ttf",
]
_FONT_CANDIDATES_LINUX = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
# DejaVu Sans covers the geometric-shape glyphs (◀ ▶ etc.) we'd otherwise
# pull from Segoe UI Symbol on Windows.
_SYM_CANDIDATES_LINUX = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
    "/usr/share/fonts/noto/NotoSansSymbols2-Regular.ttf",
]


def _first_existing(paths):
    for p in paths:
        if os.path.isfile(p):
            return p
    return None


class _Font:
    """Minimal replacement for sdl2.ext.FontManager: opens a single TTF at a
    fixed point size and renders blended UTF-8 text to a fresh SDL_Surface
    (caller turns it into a texture and frees it)."""

    def __init__(self, path, size):
        self.font = S.TTF_OpenFont(path.encode("utf-8"), float(size))
        if not self.font:
            raise RuntimeError(
                f"TTF_OpenFont failed for {path!r}: {S.get_error()}"
            )

    def render_surface(self, text, color):
        if not text:
            return None
        col = S.SDL_Color(color.r, color.g, color.b, 255)
        # length 0 => NUL-terminated UTF-8.
        return S.TTF_RenderText_Blended(
            self.font, text.encode("utf-8"), 0, col
        )
