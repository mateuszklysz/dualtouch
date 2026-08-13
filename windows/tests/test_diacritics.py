"""Headless tests for the Feature-B diacritic-variant logic
(triton/diacritics.py + the settings roundtrip).

Pure logic: the per-locale variant map and its merge/lookup rules, the
variant-row geometry (shared by the renderer and the pad/mouse/A input
paths), and the settings.json roundtrip (the tray merges the built-in map
with the user's map). No SDL, Steam, or controller involved.
"""

import json
from collections import namedtuple

import appsettings
from triton import diacritics

KeyLayout = namedtuple("KeyLayout", "x y w h row col")


def test_builtin_map_has_the_core_locales():
    assert {"en", "de", "es", "fr", "it", "pt"} <= set(
        diacritics.DIACRITIC_VARIANTS
    )


def test_lookup_finds_variants_for_a_letter():
    variants = diacritics.lookup_variants(
        diacritics.DIACRITIC_VARIANTS, "en", "a"
    )
    assert variants and "á" in variants and "à" in variants


def test_lookup_returns_none_for_a_letter_without_variants():
    assert (
        diacritics.lookup_variants(diacritics.DIACRITIC_VARIANTS, "en", "b")
        is None
    )


def test_lookup_falls_back_to_en_for_unknown_locale():
    # A Windows layout like "ja" has no map entry — it falls back to "en" so
    # the feature still works on unmapped layouts.
    assert (
        diacritics.lookup_variants(diacritics.DIACRITIC_VARIANTS, "ja", "e")
        is not None
    )


def test_merge_user_overrides_per_letter_but_keeps_rest():
    user = {"de": {"a": ["ä", "â"]}}  # user's German map overrides only "a"
    merged = diacritics.merge_diacritic_maps(
        diacritics.DIACRITIC_VARIANTS, user
    )
    assert merged["de"]["a"] == ["ä", "â"]
    # Every other German letter still comes from the built-in map.
    assert merged["de"]["o"] == diacritics.DIACRITIC_VARIANTS["de"]["o"]
    # A locale only present in the user map is added.
    merged2 = diacritics.merge_diacritic_maps(
        diacritics.DIACRITIC_VARIANTS, {"pl": {"z": ["ż", "ź"]}}
    )
    assert merged2["pl"]["z"] == ["ż", "ź"]


def test_merge_normalizes_and_snapshots():
    src = {"EN": {"A": "áà"}}
    merged = diacritics.merge_diacritic_maps(src)
    assert merged == {"en": {"a": ["á", "à"]}}
    # Later mutation of the source must not leak into the merged map.
    src["EN"]["A"] = "x"
    assert merged["en"]["a"] == ["á", "à"]


def test_row_rect_centered_above_key_and_clamped():
    layout = KeyLayout(500, 151, 90, 67, 2, 1)  # mid-window 'a' key
    rect = diacritics.variant_row_rect(layout, 8, 1286)
    x, y, w, h = rect
    assert w == 8 * diacritics.CANDIDATE_W + 7 * diacritics.CANDIDATE_GAP
    assert h == diacritics.CANDIDATE_H
    assert y == 151 - diacritics.CANDIDATE_H - diacritics.ROW_ABOVE_GAP
    # Centered over the key: the key center maps to the strip center.
    assert x + w // 2 == layout.x + layout.w // 2
    # A strip wider than the window can't fit -> None (pathological map).
    assert diacritics.variant_row_rect(layout, 100, 1286) is None
    # No candidates -> None.
    assert diacritics.variant_row_rect(layout, 0, 1286) is None


def test_row_rect_clamps_top_for_top_row():
    rect = diacritics.variant_row_rect(
        KeyLayout(300, 5, 90, 67, 0, 1), 4, 1286
    )
    assert rect is not None
    assert rect[1] == 0  # never goes above the window


def test_index_from_pointer_x():
    w = 3 * diacritics.CANDIDATE_W + 2 * diacritics.CANDIDATE_GAP
    rect = (100, 150, w, diacritics.CANDIDATE_H)
    step = diacritics.CANDIDATE_W + diacritics.CANDIDATE_GAP
    assert diacritics.variant_index_at_point(rect, 100, 160, 3) == 0
    assert diacritics.variant_index_at_point(rect, 100 + step, 160, 3) == 1
    assert diacritics.variant_index_at_point(rect, 100 + 2 * step, 160, 3) == 2
    # Drifting OUT of the strip must still select a variant (the user's
    # requirement): horizontal overshoot clamps to the nearest candidate,
    # vertical drift clamps into the row band — never falls back to base.
    assert diacritics.variant_index_at_point(rect, 0, 160, 3) == 0  # left edge
    assert (
        diacritics.variant_index_at_point(rect, 2000, 160, 3) == 2
    )  # right edge
    assert (
        diacritics.variant_index_at_point(rect, 100, 149, 3) == 0
    )  # above row
    assert (
        diacritics.variant_index_at_point(rect, 100, 250, 3) == 0
    )  # below row
    assert diacritics.variant_index_at_point(rect, 100 + step, 149, 3) == 1
    assert diacritics.variant_index_at_point(None, 100, 160, 3) == -1


def test_step_index_cycles_through_base_and_variants():
    # Right: base(-1) -> v0 -> v1 -> v2 -> base(-1).
    assert diacritics.step_variant_index(-1, 1, 3) == 0
    assert diacritics.step_variant_index(0, 1, 3) == 1
    assert diacritics.step_variant_index(2, 1, 3) == -1
    # Left: base -> v2 -> v1 -> v0 -> base.
    assert diacritics.step_variant_index(-1, -1, 3) == 2
    assert diacritics.step_variant_index(2, -1, 3) == 1
    assert diacritics.step_variant_index(0, -1, 3) == -1


def test_detect_windows_locale_returns_a_two_letter_tag():
    # Read-only Win32 query: returns the active keyboard layout's ISO 639-1
    # tag ("en", "de", ...) or None on failure — never a malformed value.
    tag = diacritics.detect_windows_locale()
    assert tag is None or (len(tag) == 2 and tag.isalpha())


def test_settings_roundtrip_keeps_user_variant_map(tmpdir):
    """The settings loader must pass the user's diacritic_variants dict through
    and the diacritics keys must exist with sane defaults when unset."""
    old_ud = appsettings.user_data_dir
    appsettings.user_data_dir = lambda: str(tmpdir)
    try:
        data = {
            "diacritics_enabled": False,
            "diacritic_locale": "de",
            "diacritic_variants": {"de": {"s": ["ß", "ś"]}},
        }
        with open(appsettings._settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        s = appsettings._load_settings()
        assert s["diacritics_enabled"] is False
        assert s["diacritic_locale"] == "de"
        assert s["diacritic_variants"] == {"de": {"s": ["ß", "ś"]}}
        # Defaults when the file sets none of the keys.
        with open(appsettings._settings_path(), "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2)
        s2 = appsettings._load_settings()
        assert s2["diacritics_enabled"] is True
        assert s2["diacritic_locale"] == "auto"
        assert s2["diacritic_variants"] == diacritics.DIACRITIC_VARIANTS
    finally:
        appsettings.user_data_dir = old_ud


def test_tap_char_builds_sendinput_unicode():
    """uinput.tap_char must be a REAL injection path, not a silent no-op: with
    no plain/shift VK on the active layout and the clipboard path disabled, it
    builds two SendInput KEYBDINPUTs (wVk=0, KEYEVENTF_UNICODE down + up) for
    the char's UTF-16 code unit. SendInput / clipboard are stubbed so nothing
    is injected on the test machine; the struct contents are verified."""
    import applog

    applog.set_logging_enabled(
        False
    )  # don't contaminate the real dualtouch.log
    import ctypes

    import steamcontroller.uinput as sui

    calls = {}
    user32 = sui._U32
    real_send = user32.SendInput
    real_vkscan = user32.VkKeyScanW
    real_openclip = user32.OpenClipboard

    # Pin VkKeyScanW to "no such key on the active layout" so tap_char's
    # VK-first path is bypassed. The paste path is disabled by making
    # OpenClipboard fail, forcing the UNICODE fallback to be exercised
    # deterministically regardless of the host machine's keyboard layout.
    user32.VkKeyScanW.restype = ctypes.c_short
    user32.VkKeyScanW.argtypes = [ctypes.c_uint]

    def _stub_vkscan(ch):
        return -1

    def _stub_openclipboard(_hwnd):
        return 0  # paste path refuses -> UNICODE

    setattr(user32, "VkKeyScanW", _stub_vkscan)  # noqa: B010
    setattr(user32, "OpenClipboard", _stub_openclipboard)  # noqa: B010

    def fake_send(n, inputs, cb):
        calls["n"] = int(n)
        calls["cb"] = int(cb)
        arr = ctypes.cast(inputs, ctypes.POINTER(sui._INPUT))
        calls["kis"] = [
            (arr[i].u.ki.wVk, int(arr[i].u.ki.wScan), int(arr[i].u.ki.dwFlags))
            for i in range(n)
        ]
        return 2

    setattr(user32, "SendInput", fake_send)  # noqa: B010
    try:
        kb = sui.Keyboard()
        assert kb.tap_char("á") is True
        assert kb.tap_char("") is False  # empty: refused, no SendInput
        assert kb.tap_char("\U0001f600") is False  # non-BMP: refused
    finally:
        setattr(user32, "SendInput", real_send)  # noqa: B010
        setattr(user32, "VkKeyScanW", real_vkscan)  # noqa: B010
        setattr(user32, "OpenClipboard", real_openclip)  # noqa: B010

    # Two inputs (key-down + key-up), correct struct size, VK 0 + the char's
    # code unit, KEYEVENTF_UNICODE (0x0004) and UNICODE|KEYUP (0x0006).
    assert calls["n"] == 2
    assert calls["cb"] == ctypes.sizeof(sui._INPUT) * 2
    assert calls["kis"] == [(0, ord("á"), 0x0004), (0, ord("á"), 0x0006)]


def test_tap_char_altgr_skips_vk_and_uses_unicode():
    """Polish (and other AltGr) accents: VkKeyScanW returns CTRL|ALT (0x06)
    as the modifier byte, e.g. ś on a Polish layout. The log proved a
    synthesized Ctrl+Alt chord is NOT accepted as AltGr by apps (they check
    the AltGr scancode), so it degraded to the unaccented letter 's'. AltGr
    forms must therefore skip the VK path entirely and use paste/UNICODE —
    which type the exact char."""
    import applog

    applog.set_logging_enabled(False)  # don't contaminate the real log
    import ctypes

    import steamcontroller.uinput as sui

    calls = {}
    user32 = sui._U32
    real_send = user32.SendInput
    real_vkscan = user32.VkKeyScanW
    real_openclip = user32.OpenClipboard

    user32.VkKeyScanW.restype = ctypes.c_short
    user32.VkKeyScanW.argtypes = [ctypes.c_uint]

    def _stub_vkscan(ch):
        return 0x0653  # VK 'S' (0x53) + modifier CTRL|ALT (0x06) = AltGr ś

    def _stub_openclipboard(_hwnd):
        return 0  # paste refuses -> UNICODE fallback

    setattr(user32, "VkKeyScanW", _stub_vkscan)  # noqa: B010
    setattr(user32, "OpenClipboard", _stub_openclipboard)  # noqa: B010

    calls["kis"] = []

    def fake_send(n, inputs, cb):
        arr = ctypes.cast(inputs, ctypes.POINTER(sui._INPUT))
        calls["kis"].extend(
            [
                (
                    arr[i].u.ki.wVk,
                    int(arr[i].u.ki.wScan),
                    int(arr[i].u.ki.dwFlags),
                )
                for i in range(n)
            ]
        )
        return n

    setattr(user32, "SendInput", fake_send)  # noqa: B010
    try:
        kb = sui.Keyboard()
        assert kb.tap_char("ś") is True
    finally:
        setattr(user32, "SendInput", real_send)  # noqa: B010
        setattr(user32, "VkKeyScanW", real_vkscan)  # noqa: B010
        setattr(user32, "OpenClipboard", real_openclip)  # noqa: B010

    # NO bare-VK / AltGr chord: the only inputs are the UNICODE pair
    # (wVk=0, wScan=U+015B "ś", KEYEVENTF_UNICODE 0x0004 + KEYUP 0x0006).
    assert calls["kis"] == [(0, 0x015B, 0x0004), (0, 0x015B, 0x0006)]


def test_tap_char_uses_vk_for_plain_letter():
    """Plain (unshifted) keys on the active layout keep using the fast VK
    path: a VkKeyScanW returning VK+no-modifier taps exactly that VK."""
    import applog

    applog.set_logging_enabled(False)  # don't contaminate the real log
    import ctypes

    import steamcontroller.uinput as sui

    calls = {}
    user32 = sui._U32
    real_send = user32.SendInput
    real_vkscan = user32.VkKeyScanW

    user32.VkKeyScanW.restype = ctypes.c_short
    user32.VkKeyScanW.argtypes = [ctypes.c_uint]

    def _stub_vkscan(ch):
        return 0x0064  # VK 'd', no modifier

    setattr(user32, "VkKeyScanW", _stub_vkscan)  # noqa: B010
    calls["kis"] = []

    def fake_send(n, inputs, cb):
        arr = ctypes.cast(inputs, ctypes.POINTER(sui._INPUT))
        calls["kis"].extend(
            [
                (
                    arr[i].u.ki.wVk,
                    int(arr[i].u.ki.wScan),
                    int(arr[i].u.ki.dwFlags),
                )
                for i in range(n)
            ]
        )
        return n  # every event delivered

    setattr(user32, "SendInput", fake_send)  # noqa: B010
    try:
        kb = sui.Keyboard()
        assert kb.tap_char("d") is True
    finally:
        setattr(user32, "SendInput", real_send)  # noqa: B010
        setattr(user32, "VkKeyScanW", real_vkscan)  # noqa: B010

    # Plain VK path: VK 'd' down + up (one event per SendInput call).
    assert calls["kis"] == [(0x64, 0, 0), (0x64, 0, 0x0002)]


def test_pad_hold_opens_row_and_release_commits_variant():
    """End-to-end of the primary SC input path (DEFER model): a pad click on a
    variant-capable letter types NOTHING at the press edge; held past the hold
    delay the variant row opens (no meaningless repeat, no base letter typed);
    the finger over the row selects a candidate; releasing commits it as a
    ("variant", char) item for the main thread. A QUICK tap types the base on
    release instead (see test_pad_quick_tap_types_base_on_release). Uses the
    real layout and the shared diacritic session state — no SDL, no HID."""
    from collections import deque

    from triton import config, screen, state, vkb
    from triton.pad import _PadMixin
    from triton.screen import CoordFraction

    screen.width, screen.height = 1286, 369
    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("keys", kb_config)
    kb = kb_config.construct()
    kb.update_dimensions()
    state.reset_session()
    state.set_virtual_kb(kb)
    state.set_diacritics_enabled(True)
    state.set_active_locale("en")

    class _KB:
        def __init__(self):
            self.downs = []

        def pressEvent(self, keys):
            self.downs.append(keys)

        def releaseEvent(self, keys):
            pass

    class _P:
        def __init__(self, buttons):
            self.buttons = buttons

    class _CS:
        def __init__(self):
            self.click_queue = deque()

    class _D(_PadMixin):
        _kb: _KB
        sc_input_previous: _P
        controller_state: _CS

        BACKSPACE_HOLD_DELAY = vkb.KEY_REPEAT_DELAY
        BACKSPACE_REPEAT = vkb.KEY_REPEAT_INTERVAL
        PAD_CLICK_SETTLE = 0.05

        def __init__(self):
            self._kb = _KB()
            self._select_pad = None
            self._diacritic_pad = None
            self._deferred_base = {}
            self._click_repeat_at = {}
            self._click_settle_at = {}
            self._select_real_touch = False
            self._select_dir = 0
            self._select_base_dir = 0
            self._select_reverse_buffer = []
            self.sc_input_previous = _P(0)
            self.controller_state = _CS()
            self._prev = 0

        def _is_select_key(self, coord_frac):
            return False

        def frame(self, buttons, cf, raw_x, now, real_touch=True):
            self.sc_input_previous = _P(self._prev)
            r = self.handle_pad_input(
                cf,
                buttons,
                0x00000200,
                0x00000100,
                click_button_mask=0x00000400,
                allow_click=True,
                now=now,
                trigger_pressed=False,
                trigger_prev=False,
                raw_x=raw_x,
                real_touch=real_touch,
            )
            self._prev = buttons
            return r

    LPADTOUCH, LT, LPAD = 0x00000200, 0x00000100, 0x00000400
    a_layout = kb.get_key_layout(3, 1)  # 'a'
    assert a_layout is not None
    cf = CoordFraction.from_absolute(
        a_layout.x + a_layout.w // 2, a_layout.y + a_layout.h // 2
    )
    d = _D()
    t = 1000.0

    # 1) Press edge on 'a' -> NOTHING is typed (defer model): no click queued,
    #    the press coordinate is held in _deferred_base for the release.
    d.frame(LPADTOUCH | LT | LPAD, cf, 0, t)
    assert list(d.controller_state.click_queue) == []
    assert d._deferred_base == {LT: cf}

    # 2) Held past the hold delay -> the row opens instead of a key repeat.
    t += vkb.KEY_REPEAT_DELAY
    d.frame(LPADTOUCH | LT | LPAD, cf, 0, t)
    assert state.is_diacritic_open()
    assert list(d.controller_state.click_queue) == []
    sess = state.get_diacritic()
    assert sess is not None
    assert sess[0] == "a" and len(sess[1]) == 8 and sess[4] == "pad"

    # 3) Finger over candidate 2 in the row -> index 2.
    rx, ry, _rw, _rh = sess[3]
    t += 0.02
    d.frame(
        LPADTOUCH | LT | LPAD,
        CoordFraction.from_absolute(
            rx + 2 * (diacritics.CANDIDATE_W + diacritics.CANDIDATE_GAP),
            ry + 10,
        ),
        100,
        t,
    )
    assert state.get_diacritic_index() == 2

    # 4) Release over the row -> a variant commit item for the main thread.
    t += 0.02
    d.frame(
        LPADTOUCH,
        CoordFraction.from_absolute(
            rx + 2 * (diacritics.CANDIDATE_W + diacritics.CANDIDATE_GAP),
            ry + 10,
        ),
        100,
        t,
    )
    assert list(d.controller_state.click_queue) == [("variant", "â")]
    assert d._deferred_base == {}


def test_pad_release_with_finger_lifted_still_commits_and_unlatches():
    """CASE-B regression for the bumper (L1/R1) path: the finger lifts BEFORE
    the click releases, so on the release frame there is no touch bit at all
    (the click button drops its synthesized touch the same frame it releases).
    The variant must still commit, AND the pad must return to normal — the
    diacritic latch must clear, or every later press on that pad would only
    move the highlight and never type again."""
    from collections import deque

    from triton import config, screen, state, vkb
    from triton.pad import _PadMixin
    from triton.screen import CoordFraction

    screen.width, screen.height = 1286, 369
    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("keys", kb_config)
    kb = kb_config.construct()
    kb.update_dimensions()
    state.reset_session()
    state.set_virtual_kb(kb)
    state.set_diacritics_enabled(True)
    state.set_active_locale("en")

    class _KB:
        def __init__(self):
            self.downs = []

        def pressEvent(self, keys):
            self.downs.append(keys)

        def releaseEvent(self, keys):
            pass

    class _P:
        def __init__(self, buttons):
            self.buttons = buttons

    class _CS:
        def __init__(self):
            self.click_queue = deque()

    class _D(_PadMixin):
        _kb: _KB
        sc_input_previous: _P
        controller_state: _CS

        BACKSPACE_HOLD_DELAY = vkb.KEY_REPEAT_DELAY
        BACKSPACE_REPEAT = vkb.KEY_REPEAT_INTERVAL
        PAD_CLICK_SETTLE = 0.05

        def __init__(self):
            self._kb = _KB()
            self._select_pad = None
            self._diacritic_pad = None
            self._deferred_base = {}
            self._click_repeat_at = {}
            self._click_settle_at = {}
            self._select_real_touch = False
            self._select_dir = 0
            self._select_base_dir = 0
            self._select_reverse_buffer = []
            self.sc_input_previous = _P(0)
            self.controller_state = _CS()
            self._prev = 0

        def _is_select_key(self, coord_frac):
            return False

        def frame(self, buttons, cf, raw_x, now, real_touch=True):
            self.sc_input_previous = _P(self._prev)
            r = self.handle_pad_input(
                cf,
                buttons,
                0x00000200,
                0x00000100,
                click_button_mask=0x00000400,
                allow_click=True,
                now=now,
                trigger_pressed=False,
                trigger_prev=False,
                raw_x=raw_x,
                real_touch=real_touch,
            )
            self._prev = buttons
            return r

    LPADTOUCH, LT, LPAD = 0x00000200, 0x00000100, 0x00000400
    a_layout = kb.get_key_layout(3, 1)  # 'a'
    assert a_layout is not None
    cf = CoordFraction.from_absolute(
        a_layout.x + a_layout.w // 2, a_layout.y + a_layout.h // 2
    )
    d = _D()
    t = 1000.0

    # 1) Press edge -> nothing typed yet (defer model).
    d.frame(LPADTOUCH | LT | LPAD, cf, 0, t)
    assert list(d.controller_state.click_queue) == []

    # 2) Held past the hold delay -> the row opens.
    t += vkb.KEY_REPEAT_DELAY
    d.frame(LPADTOUCH | LT | LPAD, cf, 0, t)
    assert state.is_diacritic_open()
    sess = state.get_diacritic()
    assert sess is not None
    rx, ry, _rw, _rh = sess[3]

    # 3) Finger over candidate 2 -> index 2.
    t += 0.02
    d.frame(
        LPADTOUCH | LT | LPAD,
        CoordFraction.from_absolute(
            rx + 2 * (diacritics.CANDIDATE_W + diacritics.CANDIDATE_GAP),
            ry + 10,
        ),
        100,
        t,
    )
    assert state.get_diacritic_index() == 2

    # 4) CASE B release: the finger lifted and the click released on the same
    #    frame — NO touch bit at all. Must queue the variant commit (the
    #    natural "highlight, lift, release bumper" gesture) and clear the pad
    #    latch so the pad types again. (Closing the row is the main thread's
    #    job — process_click_queue -> commit_diacritic — exactly like the
    #    A-button path.)
    t += 0.02
    d.frame(
        0,
        CoordFraction.from_absolute(
            rx + 2 * (diacritics.CANDIDATE_W + diacritics.CANDIDATE_GAP),
            ry + 10,
        ),
        100,
        t,
        real_touch=False,
    )
    assert list(d.controller_state.click_queue) == [("variant", "â")]
    assert d._diacritic_pad is None
    assert d._deferred_base == {}
    # Drain the commit item (the main thread's commit_diacritic would do it).
    d.controller_state.click_queue.popleft()

    # 5) The pad must work normally again — the latch did NOT survive. A new
    #    press defers again (nothing queued), and its release types the base.
    t += 0.05
    d.frame(LPADTOUCH | LT | LPAD, cf, 0, t)
    assert list(d.controller_state.click_queue) == []
    t += 0.02
    d.frame(0, cf, 0, t, real_touch=False)
    q = d.controller_state.click_queue.popleft()
    assert isinstance(q, tuple) and q[0] == "deferred"
    key = kb.find_key_expanded(*q[1].to_absolute())
    assert key is not None and key.str == "a"


def test_pad_quick_tap_types_base_on_release():
    """Defer-model quick tap: press and release a variant-capable letter
    BEFORE the hold delay — no row ever opens, and the release types the base
    letter (exactly what a quick tap should do; nothing was typed at the
    press edge)."""
    from collections import deque

    from triton import config, screen, state, vkb
    from triton.pad import _PadMixin
    from triton.screen import CoordFraction

    screen.width, screen.height = 1286, 369
    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("keys", kb_config)
    kb = kb_config.construct()
    kb.update_dimensions()
    state.reset_session()
    state.set_virtual_kb(kb)
    state.set_diacritics_enabled(True)
    state.set_active_locale("en")

    class _KB:
        def pressEvent(self, keys):
            pass

        def releaseEvent(self, keys):
            pass

    class _P:
        def __init__(self, buttons):
            self.buttons = buttons

    class _CS:
        def __init__(self):
            self.click_queue = deque()

    class _D(_PadMixin):
        _kb: _KB
        sc_input_previous: _P
        controller_state: _CS

        BACKSPACE_HOLD_DELAY = vkb.KEY_REPEAT_DELAY
        BACKSPACE_REPEAT = vkb.KEY_REPEAT_INTERVAL
        PAD_CLICK_SETTLE = 0.05

        def __init__(self):
            self._kb = _KB()
            self._select_pad = None
            self._diacritic_pad = None
            self._deferred_base = {}
            self._click_repeat_at = {}
            self._click_settle_at = {}
            self._select_real_touch = False
            self._select_dir = 0
            self._select_base_dir = 0
            self._select_reverse_buffer = []
            self.sc_input_previous = _P(0)
            self.controller_state = _CS()
            self._prev = 0

        def _is_select_key(self, coord_frac):
            return False

        def frame(self, buttons, cf, raw_x, now, real_touch=True):
            self.sc_input_previous = _P(self._prev)
            r = self.handle_pad_input(
                cf,
                buttons,
                0x00000200,
                0x00000100,
                click_button_mask=0x00000400,
                allow_click=True,
                now=now,
                trigger_pressed=False,
                trigger_prev=False,
                raw_x=raw_x,
                real_touch=real_touch,
            )
            self._prev = buttons
            return r

    LPADTOUCH, LT, LPAD = 0x00000200, 0x00000100, 0x00000400
    a_layout = kb.get_key_layout(3, 1)  # 'a'
    assert a_layout is not None
    cf = CoordFraction.from_absolute(
        a_layout.x + a_layout.w // 2, a_layout.y + a_layout.h // 2
    )
    d = _D()
    t = 1000.0

    # Press edge: nothing typed (defer).
    d.frame(LPADTOUCH | LT | LPAD, cf, 0, t)
    assert list(d.controller_state.click_queue) == []
    assert d._deferred_base == {LT: cf}

    # Quick release well before the hold delay: the base letter types, no row
    # ever opened, no variant latch left behind.
    t += 0.05
    d.frame(0, cf, 0, t, real_touch=False)
    assert state.is_diacritic_open() is False
    assert d._diacritic_pad is None
    assert d._deferred_base == {}
    q = d.controller_state.click_queue.popleft()
    # Deferred release: a ("deferred", coord) marker so the main-thread
    # dispatch types the base WITHOUT a second click sound.
    assert isinstance(q, tuple) and q[0] == "deferred"
    key = kb.find_key_expanded(*q[1].to_absolute())
    assert key is not None and key.str == "a"


def test_pad_quick_tap_base_release_with_real_leftpad_values():
    """The real controller.py left-pad call passes allow_click=False (LT role
    is None when L2 isn't pressed) and trigger_pressed=False. The quick-tap
    base release must fire under EXACTLY those values — if the release
    condition accidentally depended on allow_click, every normal letter typed
    on the left pad would vanish on hardware (the reported bug)."""
    import applog

    applog.set_logging_enabled(False)
    from collections import deque

    from triton import config, screen, state, vkb
    from triton.pad import _PadMixin
    from triton.screen import CoordFraction

    screen.width, screen.height = 1286, 369
    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("keys", kb_config)
    kb = kb_config.construct()
    kb.update_dimensions()
    state.reset_session()
    state.set_virtual_kb(kb)
    state.set_diacritics_enabled(True)
    state.set_active_locale("en")

    class _KB:
        def pressEvent(self, keys):
            pass

        def releaseEvent(self, keys):
            pass

    class _P:
        def __init__(self, buttons):
            self.buttons = buttons

    class _CS:
        def __init__(self):
            self.click_queue = deque()

    class _D(_PadMixin):
        _kb: _KB
        sc_input_previous: _P
        controller_state: _CS

        BACKSPACE_HOLD_DELAY = vkb.KEY_REPEAT_DELAY
        BACKSPACE_REPEAT = vkb.KEY_REPEAT_INTERVAL
        PAD_CLICK_SETTLE = 0.05

        def __init__(self):
            self._kb = _KB()
            self._select_pad = None
            self._diacritic_pad = None
            self._deferred_base = {}
            self._click_repeat_at = {}
            self._click_settle_at = {}
            self._select_real_touch = False
            self._select_dir = 0
            self._select_base_dir = 0
            self._select_reverse_buffer = []
            self.sc_input_previous = _P(0)
            self.controller_state = _CS()
            self._prev = 0

        def _is_select_key(self, coord_frac):
            return False

        def frame(self, buttons, cf, raw_x, now, real_touch=True):
            self.sc_input_previous = _P(self._prev)
            r = self.handle_pad_input(
                cf,
                buttons,
                0x00000200,
                0x00000100,
                click_button_mask=0x00000400,
                allow_click=False,
                now=now,
                trigger_pressed=False,
                trigger_prev=False,
                raw_x=raw_x,
                real_touch=real_touch,
            )
            self._prev = buttons
            return r

    LPADTOUCH, LT, LPAD = 0x00000200, 0x00000100, 0x00000400
    a_layout = kb.get_key_layout(3, 1)  # 'a'
    assert a_layout is not None
    cf = CoordFraction.from_absolute(
        a_layout.x + a_layout.w // 2, a_layout.y + a_layout.h // 2
    )
    d = _D()
    t = 1000.0

    # Press edge with the real left-pad values: defer, nothing typed.
    d.frame(LPADTOUCH | LT | LPAD, cf, 0, t)
    assert list(d.controller_state.click_queue) == []
    assert d._deferred_base == {LT: cf}

    # Quick release (finger lifts with the click): base must type.
    t += 0.05
    d.frame(0, cf, 0, t, real_touch=False)
    assert d._deferred_base == {}
    q = d.controller_state.click_queue.popleft()
    assert isinstance(q, tuple) and q[0] == "deferred"
    key = kb.find_key_expanded(*q[1].to_absolute())
    assert key is not None and key.str == "a"


def test_open_diacritic_rc_refuses_impossible_row():
    """Finding-2 regression: a candidate strip wider than the window can't be
    clamped into it (pathological user map, or a narrow OSK), so
    variant_row_rect returns None. open_diacritic_rc must refuse (return
    False, no row opened) instead of letting a None rect crash
    state.open_diacritic's tuple(int(v) for v in rect) — which would kill the
    main loop AND the controller thread."""

    from triton import config, screen, state, vkb

    screen.width, screen.height = 200, 369  # tiny window
    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("keys", kb_config)
    kb = kb_config.construct()
    kb.update_dimensions()
    state.reset_session()
    state.set_virtual_kb(kb)
    state.set_diacritics_enabled(True)
    state.set_active_locale("en")

    # 'a' (row 2, col 1) has 8 built-in variants -> 254 px wide strip; a 200 px
    # window can't hold it. Must refuse cleanly, no exception, no open row.
    assert vkb.open_diacritic_rc(kb, 3, 1, "pad") is False
    assert state.is_diacritic_open() is False


def test_open_diacritic_rc_carries_base_case_into_variants():
    """Finding-3b regression: the base letter fired at the press edge follows
    the OS shift state, so a shift-held hold types an uppercase base. The
    commit backspaces the base and injects the variant — the variant row must
    carry that same case, or "A" would become lowercase "á" with no path to
    "Á"."""

    from triton import config, screen, state, vkb

    # The row case also follows OS Caps Lock; pin it OFF so this test is
    # deterministic regardless of the host machine's caps state.
    real_caps = state.is_caps_on
    state.is_caps_on = lambda: False
    try:
        screen.width, screen.height = 1286, 369
        kb_config = vkb.VirtualKeyboardConfig()
        layout = config.YamlFile("keyboard-layout.yaml")
        layout.read()
        layout.add_to_config("keys", kb_config)
        kb = kb_config.construct()
        kb.update_dimensions()
        state.reset_session()
        state.set_virtual_kb(kb)
        state.set_diacritics_enabled(True)
        state.set_active_locale("en")
        state.set_shift_held(True)

        assert vkb.open_diacritic_rc(kb, 3, 1, "pad") is True  # 'a'
        sess = state.get_diacritic()
        assert sess is not None
        # The variants row is uppercase, matching the uppercase base the hold
        # typed into the field.
        assert all(ch.isupper() for ch in sess[1])
        assert "Á" in sess[1]
        assert "á" not in sess[1]

        # Un-shifted, the row stays lowercase.
        state.set_shift_held(False)
        state.close_diacritic()
        assert vkb.open_diacritic_rc(kb, 3, 1, "pad") is True
        sess2 = state.get_diacritic()
        assert sess2 is not None
        assert all(ch.islower() for ch in sess2[1])
    finally:
        state.is_caps_on = real_caps
