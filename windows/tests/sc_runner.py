"""SceneRunner — GdUnit4-style scene runner for the DualTouch OSK.

Drives the REAL production pipeline headlessly: every pushed frame flows
through controller.update() -> ControllerManager.handle_input() and then
triton.drain_input_work() — exactly what triton.main()'s loop runs each
iteration, minus the SDL window/render. OS injection surfaces (pynput
keyboard/mouse) are replaced by recorders, so tests assert typed output at
the OS boundary without touching the real desktop.

Layers built on this module:
  • Layer 1 — pure state-machine tests (vkb.step_cursor etc.), no runner.
  • Layer 2 — synthetic input injection through SceneRunner (this file).
"""

import steamcontroller.uinput as sui
from steamcontroller import (
    SCButtons,
    SCStatus,
    SteamControllerInput,
)
from triton import config, controller, diacritics, screen, state, vkb, vptr
from triton.screen import CoordFraction
from triton.triton import drain_input_work

# Frame cadence of the fake input thread (the real merger polls ~250 Hz;
# 60 Hz keeps hold-to-repeat math readable in whole frames).
FRAME = 1.0 / 60

# Raw-pad inverse of controller.adjust_raw_x/y (scalar 6/5 baked in).
_X_SCALE = 0x20000 / (6 / 5)
_Y_SCALE = -0x10000 / (6 / 5)


def raw_for_px(px, py, center_fraction_x=1 / 4) -> tuple[int, int]:
    """Raw lpad_x/lpad_y values whose pointer lands on pixel (px, py)."""
    return (
        int(round(((px / screen.width) - center_fraction_x) * _X_SCALE)),
        int(round(((py / screen.height) - 1 / 2) * _Y_SCALE)),
    )


# --- OS-boundary recorders -------------------------------------------


class RecordingKeyboard:
    """pynput.Keyboard stand-in shared by every injection surface.

    Events land on ONE class-level stream so output typed through vkb.kb,
    ControllerManager._kb, and EventMapper's keyboard is interleaved in
    true order."""

    EVENTS: list = []

    def __init__(self, *args, **kwargs):
        self.down_keys: set = set()

    def _rec(self, kind, *detail):
        RecordingKeyboard.EVENTS.append((kind, *detail))

    def pressEvent(self, keys):
        keys = tuple(keys)
        self._rec("down", *keys)
        self.down_keys.update(keys)
        for k in keys:
            if k not in ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"):
                self._type_char(k)

    def releaseEvent(self, keys):
        keys = tuple(keys)
        self._rec("up", *keys)
        self.down_keys.difference_update(keys)

    def reset_shift_state(self):
        self._rec("reset-shift")

    def tap_with_modifier(self, keycode, modifiers=()):
        self._rec("tap-mod", keycode, tuple(modifiers))

    def tap_char(self, char):
        self._rec("char", char)
        return True

    def force_caps_off(self):
        self._rec("caps-off")

    def _release_stuck_ctrl(self):
        self._rec("release-ctrl")

    def reset_modifier_state(self):
        self._rec("reset-modifiers")

    def _type_char(self, keycode):
        ch = _KEY_CHARS.get(keycode)
        if ch is None:
            return
        if self.down_keys & {"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"}:
            ch = ch.upper()
        self._rec("char", ch)


class RecordingMouse:
    """pynput.Mouse stand-in (right-stick cursor + trigger mouse holds)."""

    EVENTS: list = []

    def move(self, dx, dy):
        RecordingMouse.EVENTS.append(("move", dx, dy))

    def press(self, button):
        RecordingMouse.EVENTS.append(("press", button))

    def release(self, button):
        RecordingMouse.EVENTS.append(("release", button))


_KEY_CHARS = {
    getattr(sui.Keys, f"KEY_{c}"): c.lower()
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
}
_KEY_CHARS[sui.Keys.KEY_BACKSPACE] = "\b"
_KEY_CHARS.update(
    {
        getattr(sui.Keys, name): ch
        for name, ch in [
            ("KEY_SPACE", " "),
            ("KEY_ENTER", "\n"),
            ("KEY_TAB", "\t"),
            ("KEY_GRAVE", "`"),
            ("KEY_MINUS", "-"),
            ("KEY_EQUAL", "="),
            ("KEY_LEFTBRACE", "["),
            ("KEY_RIGHTBRACE", "]"),
            ("KEY_BACKSLASH", "\\"),
            ("KEY_SEMICOLON", ";"),
            ("KEY_APOSTROPHE", "'"),
            ("KEY_COMMA", ","),
            ("KEY_DOT", "."),
            ("KEY_SLASH", "/"),
            ("KEY_1", "1"),
            ("KEY_2", "2"),
            ("KEY_3", "3"),
            ("KEY_4", "4"),
            ("KEY_5", "5"),
            ("KEY_6", "6"),
            ("KEY_7", "7"),
            ("KEY_8", "8"),
            ("KEY_9", "9"),
            ("KEY_0", "0"),
        ]
    }
)


class VirtualClock:
    """Deterministic time.monotonic replacement (patched by the fixture).
    Single-threaded harness: every module reads the same virtual now."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt=FRAME):
        self.t += dt


class FakeSource:
    """Minimal `sc` facade (the InputMerger contract): poll() returns None
    so nothing else feeds frames; haptics/lizard/exit are recorded."""

    def __init__(self):
        self.exits = 0
        self.lizard_calls: list = []

    def poll(self):
        return None

    def set_lizard(self, on):
        self.lizard_calls.append(bool(on))

    def haptic_click(self):
        pass

    def haptic_pad_click(self):
        pass

    def addExit(self):
        self.exits += 1

    def close(self):
        pass


def make_frame(
    buttons=0,
    ltrig=0.0,
    rtrig=0.0,
    lpad=(0, 0),
    rpad=(0, 0),
    lstick=(0, 0),
    rstick=(0, 0),
    lpad_press=0.0,
    rpad_press=0.0,
):
    """Build a real SteamControllerInput INPUT frame."""
    return SteamControllerInput(
        status=SCStatus.INPUT,
        seq=1,
        buttons=int(buttons),
        ltrig=ltrig,
        rtrig=rtrig,
        lpad_x=lpad[0],
        lpad_y=lpad[1],
        rpad_x=rpad[0],
        rpad_y=rpad[1],
        lstick_x=lstick[0],
        lstick_y=lstick[1],
        rstick_x=rstick[0],
        rstick_y=rstick[1],
        lpad_press=lpad_press,
        rpad_press=rpad_press,
    )


class SceneRunner:
    """One headless OSK session driven frame-by-frame."""

    def __init__(self, clock=None):
        RecordingKeyboard.EVENTS = []
        RecordingMouse.EVENTS = []
        state.reset_session()
        state.set_steam_running(False)
        state.set_rumble_enabled(True)
        state.set_key_sound_enabled(True)
        # The click queue is a CLASS attribute (shared across instances).
        controller.ControllerState.click_queue.clear()
        # Deterministic geometry + locale.
        screen.width, screen.height = 1286, 369
        self.virtual_kb = self._build_kb()
        self.virtual_kb.update_dimensions()
        state.set_virtual_kb(self.virtual_kb)
        state.set_diacritic_variants(diacritics.DIACRITIC_VARIANTS)
        state.set_active_locale("en")
        state.set_diacritics_enabled(True)
        state.set_sc_click_button(None)  # default LB/RB bumpers
        state.set_sc_pad_click_enter(False)
        # Pin every settings-backed knob the manager reads at __init__:
        # other modules' roundtrip tests may have flipped these globals.
        state.set_sc_kbd_stick_nav(True)
        state.set_sc_osk_trigger_threshold(None)
        # Feedback counters wired into the same hooks production uses.
        self.sounds = 0
        self.haptics = 0
        self.pad_haptics = 0
        state.set_key_sound(lambda: setattr(self, "sounds", self.sounds + 1))
        state.set_haptic_tick(
            lambda: setattr(self, "haptics", self.haptics + 1)
        )
        state.set_pad_click_haptic(
            lambda: setattr(self, "pad_haptics", self.pad_haptics + 1)
        )
        self.clock = clock if clock is not None else VirtualClock()
        # push() advances one FRAME by default; cadence tests flip this
        # off to drive the clock in explicit steps.
        self.auto_advance = True
        self.src = FakeSource()
        self.cstate = controller.ControllerState()
        # Production seeds the pointer pair before the manager is built
        # (get_pointers() must never be None on the first frame).
        center = CoordFraction.from_absolute(
            screen.width // 2, screen.height // 2
        )
        idle = vptr.VirtualPointer(state.InputState.INACTIVE, center)
        self.cstate.set_pointers(
            idle,
            vptr.VirtualPointer(state.InputState.INACTIVE, center),
        )
        self.manager = controller.ControllerManager(self.cstate)
        # First real frame is swallowed as the edge-detection baseline.
        self.push()

    @staticmethod
    def _build_kb():
        cfg = config.YamlFile("keyboard-layout.yaml")
        cfg.read()
        cfg.add_to_config("keys", vkb.VirtualKeyboardConfig())
        kb = vkb.VirtualKeyboardConfig().construct()
        kb.update_dimensions()
        return kb

    # -- clock -----------------------------------------------------

    def advance(self, dt=FRAME):
        self.clock.advance(dt)

    # -- input -----------------------------------------------------

    def push(self, **kw):
        """Advance one frame, feed it through update(), drain the queues."""
        if self.auto_advance:
            self.advance()
        controller.update(self.src, make_frame(**kw), self.manager)
        drain_input_work(self.cstate, self.virtual_kb)

    def push_idle(self, n=1):
        for _ in range(n):
            self.push()

    def tap_button(self, btn, **kw):
        """Press+release a button around one idle frame."""
        kw["buttons"] = kw.get("buttons", 0) | btn
        self.push(**kw)
        self.push(
            buttons=kw["buttons"]
            & ~(btn | _dpad_bit(btn)),  # drop dpad bits on release
        )

    def hold_button(self, btn, seconds, **kw):
        """Hold a button for ~seconds across whole frames, then release."""
        kw["buttons"] = kw.get("buttons", 0) | btn
        while seconds > 0:
            self.push(**kw)
            seconds -= FRAME
        self.push(
            buttons=kw["buttons"] & ~(btn | _dpad_bit(btn)),
        )

    def navigate(self, *directions):
        """DPAD taps (edge per direction, released before the next)."""
        bits = {
            "UP": SCButtons.DPAD_UP,
            "DOWN": SCButtons.DPAD_DOWN,
            "LEFT": SCButtons.DPAD_LEFT,
            "RIGHT": SCButtons.DPAD_RIGHT,
        }
        for d in directions:
            self.tap_button(bits[d])

    def stick(self, dx, dy, steam=False):
        return dict(
            lstick=(dx, dy),
            buttons=SCButtons.STEAM if steam else 0,
        )

    # -- pointer ---------------------------------------------------

    def cell_rc(self, label):
        """(row, col) of the key whose label matches, or None."""
        for r, row in enumerate(self.virtual_kb.keys):
            for c, key in enumerate(row):
                if key.str == label:
                    return (r, c)
        return None

    def cell_center(self, label=None, *, rc=None):
        r, c = rc if rc is not None else self.cell_rc(label)
        layout = self.virtual_kb.get_key_layout(r, c)
        assert layout is not None
        return (
            layout.x + layout.w // 2,
            layout.y + layout.h // 2,
        )

    def raw_at_cell(self, label=None, *, rc=None, side="left"):
        px, py = self.cell_center(label, rc=rc)
        cfx = 1 / 4 if side == "left" else 3 / 4
        rx, ry = raw_for_px(px, py, cfx)
        return {"lpad": (rx, ry)} if side == "left" else {"rpad": (rx, ry)}

    def _raw_at_px(self, px, py) -> dict:
        """Left-pad raw values landing the pointer on an absolute pixel."""
        return {"lpad": raw_for_px(px, py)}

    def point(self, label=None, *, rc=None):
        """Park the left-pad pointer on a cell (touch, no click)."""
        kw: dict = self.raw_at_cell(label, rc=rc)
        kw["buttons"] = SCButtons.LPADTOUCH
        self.push(**kw)

    def click_cell(self, label=None, *, rc=None, side="left"):
        """Click-button insert path: LB/RB held with the pointer parked."""
        btn = SCButtons.LB if side == "left" else SCButtons.RB
        pos = self.raw_at_cell(label, rc=rc, side=side)
        self.push(buttons=btn | SCButtons.LPADTOUCH, **pos)
        self.push(buttons=0, **pos)

    # -- queries ---------------------------------------------------

    @staticmethod
    def events():
        return list(RecordingKeyboard.EVENTS)

    @staticmethod
    def typed():
        """Net typed text from the recorder stream (backspaces applied)."""
        out = []
        for ev in RecordingKeyboard.EVENTS:
            if ev[0] != "char":
                continue
            ch = ev[1]
            if ch == "KEY_BACKSPACE" or ch == "\b":
                if out:
                    out.pop()
            elif isinstance(ch, str) and len(ch) == 1:
                out.append(ch)
        return "".join(out)

    def cursor(self):
        return state.get_cursor()


_DPAD = {
    SCButtons.DPAD_UP,
    SCButtons.DPAD_DOWN,
    SCButtons.DPAD_LEFT,
    SCButtons.DPAD_RIGHT,
}


def _dpad_bit(btn):
    return btn if btn in _DPAD else 0
