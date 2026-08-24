"""Every bundled layout must type what its keys print — on EVERY machine.

A printable key can be correct two ways:

1. Plain scancode: its (label, shifted) pair equals exactly what the
   scancode produces under a stock US/UK Windows layout.
2. char-mode (`char: true` in the YAML): dispatch injects the printed label
   as a CHARACTER — VkKeyScanW chord on whatever layout is active,
   KEYEVENTF_UNICODE fallback — so the label lands regardless of the OS
   input language.

The invariant below pins BOTH rules for every cell of every board. This is
the test class that would have caught both historical bugs: "dvorak o types
s" (label/keycode drift) and "azerty $ types ]" (symbol rows trusted a
French OS keymap the user's machine didn't have). Nothing escapes it:
every single-character generic key of every board must satisfy rule 1 or
rule 2, or the suite fails naming the exact key.
"""

import pytest
import steamcontroller.uinput as sui

from triton import state, vkb
from triton.layouts import layout_filename


def _board(name):
    from triton import config

    cfg = config.YamlFile(layout_filename(name))
    cfg.read()
    cfg.add_to_config("keys", vkb.VirtualKeyboardConfig())
    kb = vkb.VirtualKeyboardConfig().construct()
    kb.update_dimensions()
    return kb


def _cells(kb):
    for row in kb.keys:
        yield from row


def _table(pairs):
    return {getattr(sui.Keys, code): pair for code, pair in pairs.items()}


def _us_table():
    table = {}
    for digit, shifted in zip("1234567890", "!@#$%^&*()"):
        table[f"KEY_{digit}"] = (digit, shifted)
    for ch in "abcdefghijklmnopqrstuvwxyz":
        table[f"KEY_{ch.upper()}"] = (ch, ch.upper())
    table.update(
        {
            "KEY_GRAVE": ("`", "~"),
            "KEY_MINUS": ("-", "_"),
            "KEY_EQUAL": ("=", "+"),
            "KEY_LEFTBRACE": ("[", "{"),
            "KEY_RIGHTBRACE": ("]", "}"),
            "KEY_BACKSLASH": ("\\", "|"),
            "KEY_SEMICOLON": (";", ":"),
            "KEY_APOSTROPHE": ("'", '"'),
            "KEY_COMMA": (",", "<"),
            "KEY_DOT": (".", ">"),
            "KEY_SLASH": ("/", "?"),
        }
    )
    return _table(table)


US = _us_table()

STRICT_BOARDS = ("QWERTY", "Dvorak", "Colemak", "ABC")
ALL_BOARDS = STRICT_BOARDS + ("AZERTY", "QWERTZ")

# Native characters every board variant MUST offer (base or shifted labels)
# — the whole point of these boards.
AZERTY_REQUIRED = '²&é"' + "'(-è_çà)°=+^¨$£*µù%,?;.:/!§"
QWERTZ_REQUIRED = '^°!"§$%/()=ß?´`üÜ+*#\'öÖäÄyz;,.:-_'


def _labels(kb):
    out = set()
    for key in _cells(kb):
        out.add(key.str)
        if key.shifted:
            out.add(key.shifted)
    return out


def test_letters_always_match_their_scancode():
    """Letter keys ride their own scancodes on EVERY board (a on KEY_A …),
    so they type true under any Latin Windows layout — never drift. A
    SYMBOL label on a letter scancode is only legal in char-mode: language
    boards mirror their physical keyboard (AZERTY prints ',' on the US-M
    position), and injection makes it type true anywhere."""
    for name in ALL_BOARDS:
        for key in _cells(_board(name)):
            pair = US.get(key.keycode)
            if pair is None or not pair[0].isalpha():
                continue
            base, shifted = pair
            if key.str.isalpha():
                assert key.str == base, (
                    f"{name}: key prints {key.str!r} but its scancode types "
                    f"{base!r}"
                )
            else:
                assert key.char_mode, (
                    f"{name}: symbol {key.str!r} on scancode {base!r} needs "
                    f"`char: true` (it would type {base!r})"
                )
            if key.shifted is not None and key.shifted.isalpha():
                assert key.shifted == shifted, (
                    f"{name}: shift-label above {base!r} should be "
                    f"{shifted!r}, got {key.shifted!r}"
                )


@pytest.mark.parametrize("name", ALL_BOARDS)
def test_every_printable_cell_types_its_label(name):
    """THE invariant: every single-character generic key of every board is
    EITHER char-mode (injects the printed label) OR pairs with a scancode
    whose stock-US output IS the printed label. No third option."""
    for key in _cells(_board(name)):
        if key.modifier or key.str is None or len(key.str) != 1:
            continue
        if getattr(key.callback, "__name__", "") != "on_key_generic":
            continue
        if key.char_mode:
            continue  # rule 2: dispatch injects the label itself
        pair = US.get(key.keycode)
        assert pair is not None, (
            f"{name}: key {key.str!r} is neither char-mode nor a known US "
            f"scancode ({key.keycode}) — it would type something else"
        )
        base, shifted = pair
        assert key.str == base, (
            f"{name}: key prints {key.str!r} but its scancode types "
            f"{base!r} (mark it `char: true` or fix the pairing)"
        )
        assert key.shifted == shifted, (
            f"{name}: shift-label above {key.str!r} should be {shifted!r}, "
            f"got {key.shifted!r} (mark it `char: true` or fix the pairing)"
        )


@pytest.mark.parametrize("name", STRICT_BOARDS)
def test_strict_boards_need_neither_overrides_nor_char_mode(name):
    """QWERTY/Dvorak/Colemak/ABC assume a stock US/UK input language: every
    typewriter key matches the US table and NONE of them needs injection."""
    expected = US
    checked = set()
    for key in _cells(_board(name)):
        pair = expected.get(key.keycode)
        if pair is None:
            continue  # F-keys, Esc, Backspace, arrows… aren't in the table.
        assert not key.char_mode, (
            f"{name}: {key.str!r} carries a redundant `char: true`"
        )
        base, shifted = pair
        assert key.str == base, (
            f"{name}: key prints {key.str!r} but its scancode types "
            f"{base!r}"
        )
        assert key.shifted == shifted, (
            f"{name}: shift-label above {base!r} should be {shifted!r}, "
            f"got {key.shifted!r}"
        )
        checked.add(key.keycode)
    letter_codes = {
        code
        for code in expected
        if code.startswith("KEY_") and len(code) == 5 and code[4].isalpha()
    }
    assert letter_codes <= checked, (
        f"{name}: letter scancodes missing from the board: "
        f"{sorted(code for code in letter_codes - checked)}"
    )


@pytest.mark.parametrize(
    ("name", "required"),
    [("AZERTY", AZERTY_REQUIRED), ("QWERTZ", QWERTZ_REQUIRED)],
)
def test_native_characters_are_all_present(name, required):
    """Each language board offers its FULL native character set (as base or
    shifted labels) — no silently missing é, ß, £, § …"""
    have = _labels(_board(name))
    missing = [ch for ch in required if ch not in have]
    assert not missing, f"{name}: native characters missing: {missing}"


def test_char_mode_key_injects_its_label(monkeypatch):
    """The user's bug, end to end: on a machine whose OS layout has no `$`
    (VkKeyScanW misses), pressing the AZERTY `$` key still types `$` via the
    KEYEVENTF_UNICODE fallback."""
    import applog
    import ctypes

    applog.set_logging_enabled(False)

    kb = _board("AZERTY")
    dollar = next(k for k in _cells(kb) if k.str == "$")
    assert dollar.char_mode, "the $ key must be char-mode"

    calls = {}
    user32 = sui._U32
    real_send = user32.SendInput
    real_vkscan = user32.VkKeyScanW
    real_openclip = user32.OpenClipboard

    def _stub_vkscan(_ch):
        return -1  # active layout has no such key -> UNICODE fallback

    def _stub_openclipboard(_hwnd):
        return 0  # paste path refuses -> UNICODE fallback

    def fake_send(n, inputs, cb):
        arr = ctypes.cast(inputs, ctypes.POINTER(sui._INPUT))
        calls["kis"] = [
            (arr[i].u.ki.wVk, int(arr[i].u.ki.wScan), int(arr[i].u.ki.dwFlags))
            for i in range(int(n))
        ]
        return int(n)

    monkeypatch.setattr(user32, "VkKeyScanW", _stub_vkscan, raising=False)
    monkeypatch.setattr(user32, "OpenClipboard", _stub_openclipboard, raising=False)
    monkeypatch.setattr(user32, "SendInput", fake_send, raising=False)
    try:
        state.reset_session()
        assert state.is_shift_held() is False
        vkb.dispatch_key(None, dollar)
    finally:
        # Restore even on failure so later tests keep the real functions.
        monkeypatch.undo()

    assert calls["kis"] == [(0, ord("$"), 0x0004), (0, ord("$"), 0x0006)]
