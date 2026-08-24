"""Layer 2: pad pointer plumbing — raw->pixel mapping, click-button insert
under the pointer, settle-window debounce, force hysteresis, and the
pad-force click path (sc_pad_click_enter)."""

from steamcontroller import SCButtons
from triton import diacritics, state
from triton.pad import _PadMixin


def test_published_pointer_lands_on_aimed_cell(runner):
    runner.point("j")
    px, py = runner.cell_center("j")
    ptr = runner.cstate.get_pointers()[0]
    x, y = ptr.coord_frac.to_absolute()
    assert (round(x), round(y)) == (px, py)


def test_click_button_inserts_key_under_pointer(runner):
    runner.click_cell("j")
    assert runner.typed() == "j"
    assert runner.pad_haptics >= 1  # click rumble on the press edge


def test_settle_window_swallows_force_wobble(runner):
    """A re-engage inside PAD_CLICK_SETTLE (0.05 s) is a phantom second
    insert and must be swallowed; a fast-but-human retype still fires."""
    letter = next(
        lbl
        for lbl in ("q", "w", "p", "y")
        if diacritics.lookup_variants(
            diacritics.DIACRITIC_VARIANTS, "en", lbl
        )
        is None
    )
    pos = runner.raw_at_cell(letter)
    t = runner.clock.t
    runner.push(buttons=SCButtons.LB, **pos)  # press #1 -> types
    runner.advance(0.01)
    runner.push(buttons=0, **pos)  # release
    runner.clock.t = t + 0.02  # re-engage well inside the settle window
    runner.push(buttons=SCButtons.LB, **pos)  # wobble: swallowed
    runner.push(buttons=0, **pos)
    assert runner.typed() == letter


class _Hyst(_PadMixin):
    def __init__(self):
        self._pad_click_engage = 2500
        self._pad_click_release = 1000

    press_click = _PadMixin._press_click


def test_press_click_hysteresis():
    h = _Hyst()
    assert h._press_click(2600, False)  # above engage: click
    assert not h._press_click(500, True)  # below release: unclick
    assert not h._press_click(1500, False)  # mid band holds previous...
    assert h._press_click(1500, True)  # ...in both directions


def test_pad_force_click_path_inserts(runner):
    """sc_pad_click_enter ON: physical pad force crossing ENGAGE clicks."""
    state.set_sc_pad_click_enter(True)
    letter = next(
        lbl
        for lbl in ("q", "w", "p", "y")
        if diacritics.lookup_variants(
            diacritics.DIACRITIC_VARIANTS, "en", lbl
        )
        is None
    )
    pos = runner.raw_at_cell(letter)
    touch = SCButtons.LPADTOUCH
    runner.push(buttons=touch, lpad_press=2600, **pos)
    assert runner.typed() == letter
    runner.push(buttons=touch, lpad_press=0, **pos)  # release: rumble
    assert runner.pad_haptics >= 2  # press + release ticks


def test_raw_mapping_roundtrips_through_adjust(runner):
    from triton.controller import adjust_raw_x, adjust_raw_y

    for label in ("a", "j", "Backspace", "Esc", "Space"):
        px, py = runner.cell_center(label)
        rx, ry = runner.raw_at_cell(label)["lpad"]
        assert adjust_raw_x(rx, 1 / 4) == px
        assert adjust_raw_y(ry, 1 / 2) == py
