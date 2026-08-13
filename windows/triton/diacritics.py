"""Diacritic-variant (hold a letter to pick accented variants) pure logic.

Feature B: when a letter key is held past the hold delay, a small row of
accented variants pops up above the key; the highlighted variant follows the
finger / mouse / DPAD and releasing types the chosen variant in place of the
base letter that already fired on the press edge (tap-first, hold-to-extend —
a quick tap still types the base with zero added latency).

Everything here is pure logic so it can be unit-tested headlessly:
the per-locale variant map (with the built-in fallback), the merge / lookup
rules, and the variant-row geometry math shared by the renderer (screen.py)
and the input paths (pad / mouse / A). The only Windows-specific bit is
detect_windows_locale(), which the tray uses to pick the default locale; it
returns None gracefully on any failure.
"""

import ctypes

# --- Built-in per-locale fallback map ---------------------------------------
# letter -> [accented variants], keyed by lowercase ISO 639-1 language tag.
# Merged with the user's settings.json "diacritic_variants" map by the tray
# (user wins per letter); locales cover the common European layouts. Polish
# (pl) is fully covered, plus Czech, Slovak, Hungarian, Romanian, Turkish,
# the Nordics, the Baltics, and the South Slavs.
# All characters below are covered by BOTH bundled fonts (Selawik Semibold and
# DejaVu Sans Condensed Bold — verified with a rasterizer), so they render as
# real glyphs, not tofu.
DIACRITIC_VARIANTS = {
    "en": {
        "a": ["á", "à", "â", "ä", "ã", "å", "ā", "æ"],
        "e": ["é", "è", "ê", "ë", "ē", "ė"],
        "i": ["í", "ì", "î", "ï", "ī"],
        "n": ["ñ", "ń"],
        "o": ["ó", "ò", "ô", "ö", "õ", "ø", "ō"],
        "s": ["ś", "š"],
        "u": ["ú", "ù", "û", "ü", "ū"],
        "y": ["ý", "ÿ"],
        "z": ["ž"],
    },
    "de": {
        "a": ["ä", "á", "à", "â"],
        "e": ["é", "è", "ê", "ë"],
        "i": ["í", "ì", "î", "ï"],
        "o": ["ö", "ó", "ò", "ô"],
        "u": ["ü", "ú", "ù", "û"],
        "y": ["ÿ"],
        "s": ["ß"],
    },
    "es": {
        "a": ["á", "à", "â", "ä", "ã", "ā"],
        "e": ["é", "è", "ê", "ë"],
        "i": ["í", "ì", "î", "ï"],
        "n": ["ñ"],
        "o": ["ó", "ò", "ô", "ö", "õ"],
        "u": ["ú", "ù", "û", "ü"],
        "y": ["ý"],
    },
    "fr": {
        "a": ["à", "â", "æ"],
        "c": ["ç"],
        "e": ["é", "è", "ê", "ë"],
        "i": ["î", "ï", "ì", "í"],
        "o": ["ô", "œ", "ò", "ó"],
        "u": ["ù", "û", "ü", "ú"],
        "y": ["ÿ"],
    },
    "it": {
        "a": ["à"],
        "e": ["è", "é", "ê", "ë"],
        "i": ["ì", "í", "î", "ï"],
        "o": ["ò", "ó", "ô"],
        "u": ["ù", "ú", "û"],
    },
    "pt": {
        "a": ["á", "à", "â", "ã", "ä"],
        "c": ["ç"],
        "e": ["é", "è", "ê", "ë"],
        "i": ["í", "ì", "î", "ï"],
        "o": ["ó", "ò", "ô", "õ", "ö"],
        "u": ["ú", "ù", "û", "ü"],
    },
    "pl": {
        "a": ["ą", "á", "à", "â", "ä", "ã"],
        "c": ["ć", "ĉ", "č", "ç"],
        "e": ["ę", "é", "è", "ê", "ë", "ē"],
        "l": ["ł", "ĺ", "ļ"],
        "n": ["ń", "ñ", "ň"],
        "o": ["ó", "ò", "ô", "ö", "õ", "ø"],
        "s": ["ś", "š", "ş"],
        "z": ["ź", "ż", "ž"],
    },
    "cs": {
        "a": ["á", "à", "â", "ä", "ą"],
        "c": ["č", "ć", "ĉ", "ç"],
        "d": ["ď", "đ", "ð"],
        "e": ["é", "ě", "è", "ê", "ë", "ę"],
        "i": ["í", "ì", "î", "ï"],
        "n": ["ň", "ń", "ñ"],
        "o": ["ó", "ò", "ô", "ö", "õ"],
        "r": ["ř"],
        "s": ["š", "ś", "ş"],
        "t": ["ť", "ţ"],
        "u": ["ú", "ů", "ù", "û", "ü"],
        "y": ["ý", "ÿ"],
        "z": ["ž", "ź", "ż"],
    },
    "sk": {
        "a": ["á", "ä", "à", "â"],
        "c": ["č", "ć", "ĉ", "ç"],
        "d": ["ď", "đ", "ð"],
        "e": ["é", "è", "ê", "ë"],
        "i": ["í", "ì", "î", "ï"],
        "l": ["ĺ", "ľ", "ł", "ļ"],
        "n": ["ň", "ń", "ñ"],
        "o": ["ó", "ô", "ò", "ö", "õ"],
        "r": ["ŕ", "ř"],
        "s": ["š", "ś", "ş"],
        "t": ["ť", "ţ"],
        "u": ["ú", "ů", "ù", "û", "ü"],
        "y": ["ý", "ÿ"],
        "z": ["ž", "ź", "ż"],
    },
    "hu": {
        "a": ["á", "à", "â", "ä"],
        "e": ["é", "è", "ê", "ë"],
        "i": ["í", "ì", "î", "ï"],
        "o": ["ó", "ö", "ő", "ò", "ô", "õ"],
        "u": ["ú", "ü", "ű", "ù", "û"],
    },
    "ro": {
        "a": ["ă", "â", "á", "à"],
        "i": ["î", "í", "ì"],
        "s": ["ș", "ş", "š", "ś"],
        "t": ["ț", "ţ", "ť"],
    },
    "tr": {
        "a": ["â", "á", "à", "ä"],
        "c": ["ç", "ć", "ĉ", "č"],
        "g": ["ğ"],
        "i": ["ı", "î", "í", "ì"],
        "o": ["ö", "ó", "ò", "ô"],
        "s": ["ş", "š", "ś"],
        "u": ["ü", "û", "ú", "ù"],
    },
    "sv": {
        "a": ["å", "ä", "á", "à", "â"],
        "e": ["é", "è", "ê", "ë"],
        "o": ["ö", "ø", "ó", "ò", "ô"],
        "u": ["ü", "ú", "ù", "û"],
        "y": ["ý", "ÿ"],
    },
    "da": {
        "a": ["å", "æ", "á", "à", "â", "ä"],
        "e": ["é", "è", "ê", "ë"],
        "o": ["ø", "ö", "ó", "ò", "ô"],
        "u": ["ú", "ù", "û", "ü"],
        "y": ["ý", "ÿ"],
    },
    "no": {
        "a": ["å", "æ", "á", "à", "â", "ä"],
        "e": ["é", "è", "ê", "ë"],
        "o": ["ø", "ö", "ó", "ò", "ô"],
        "u": ["ú", "ù", "û", "ü"],
        "y": ["ý", "ÿ"],
    },
    "fi": {
        "a": ["ä", "å", "á", "à", "â"],
        "e": ["é", "è", "ê", "ë"],
        "o": ["ö", "ó", "ò", "ô"],
        "u": ["ú", "ù", "û", "ü"],
        "y": ["ý", "ÿ"],
    },
    "nl": {
        "a": ["á", "à", "â", "ä", "ã", "å"],
        "e": ["é", "è", "ê", "ë", "ē"],
        "i": ["í", "ì", "î", "ï"],
        "o": ["ó", "ò", "ô", "ö", "õ"],
        "u": ["ú", "ù", "û", "ü", "ū"],
        "y": ["ý", "ÿ"],
        "c": ["ç"],
        "n": ["ñ", "ń"],
    },
    "et": {
        "a": ["ä", "á", "à", "â", "ą"],
        "e": ["é", "è", "ê", "ë", "ę", "ė"],
        "i": ["í", "ì", "î", "ï"],
        "o": ["ö", "õ", "ó", "ò", "ô"],
        "s": ["š", "ś", "ş"],
        "u": ["ü", "ú", "ù", "û"],
        "z": ["ž", "ź", "ż"],
    },
    "lt": {
        "a": ["ą", "á", "à", "â", "ä"],
        "c": ["č", "ć", "ĉ", "ç"],
        "e": ["ę", "ė", "é", "è", "ê", "ë"],
        "i": ["į", "í", "ì", "î", "ï"],
        "o": ["ó", "ò", "ô", "ö", "õ"],
        "s": ["š", "ś", "ş"],
        "u": ["ų", "ū", "ú", "ù", "û", "ü"],
        "z": ["ž", "ź", "ż"],
    },
    "lv": {
        "a": ["ā", "ą", "á", "à", "â", "ä"],
        "c": ["č", "ć", "ĉ", "ç"],
        "e": ["ē", "ę", "ė", "é", "è", "ê", "ë"],
        "g": ["ģ"],
        "i": ["ī", "į", "í", "ì", "î", "ï"],
        "k": ["ķ"],
        "l": ["ļ", "ĺ", "ľ", "ł"],
        "n": ["ņ", "ń", "ñ", "ň"],
        "o": ["ō", "ó", "ò", "ô", "ö", "õ"],
        "r": ["ŗ", "ř", "ŕ"],
        "s": ["š", "ś", "ş"],
        "u": ["ū", "ų", "ú", "ù", "û", "ü"],
        "z": ["ž", "ź", "ż"],
    },
    "hr": {
        "c": ["č", "ć", "ĉ", "ç"],
        "d": ["đ", "ď", "ð"],
        "s": ["š", "ś", "ş"],
        "z": ["ž", "ź", "ż"],
        "a": ["á", "à", "â", "ä"],
        "e": ["é", "è", "ê", "ë"],
        "i": ["í", "ì", "î", "ï"],
        "o": ["ó", "ò", "ô", "ö"],
        "u": ["ú", "ù", "û", "ü"],
    },
    "sl": {
        "c": ["č", "ć", "ĉ", "ç"],
        "s": ["š", "ś", "ş"],
        "z": ["ž", "ź", "ż"],
        "a": ["á", "à", "â", "ä"],
        "e": ["é", "è", "ê", "ë"],
        "i": ["í", "ì", "î", "ï"],
        "o": ["ó", "ò", "ô", "ö"],
        "u": ["ú", "ù", "û", "ü"],
    },
    "is": {
        "a": ["á", "à", "â", "ä", "å"],
        "e": ["é", "è", "ê", "ë"],
        "i": ["í", "ì", "î", "ï"],
        "o": ["ó", "ò", "ô", "ö", "ø"],
        "u": ["ú", "ù", "û", "ü"],
        "y": ["ý", "ÿ"],
        "d": ["ð", "đ", "ď"],
        "t": ["þ", "ţ", "ť"],
    },
    "eo": {
        "c": ["ĉ", "ć", "č", "ç"],
        "g": ["ĝ", "ğ"],
        "h": ["ĥ", "ħ"],
        "j": ["ĵ"],
        "s": ["ŝ", "š", "ś", "ş"],
        "u": ["ŭ", "ú", "ù", "û", "ü"],
    },
    "sq": {
        "c": ["ç", "ć", "ĉ", "č"],
        "e": ["ë", "é", "è", "ê"],
        "i": ["í", "ì", "î", "ï"],
        "o": ["ó", "ò", "ô", "ö"],
        "u": ["ú", "ù", "û", "ü"],
    },
}


def merge_diacritic_maps(*maps):
    """Merge per-locale per-letter variant maps into one. Later maps win per
    (locale, letter); keys are normalized to lowercase; variant values are
    snapshotted to lists of single chars. Returns a fresh dict (never aliases
    a caller's map), so the user's settings override the built-in fallback
    per letter without mutating either."""
    merged = {}
    for m in maps:
        if not isinstance(m, dict):
            continue
        for locale, letters in m.items():
            if not isinstance(letters, dict):
                continue
            loc_map = merged.setdefault(str(locale).lower(), {})
            for letter, variants in letters.items():
                if isinstance(variants, str):
                    variants = list(variants)
                if not isinstance(variants, (list, tuple)):
                    continue
                loc_map[str(letter).lower()] = [str(v) for v in variants]
    return merged


def lookup_variants(variant_map, locale, letter):
    """The variant list for `letter` under `locale`, or None. Falls back to
    the built-in "en" locale when the active locale has no entry (a Windows
    layout like "ja" with no map still gets a sensible English accented
    set); there is no per-letter fallback (a letter absent from the locale's
    own map has no variants)."""
    loc_map = variant_map.get(locale)
    if not loc_map:
        loc_map = variant_map.get("en")
    if not loc_map:
        return None
    variants = loc_map.get(letter)
    return list(variants) if variants else None


# --- Variant-row geometry (shared by renderer + input paths) ----------------
# Candidate sizes in window px. Sized like the real keyboard keys they stand
# in for (the default layout's letter keys are ~84x67), so the popup reads as
# a row of normal-sized keys, not a tiny strip. The whole row is drawn just
# above the held key, centered over it, and clamped into the window.
CANDIDATE_W = 82
CANDIDATE_H = 67
CANDIDATE_GAP = 4
# Gap (px) between the key's top edge and the strip's bottom edge. The strip
# height + this gap must equal the key row pitch (key height 67 + inner
# padding 6 = 73) so the popup spans the ENTIRE row of keys above the held
# key — if it fell short, the exposed tops of those keys would show above the
# popup's top edge and look like they were lit up.
ROW_ABOVE_GAP = 6


def variant_row_rect(key_layout, n, width):
    """Pixel rect (x, y, w, h) of the whole variant strip for a key at
    `key_layout` (a vkb.KeyLayout) with `n` candidates, centered horizontally
    over the key, sitting just above it, clamped into a `width`-px window.
    Returns None if there are no candidates."""
    if not n or key_layout is None:
        return None
    total_w = n * CANDIDATE_W + (n - 1) * CANDIDATE_GAP
    # A strip wider than the window can't be clamped into it — bail (only
    # reachable with a pathological multi-hundred-candidate map).
    if total_w > width:
        return None
    x = key_layout.x + key_layout.w // 2 - total_w // 2
    y = key_layout.y - CANDIDATE_H - ROW_ABOVE_GAP
    x = max(0, min(x, max(0, width - total_w)))
    y = max(0, y)
    return (x, y, total_w, CANDIDATE_H)


def variant_index_at_point(rect, x, y, n):
    """The candidate index under window-pixel (x, y), clamped into the row's
    candidates, or -1 when `rect` is None / empty.

    While the variant row is open the selection must STAY within the variants:
    a pointer that drifts out of the strip vertically (above/below the row,
    e.g. back toward the origin key) still resolves to the NEAREST variant —
    it must never fall back to the base letter just because the finger left
    the strip. Horizontally the index clamps to the first/last candidate. The
    explicit "base" state (index -1, release keeps the base letter) is only
    reachable via the DPAD step cycle, never from pointer position."""
    if rect is None or n <= 0:
        return -1
    rx, ry, rw, rh = rect
    # Clamp y into the row band so vertical drift never deselects the row.
    if y < ry:
        y = ry
    elif y > ry + rh - 1:
        y = ry + rh - 1
    # Clamp x into the row horizontally (before/after the strip picks the
    # nearest edge candidate).
    if x < rx:
        x = rx
    elif x > rx + rw - 1:
        x = rx + rw - 1
    i = int((x - rx) // (CANDIDATE_W + CANDIDATE_GAP))
    return max(0, min(n - 1, i))


def step_variant_index(index, direction, n):
    """Step the highlighted variant `direction` (+1 = right, -1 = left),
    cycling through the row including the "base" state (index -1, release
    keeps the base letter). The cycle is: base -> v0 -> ... -> v_{n-1} ->
    base, so both directions wrap."""
    if n <= 0:
        return -1
    if direction > 0:
        return -1 if index >= n - 1 else index + 1
    return n - 1 if index < 0 else index - 1


def detect_windows_locale():
    """The Windows keyboard input layout the user is actually typing with, as
    a lowercase ISO 639-1 language tag (e.g. "en", "de", "pl"), or None if it
    can't be determined. Resolves the input-locale identifier (HKL) low-word
    language ID to its short name with GetLocaleInfoW(LOCALE_SISO639LANGNAME).

    Sources, in order: (1) the FOREGROUND window's thread input layout
    (GetKeyboardLayout(GetWindowThreadProcessId(GetForegroundWindow())) — the
    app the user is actually typing into; GetKeyboardLayout(0) would return
    only the CALLING tray thread's layout, which routinely disagrees with the
    active app's, e.g. a foreground game in en while the system default is
    pl), then (2) the user's default language (GetUserDefaultLCID). Defensive:
    any ctypes failure returns None so callers fall back to "en"."""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        user32.GetKeyboardLayout.restype = ctypes.c_size_t
        user32.GetKeyboardLayout.argtypes = [ctypes.c_ulong]
        user32.GetUserDefaultLCID.restype = ctypes.c_ulong
        user32.GetUserDefaultLCID.argtypes = []

        def _tag_for_langid(langid):
            buf = ctypes.create_unicode_buffer(16)
            # LOCALE_SISO639LANGNAME = 0x59 → "en" / "de" / "pl" / ...
            if user32.GetLocaleInfoW(langid, 0x59, buf, len(buf)):
                tag = buf.value.strip().lower()
                return tag or None
            return None

        hkl = 0
        fg = user32.GetForegroundWindow()
        if fg:
            tid = user32.GetWindowThreadProcessId(fg, None)
            if tid:
                hkl = user32.GetKeyboardLayout(tid)
        if not hkl:
            hkl = user32.GetKeyboardLayout(0)
        if hkl:
            tag = _tag_for_langid(hkl & 0xFFFF)
            if tag:
                return tag
        # Fall back to the user's default input language.
        return _tag_for_langid(user32.GetUserDefaultLCID() & 0xFFFF)
    except Exception:
        pass
    return None
