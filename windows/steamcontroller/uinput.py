"""Windows replacement for the Linux uinput key sender.
Maps the KEY_* names used by triton to pynput keys and sends them via the
OS-level injection layer so the keystrokes land in whichever window is
focused (which on Windows is whatever the user had focused before triton
started, since the SDL2 window doesn't steal focus unless clicked)."""

import ctypes
import time

from pynput.keyboard import Controller as _Controller
from pynput.keyboard import Key as _Key
from pynput.keyboard import KeyCode as _KeyCode
from pynput.mouse import Button as _MouseButton
from pynput.mouse import Controller as _MouseController

_MOUSE_BUTTONS = {
    "left": _MouseButton.left,
    "right": _MouseButton.right,
    "middle": _MouseButton.middle,
}

# PRIVATE handles for our raw SendInput / clipboard work. ctypes.windll.user32
# is a PROCESS-WIDE singleton that pynput ALSO binds to (pynput._util.win32
# exposes the same function objects, e.g. VkKeyScan IS user32.VkKeyScanW).
# Setting restype/argtypes on the shared handle breaks pynput's next call with
# "'str' object cannot be interpreted as an integer" (every normal letter dies
# after the first variant paste — the bug the logs proved). These private
# handles isolate our argtypes mutations from pynput.
_U32 = ctypes.WinDLL("user32.dll", use_last_error=False)
_K32 = ctypes.WinDLL("kernel32.dll", use_last_error=False)


class _KeysProxy:
    """`Keys[name]` and `Keys.NAME` both return the name string so the rest of
    the code can pass keycodes around as strings and we resolve them inside
    Keyboard.pressEvent / releaseEvent."""

    def __getitem__(self, name):
        return name

    def __getattr__(self, name):
        return name


Keys = _KeysProxy()


def _build_keymap():
    m = {}
    for c in "abcdefghijklmnopqrstuvwxyz":
        m["KEY_" + c.upper()] = _KeyCode.from_char(c)
    for d in "0123456789":
        m["KEY_" + d] = _KeyCode.from_char(d)
    m.update(
        {
            "KEY_SPACE": _Key.space,
            "KEY_ENTER": _Key.enter,
            "KEY_BACKSPACE": _Key.backspace,
            "KEY_TAB": _Key.tab,
            "KEY_ESC": _Key.esc,
            "KEY_CAPSLOCK": _Key.caps_lock,
            "KEY_LEFTSHIFT": _Key.shift,
            "KEY_RIGHTSHIFT": _Key.shift_r,
            "KEY_LEFTCTRL": _Key.ctrl,
            "KEY_RIGHTCTRL": _Key.ctrl_r,
            "KEY_LEFTALT": _Key.alt,
            "KEY_RIGHTALT": _Key.alt_r,
            "KEY_LEFTMETA": _Key.cmd,  # Windows / Super key
            "KEY_LEFTWIN": _Key.cmd,
            "KEY_MINUS": _KeyCode.from_char("-"),
            "KEY_EQUAL": _KeyCode.from_char("="),
            "KEY_DOT": _KeyCode.from_char("."),
            "KEY_COMMA": _KeyCode.from_char(","),
            "KEY_SLASH": _KeyCode.from_char("/"),
            "KEY_BACKSLASH": _KeyCode.from_char("\\"),
            "KEY_SEMICOLON": _KeyCode.from_char(";"),
            "KEY_APOSTROPHE": _KeyCode.from_char("'"),
            "KEY_GRAVE": _KeyCode.from_char("`"),
            "KEY_LEFTBRACE": _KeyCode.from_char("["),
            "KEY_RIGHTBRACE": _KeyCode.from_char("]"),
            "KEY_QUESTION": _KeyCode.from_char("?"),
            "KEY_LEFT": _Key.left,
            "KEY_RIGHT": _Key.right,
            "KEY_UP": _Key.up,
            "KEY_DOWN": _Key.down,
            "KEY_PAGEUP": _Key.page_up,
            "KEY_PAGEDOWN": _Key.page_down,
            "KEY_HOME": _Key.home,
            "KEY_END": _Key.end,
            "KEY_DELETE": _Key.delete,
            "KEY_INSERT": _Key.insert,
            "KEY_PRINTSCREEN": _Key.print_screen,
            "KEY_SCROLLLOCK": _Key.scroll_lock,
            "KEY_PAUSE": _Key.pause,
            "KEY_NUMLOCK": _Key.num_lock,
            "KEY_F1": _Key.f1,
            "KEY_F2": _Key.f2,
            "KEY_F3": _Key.f3,
            "KEY_F4": _Key.f4,
            "KEY_F5": _Key.f5,
            "KEY_F6": _Key.f6,
            "KEY_F7": _Key.f7,
            "KEY_F8": _Key.f8,
            "KEY_F9": _Key.f9,
            "KEY_F10": _Key.f10,
            "KEY_F11": _Key.f11,
            "KEY_F12": _Key.f12,
            # Media transport keys (driven by the Steam + left-stick chords).
            "KEY_VOLUMEUP": _Key.media_volume_up,
            "KEY_VOLUMEDOWN": _Key.media_volume_down,
            "KEY_MUTE": _Key.media_volume_mute,
            "KEY_PREVIOUSSONG": _Key.media_previous,
            "KEY_NEXTSONG": _Key.media_next,
            "KEY_PLAYPAUSE": _Key.media_play_pause,
        }
    )
    return m


_KEYMAP = _build_keymap()


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUT_UNION)]


class Keyboard:
    def __init__(self):
        self._kb = _Controller()

    def _resolve(self, code):
        if isinstance(code, str):
            return _KEYMAP.get(code)
        return None

    def pressEvent(self, keys):
        for code in keys:
            k = self._resolve(code)
            if k is None:
                continue
            try:
                self._kb.press(k)
            except Exception as e:
                # A windowed exe has no stdout — mirror into dualtouch.log so a
                # pynput failure on NORMAL letters is visible (this is the exact
                # seam where a desync can silently eat letters).
                print(f"uinput: press {code!r} failed: {e}")
                self._diag(f"press {code!r} FAILED: {e!r}")

    def releaseEvent(self, keys):
        for code in keys:
            k = self._resolve(code)
            if k is None:
                continue
            try:
                self._kb.release(k)
            except Exception as e:
                print(f"uinput: release {code!r} failed: {e}")
                self._diag(f"release {code!r} FAILED: {e!r}")

    def reset_shift_state(self):
        """Drop pynput's INTERNAL shift/caps bookkeeping without sending any
        OS key event.

        pynput's Controller._resolve() uppercases any character key while this
        instance's shift_pressed is True, and shift_pressed is INSTANCE-LOCAL
        (Key.shift in _modifiers, or _caps_lock). A Shift pressed on this
        instance and released on the controller thread's SEPARATE instance
        (the paste/emoji/arrow re-press, see vkb.on_key_paste), or a single tap
        of the on-screen Caps key (whose OS-side effect PowerToys remaps away),
        leaves this instance believing Shift is held forever — so every letter
        comes out uppercase regardless of the real OS shift state. Clearing the
        internal flags here makes the injected letter's case follow the REAL
        OS shift only."""
        try:
            kb = self._kb
            # pynput internals (exist at runtime; absent from its stubs).
            with kb._modifiers_lock:  # type: ignore[reportAttributeAccessIssue]
                for mod in (
                    kb._Key.shift.value,
                    kb._Key.shift_l.value,
                    kb._Key.shift_r.value,
                ):
                    kb._modifiers.discard(mod)  # type: ignore[reportAttributeAccessIssue]
            kb._caps_lock = False  # type: ignore[reportAttributeAccessIssue]
        except Exception:
            pass

    def reset_modifier_state(self):
        """Clear pynput's ENTIRE internal modifier bookkeeping (Shift, Ctrl,
        Alt, Meta) without sending OS events. The OSK's raw SendInput paths
        (variant paste Ctrl+V, AltGr chords) do NOT update pynput's internal
        _modifiers set, so a stale entry there makes every subsequent NORMAL
        letter (which goes through pynput) type as if a modifier were held —
        "nothing shows up", surviving OSK reopen because the module-global
        pynput Keyboard is never recreated, and physical keys don't clear
        internal state. Called at OSK open so a fresh session starts clean."""
        try:
            kb = self._kb
            # pynput internals (exist at runtime; absent from its stubs).
            with kb._modifiers_lock:  # type: ignore[reportAttributeAccessIssue]
                kb._modifiers.clear()  # type: ignore[reportAttributeAccessIssue]
            kb._caps_lock = False  # type: ignore[reportAttributeAccessIssue]
        except Exception:
            pass

    def force_caps_off(self):
        """Ensure OS Caps Lock is OFF, so the OSK types lowercase by default
        and Shift alone controls capitalization.

        The OSK Caps key / L3 send KEY_CAPSLOCK, which PowerToys Keyboard
        Manager remaps to Esc (Caps -> Esc), so the OS caps state can never
        be toggled through the OSK — if caps is ON (e.g. toggled on the
        physical keyboard), every typed letter comes out uppercase and the
        OSK can't turn it off. This forces caps OFF by sending the Caps
        hardware SCAN CODE (wVk=0, KEYEVENTF_SCANCODE) via SendInput when
        the current state is ON — PowerToys has no virtual key to remap,
        so the real Caps Lock toggles off."""
        try:
            # Private handle: pynput shares ctypes.windll.user32 and binds
            # SendInput to it — our argtypes mutation would break pynput.
            user32 = _U32
            user32.GetKeyState.restype = ctypes.c_short
            user32.GetKeyState.argtypes = [ctypes.c_int]
            if not (user32.GetKeyState(0x14) & 1):
                return  # caps already off
            ins = (_INPUT * 2)()
            ins[0].type = 1
            ins[0].u.ki.wVk = 0
            ins[0].u.ki.wScan = 0x3A
            ins[0].u.ki.dwFlags = 0x0008  # KEYEVENTF_SCANCODE
            ins[1].type = 1
            ins[1].u.ki.wVk = 0
            ins[1].u.ki.wScan = 0x3A
            ins[1].u.ki.dwFlags = 0x0008 | 0x0002  # SCANCODE | KEYUP
            user32.SendInput.restype = ctypes.c_uint
            user32.SendInput.argtypes = [
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            user32.SendInput(
                2, ctypes.cast(ins, ctypes.c_void_p), ctypes.sizeof(_INPUT) * 2
            )
        except Exception:
            pass

    def tap_with_modifier(self, modifier_code, key_code):
        """Press modifier+key as a true virtual-key chord, then release it.

        pynput injects printable character keys (KeyCode.from_char, vk=None) as
        char/Unicode events that DON'T combine with a held modifier — so Ctrl+'v'
        (paste) or Win+'.' (emoji) come through as a plain 'v' / '.' instead of
        the shortcut (and inconsistently, since the char→vk resolution is
        state-dependent). Resolving the char to its raw virtual key via
        VkKeyScan and pressing THAT makes it combine with the modifier reliably.
        """
        mod = self._resolve(modifier_code)
        key = self._resolve(key_code)
        if mod is None or key is None:
            return
        try:
            ch = getattr(key, "char", None)
            if ch:
                vk = _U32.VkKeyScanW(ord(ch)) & 0xFF
                if vk not in (0, 0xFF):
                    key = _KeyCode.from_vk(vk)
        except Exception:
            pass
        try:
            self._kb.press(mod)
            time.sleep(0.01)  # let the OS register the modifier first
            self._kb.press(key)
            self._kb.release(key)
            self._kb.release(mod)
        except Exception as e:
            print(f"uinput: tap_with_modifier {key_code!r} failed: {e}")

    def tap_char(self, char):
        """Type a single Unicode character.

        Three injection paths, tried in order:
          1. Real virtual key (VkKeyScanW) — accepted by EVERY app that handles
             normal typing (games, raw-input apps). Only used when the char's
             key is plain or shift-only on the active layout: an AltGr form
             (state bits CTRL|ALT = 0x06, e.g. Polish ą ę ś ż) must NOT be
             tapped as the bare VK, which would type the unaccented letter.
          2. Clipboard paste (Ctrl+V) — works where UNICODE is ignored: games
             commonly drop KEYEVENTF_UNICODE while still handling Ctrl+V.
          3. SendInput KEYEVENTF_UNICODE — for apps that accept WM_CHAR.
        Returns True if any path injected the char."""
        if not char or ord(char) > 0xFFFF:
            return False
        try:
            # PRIVATE handle: ctypes.windll.user32 is a shared singleton that
            # pynput also binds to (pynput._util.win32.VkKeyScan IS
            # user32.VkKeyScanW). Mutating its argtypes here would make pynput's
            # next letter call fail with "'str' object cannot be interpreted as
            # an integer" — the exact "letters stop after a variant" bug. Load a
            # private copy so our restype/argtypes can't leak into pynput.
            user32 = _U32
            user32.VkKeyScanW.restype = ctypes.c_short
            user32.VkKeyScanW.argtypes = [ctypes.c_uint]
            scan = user32.VkKeyScanW(ord(char)) & 0xFFFF
            state_bits = (scan >> 8) & 0xFF
            # Plain (0x00) or shift-only (0x01) keys are safe to tap as the
            # bare VK. AltGr (0x06) or other modifier forms are NOT — the VK
            # alone produces the wrong (unaccented) letter, so skip to paste.
            # VkKeyScanW modifier byte: 0x00 = plain, 0x01 = shift, 0x06 =
            # AltGr (Ctrl+Alt). Only plain and shift-only forms are safe to
            # tap as a bare VK — a synthesized Ctrl+Alt chord is NOT accepted
            # as AltGr by most apps (they recognize AltGr by its SCANCODE, so
            # the chord degrades to the unaccented letter, e.g. ś → s; the log
            # proves it). AltGr forms (0x06) fall through to paste/UNICODE,
            # which type the exact char.
            if scan != 0xFFFF and state_bits in (0x00, 0x01):
                # The case-carrying row already uppercased the variant when
                # Shift is logically held, so the char's VkKeyScanW shift bit
                # and the REAL held-Shift state agree. Only synthesize Shift
                # when it's actually needed AND not already held (L2/latched
                # Shift holds it on the OS) — pressing VK_SHIFT down/up over a
                # held shift would drop the user's shift early.
                shift_needed = state_bits & 0x01
                shift_held = bool(user32.GetAsyncKeyState(0x10) & 0x8000)
                ok = self._tap_vk(
                    scan & 0xFF, bool(shift_needed) and not shift_held
                )
                self._diag(
                    f"tap_char U+{ord(char):04X} vk 0x{scan:04x} state={state_bits} ok={ok}"
                )
                return ok
        except Exception as e:
            print(f"uinput: tap_char {char!r} vk failed: {e}")
        # UNICODE first: it provably works in editors/File Explorer (the log's
        # 'á unicode ok=True'), types the exact char, and has no clipboard
        # side effects. Paste is only the fallback for apps that hard-reject
        # UNICODE (sent != 2).
        ok = self._send_unicode(char)
        if ok:
            self._diag(f"tap_char U+{ord(char):04X} unicode ok=True")
            return True
        if self._paste_char(char):
            self._diag(f"tap_char U+{ord(char):04X} pasted ok")
            # The paste sent raw Ctrl+V via SendInput, which pynput (used for
            # NORMAL letters) does not track. Clear its internal modifier set
            # so the next normal letter isn't typed with a phantom Ctrl.
            self.reset_modifier_state()
            return True
        self._diag(f"tap_char U+{ord(char):04X} unicode ok=False (no path)")
        return False

    def _diag(self, msg):
        """Mirror an injection diagnostic into dualtouch.log (gated by the
        tray logging toggle, via the applog write path). The exe is windowed
        with no stdout, so without this the uinput print()s are invisible and
        the failure path can only be guessed."""
        try:
            from applog import log_line

            log_line("uinput", msg)
        except Exception:
            pass

    def _send_unicode(self, char):
        """Inject `char` as a KEYEVENTF_UNICODE SendInput pair. Layout-
        independent, but many games/raw-input apps ignore UNICODE events.

        Uses a PRIVATE user32 handle: pynput also binds to the shared
        ctypes.windll.user32, and our restype/argtypes mutations would break
        pynput's next SendInput/VkKeyScan call."""
        try:
            ins = (_INPUT * 2)()
            for i, flags in enumerate((0x0004, 0x0004 | 0x0002)):
                ins[i].type = 1
                ins[i].u.ki.wVk = 0
                ins[i].u.ki.wScan = ord(char)
                ins[i].u.ki.dwFlags = flags
            user32 = _U32
            user32.SendInput.restype = ctypes.c_uint
            user32.SendInput.argtypes = [
                ctypes.c_uint,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            sent = user32.SendInput(
                2, ctypes.cast(ins, ctypes.c_void_p), ctypes.sizeof(_INPUT) * 2
            )
            if sent != 2:
                # Record the actual result + error so the log pinpoints WHY
                # injection fails (UIPI block, throttling, queue busy, ...).
                # GetLastError lives on kernel32, not user32.
                kernel32 = _K32
                kernel32.GetLastError.restype = ctypes.c_ulong
                kernel32.GetLastError.argtypes = []
                self._diag(
                    f"send_unicode U+{ord(char):04X} sent={int(sent)} err={int(kernel32.GetLastError())}"
                )
            return sent == 2
        except Exception as e:
            print(f"uinput: tap_char {char!r} unicode failed: {e}")
            return False

    def _paste_char(self, char):
        """Type `char` via the clipboard: snapshot whatever text is on the
        clipboard, replace it with `char`, send Ctrl+V, then restore the
        snapshot. Games and raw-input apps that ignore KEYEVENTF_UNICODE still
        accept Ctrl+V, so this is the reliable fallback for accents with no
        plain VK on the active layout.

        Runs on the main (SDL) thread, so it MUST never block: OpenClipboard
        can wait indefinitely when another process holds the clipboard, which
        would freeze the whole OSK (and base-letter injection). Every clipboard
        open is guarded by a short retry loop and abandoned on timeout.

        Uses PRIVATE user32/kernel32 handles: mutating the shared
        ctypes.windll functions' argtypes would break pynput (same objects)."""
        user32 = _U32
        kernel32 = _K32
        # Without restype/argtypes, ctypes coerces the returned 64-bit HGLOBAL
        # handle to c_int — truncated, so GlobalLock/SetClipboardData get a
        # garbage handle and paste always fails. Declare them once here.
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = ctypes.c_int
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.GetClipboardData.restype = ctypes.c_void_p
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.OpenClipboard.restype = ctypes.c_int
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.CloseClipboard.restype = ctypes.c_int
        user32.CloseClipboard.argtypes = []
        user32.EmptyClipboard.restype = ctypes.c_int
        user32.EmptyClipboard.argtypes = []
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        prev = None

        def _open_clipboard():
            for _ in range(10):
                if user32.OpenClipboard(None):
                    return True
                time.sleep(0.01)
            return False

        def _read():
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            if not _open_clipboard():
                return None
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return None
                p = kernel32.GlobalLock(h)
                if not p:
                    return None
                try:
                    return ctypes.wstring_at(p)
                finally:
                    kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()

        def _write(text):
            if not _open_clipboard():
                return False
            try:
                user32.EmptyClipboard()
                buf = ctypes.create_unicode_buffer(text or "")
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(buf))
                if not h:
                    return False
                p = kernel32.GlobalLock(h)
                try:
                    ctypes.memmove(p, buf, ctypes.sizeof(buf))
                finally:
                    kernel32.GlobalUnlock(h)
                user32.SetClipboardData(CF_UNICODETEXT, h)
                return True
            except Exception:
                return False
            finally:
                # MUST always close: a process that leaves the clipboard open
                # locks it system-wide (OpenClipboard by ANY app then blocks),
                # which breaks paste everywhere AND can look like input died.
                user32.CloseClipboard()

        try:
            # Never leave a throttled Ctrl down from an earlier attempt.
            self._release_stuck_ctrl()
            prev = _read()
            if not _write(char):
                return False
            # The paste (Ctrl+V) and the restore are wrapped so the user's
            # clipboard is ALWAYS put back — even if the paste throws or the
            # process is killed mid-way, `prev` is restored in the finally.
            # Replacing the clipboard with `char` and dying before restoring
            # would silently destroy the user's clipboard contents.
            ok = False
            try:
                ok = self._tap_vk(0x56, ctrl=True)  # Ctrl+V
            finally:
                # Release Ctrl again in case the tap's key-up was throttled.
                self._release_stuck_ctrl()
                if prev is not None:
                    try:
                        # Give the focused app a moment to consume the paste
                        # before restoring the clipboard. Keep it short —
                        # this runs on the main thread.
                        time.sleep(0.02)
                        _write(prev)
                    except Exception:
                        pass
            return ok
        except Exception as e:
            print(f"uinput: paste {char!r} failed: {e}")
            return False

    def _tap_vk(self, vk, shift=False, ctrl=False, altgr=False):
        """Tap a raw virtual key (optionally with Shift / Ctrl held, or as an
        AltGr chord — Ctrl+Alt held). Sends via SendInput KEYBDINPUTs (wVk
        path), which games and raw-input apps accept even though they ignore
        KEYEVENTF_UNICODE.

        Each event is its own SendInput call and modifier key-UPs always fire,
        so a throttled/dropped event can never leave a modifier physically
        held down (the "all typing dies" bug). Modifier downs and ups are
        separate calls precisely so a partial delivery can't strand the key."""
        try:

            def _ev(wVk, flags):
                e = _INPUT()
                e.type = 1
                e.u.ki.wVk = wVk
                e.u.ki.wScan = 0
                e.u.ki.dwFlags = flags
                return e

            def _send(items):
                if not items:
                    return True
                ins = (_INPUT * len(items))(*items)
                # Private handle: pynput shares ctypes.windll.user32 and its
                # SendInput binding — mutating argtypes would break pynput.
                user32 = _U32
                user32.SendInput.restype = ctypes.c_uint
                user32.SendInput.argtypes = [
                    ctypes.c_uint,
                    ctypes.c_void_p,
                    ctypes.c_int,
                ]
                sent = user32.SendInput(
                    len(items),
                    ctypes.cast(ins, ctypes.c_void_p),
                    ctypes.sizeof(_INPUT) * len(items),
                )
                return sent == len(items)

            mods_down = []
            if shift:
                mods_down.append(_ev(0x10, 0))  # VK_SHIFT down
            if altgr:
                mods_down.append(_ev(0x11, 0))  # VK_CONTROL down
                mods_down.append(_ev(0x12, 0))  # VK_MENU down
            elif ctrl:
                mods_down.append(_ev(0x11, 0))  # VK_CONTROL down
            mods_up = []
            if altgr:
                mods_up.append(_ev(0x12, 0x0002))  # VK_MENU up
                mods_up.append(_ev(0x11, 0x0002))  # VK_CONTROL up
            elif ctrl:
                mods_up.append(_ev(0x11, 0x0002))  # VK_CONTROL up
            if shift:
                mods_up.append(_ev(0x10, 0x0002))  # VK_SHIFT up

            ok = True
            if mods_down:
                ok = _send(mods_down) and ok
            ok = _send([_ev(vk, 0)]) and ok
            ok = _send([_ev(vk, 0x0002)]) and ok
            if mods_up:
                ok = _send(mods_up) and ok
            return ok
        except Exception as e:
            print(f"uinput: tap_vk 0x{vk:02x} failed: {e}")
            return False

    def _release_stuck_ctrl(self):
        """Repair path: force every modifier key up if it is physically down.
        A throttled/dropped modifier key-up from an earlier injection leaves
        the OS key held, turning every later letter into a Ctrl/Shift/Alt
        shortcut (nothing types) — the exact symptom that survives OSK reopen
        because it is OS key state. A key-up on a not-held key is harmless
        (idempotent), so this is safe to run before each paste and at open.
        Uses keybd_event so the release itself cannot be throttled away."""
        try:
            # Private handle: pynput shares ctypes.windll.user32.
            user32 = _U32
            user32.GetAsyncKeyState.restype = ctypes.c_short
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            KEYUP = 0x0002
            # VK_CONTROL and VK_MENU only — NOT VK_SHIFT: Shift is legitimately
            # held by the user (L2 hold / latched Shift) while typing, and
            # releasing it here would drop the user's shift mid-press. Ctrl and
            # Alt are never intentionally held by the OSK outside a transient
            # paste, so an unexpected held Ctrl/Alt is always a stuck key.
            # Sent via SendInput (one KEYUP per key, matching _tap_vk).
            for vk in (0x11, 0x12, 0xA2, 0xA3, 0xA4, 0xA5):
                if user32.GetAsyncKeyState(vk) & 0x8000:
                    e = _INPUT()
                    e.type = 1
                    e.u.ki.wVk = vk
                    e.u.ki.wScan = 0
                    e.u.ki.dwFlags = KEYUP
                    user32.SendInput.restype = ctypes.c_uint
                    user32.SendInput.argtypes = [
                        ctypes.c_uint,
                        ctypes.c_void_p,
                        ctypes.c_int,
                    ]
                    user32.SendInput(
                        1,
                        ctypes.cast(ctypes.byref(e), ctypes.c_void_p),
                        ctypes.sizeof(_INPUT),
                    )
        except Exception:
            pass


class Mouse:
    """Thin wrapper over pynput's mouse for relative cursor movement, so the
    stick-as-mouse code can stay symmetric with the Keyboard wrapper."""

    def __init__(self):
        self._m = _MouseController()

    def move(self, dx, dy):
        if not dx and not dy:
            return
        try:
            self._m.move(int(dx), int(dy))
        except Exception as e:
            print(f"uinput: mouse move ({dx},{dy}) failed: {e}")

    def press(self, button="left"):
        try:
            self._m.press(_MOUSE_BUTTONS[button])
        except Exception as e:
            print(f"uinput: mouse press {button} failed: {e}")

    def release(self, button="left"):
        try:
            self._m.release(_MOUSE_BUTTONS[button])
        except Exception as e:
            print(f"uinput: mouse release {button} failed: {e}")
