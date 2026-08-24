"""Layer 2: the diacritic defer model (Feature B) through pad click and
A-button paths — quick tap types the base silently, a hold opens the
variant row, release commits base vs variant."""

import pytest
from steamcontroller import SCButtons
from triton import diacritics, state

_EN_A = diacritics.lookup_variants(diacritics.DIACRITIC_VARIANTS, "en", "a")


@pytest.fixture
def en_a():
    assert _EN_A and len(_EN_A) >= 3
    return _EN_A


def _hold_open_row(runner, label="a"):
    """Press LB on a cell and hold past KEY_REPEAT_DELAY so the variant row
    opens (source 'pad'). Explicit clock steps; auto-advance is restored
    before returning so later pushes flow normally."""
    runner.auto_advance = False
    pos = runner.raw_at_cell(label)
    runner.advance(0.02)
    runner.push(buttons=SCButtons.LB, **pos)  # press edge: defer + sound
    for _ in range(8):  # 8 * 0.06 s clears the 0.4 s delay mid-band
        runner.advance(0.06)
        runner.push(buttons=SCButtons.LB, **pos)
    assert state.is_diacritic_open()
    assert state.get_diacritic_source() == "pad"
    runner.auto_advance = True
    return pos


def test_quick_tap_types_base_silently(runner, en_a):
    rc = runner.cell_rc("a")
    state.set_cursor(*rc)  # irrelevant; the PAD path types here
    runner.click_cell("a")
    assert runner.typed() == "a"
    assert not state.is_diacritic_open()
    # One click at the press edge; the release commit is silent.
    assert runner.sounds == 1


def test_hold_commits_variant_under_finger_x(runner, en_a):
    pos = _hold_open_row(runner)
    assert list(state.get_diacritic_variants_list()) == list(en_a)
    # The resting finger already highlights whatever slot its x falls in.
    rect = state.get_diacritic_rect()
    n = state.get_diacritic_variant_count()
    px, py = runner.cell_center("a")
    picked = diacritics.variant_index_at_point(rect, px, py, n)
    runner.push(buttons=0, **pos)  # release commits the highlighted one
    assert runner.typed() == en_a[picked]
    assert not state.is_diacritic_open()
    assert runner.sounds == 1  # press edge only


def test_hold_with_finger_off_strip_defaults_to_first(runner, en_a):
    """A release whose finger never sat in the strip commits the FIRST
    variant, never the base letter."""
    _hold_open_row(runner)
    state.set_diacritic_index(-1)  # simulate no explicit pick
    off = runner._raw_at_px(2, 360)  # bottom-left corner: outside slots
    runner.push(buttons=SCButtons.LB, **off)
    runner.push(buttons=0, **off)
    assert runner.typed() == en_a[0]


def test_finger_x_picks_variant_on_release(runner, en_a):
    _hold_open_row(runner)
    rect = state.get_diacritic_rect()
    assert rect is not None  # the row was just opened
    n = state.get_diacritic_variant_count()
    slot = 2
    px = rect[0] + rect[2] * (slot + 0.5) / n
    py = rect[1] + rect[3] / 2
    over = runner.raw_at_cell(rc=runner.cell_rc("a"))
    # Slide the finger into the variant strip while still holding LB.
    runner.push(buttons=SCButtons.LB, lpad=over["lpad"])
    runner.push(buttons=SCButtons.LB, **runner._raw_at_px(px, py))
    assert state.get_diacritic_selected_char() == en_a[slot]
    runner.push(buttons=0, **runner._raw_at_px(px, py))
    assert runner.typed() == en_a[slot]


def test_dpad_moves_selection_while_row_open(runner, en_a):
    """A-button variant: while the row is open via A, DPAD left/right steps
    the selection and A-release commits it (no finger to fight)."""
    runner.auto_advance = False
    state.set_cursor(*runner.cell_rc("a"))
    runner.advance(0.02)
    runner.push(buttons=SCButtons.A)
    for _ in range(8):
        runner.advance(0.06)
        runner.push(buttons=SCButtons.A)
    assert state.get_diacritic_source() == "a"
    # From "no pick" (-1), the first RIGHT selects variant 0. Each further
    # step needs a fresh DPAD edge, so drop the bit between taps.
    runner.push(buttons=SCButtons.A | SCButtons.DPAD_RIGHT)
    assert state.get_diacritic_index() == 0
    runner.push(buttons=SCButtons.A)
    runner.push(buttons=SCButtons.A | SCButtons.DPAD_RIGHT)
    assert state.get_diacritic_index() == 1
    runner.push(buttons=SCButtons.A)  # keep holding; selection stays
    assert state.get_diacritic_index() == 1
    runner.auto_advance = True
    runner.push()  # release commits the picked variant
    assert runner.typed() == en_a[1]


def test_a_button_hold_opens_row_and_commits_first(runner, en_a):
    runner.auto_advance = False
    state.set_cursor(*runner.cell_rc("a"))
    runner.advance(0.02)
    runner.push(buttons=SCButtons.A)  # press edge: defer + click sound
    for _ in range(8):
        runner.advance(0.06)
        runner.push(buttons=SCButtons.A)
    assert state.is_diacritic_open()
    assert state.get_diacritic_source() == "a"
    runner.auto_advance = True
    runner.push()
    assert runner.typed() == en_a[0]
    assert runner.sounds == 1


def test_a_button_quick_tap_types_base_silently(runner):
    state.set_cursor(*runner.cell_rc("a"))
    runner.tap_button(SCButtons.A)
    assert runner.typed() == "a"
    assert runner.sounds == 1


def test_disabled_diacritics_type_immediately_at_press_edge(runner):
    state.set_diacritics_enabled(False)
    pos = runner.raw_at_cell("a")
    runner.push(buttons=SCButtons.LB, **pos)
    assert runner.typed() == "a"  # already typed BEFORE release
    runner.push(buttons=0, **pos)
    assert runner.typed() == "a"
