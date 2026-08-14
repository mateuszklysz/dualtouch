"""Headless tests for the YAML config loading path (triton/config.py +
triton/vkb.py). Guards against a corrupted or renamed layout file breaking
the OSK at open time — the layout is loaded on every open."""

import pytest
from triton import config, vkb


def test_keyboard_layout_loads_and_constructs():
    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("keys", kb_config)
    kb = kb_config.construct()
    assert len(kb.keys) > 0
    any_labeled = False
    for row in kb.keys:
        assert len(row) > 0
        for key in row:
            # KeyButton.str is the label shown on the key (renderer reads
            # .str / .shifted / .keycode); glyph-only keys may have "".
            assert key.str is not None
            assert key.keycode is not None
            if key.str:
                any_labeled = True
    assert any_labeled  # the layout is not just empty glyph shells


def test_move_key_routes_shift_to_move_and_ctrl_key_wired():
    """Pure-logic layout contract after the Move/Emoji swap: the bottom row's
    Move key (glyph = emoji smiley) opens the emoji picker on a plain press
    (Win+. - NOT exercised here, it sends real input) and requests the window
    position cycle while Shift is held; the former emoji slot is now a Ctrl
    key (behavior ctrl)."""
    from triton import state

    state.reset_session()
    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("keys", kb_config)
    kb = kb_config.construct()

    bottom = kb.keys[-1]
    move = next(k for k in bottom if k.callback.__name__ == "on_key_move")
    ctrl = next(k for k in bottom if k.callback.__name__ == "on_key_ctrl")

    assert move.glyph == "glyph_smiley.png"
    assert ctrl.keycode == "KEY_LEFTCTRL"
    # Shift+Move -> position cycle (the "Move" action is the shifted form).
    state.set_shift_held(True)
    assert state.take_position_cycle_request() is False
    move.callback(None, 0)
    assert state.take_position_cycle_request() is True
    state.set_shift_held(False)


def test_unknown_config_key_is_rejected():
    # add_to_config must fail loudly on a malformed layout instead of
    # silently constructing an empty keyboard.
    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    with pytest.raises(AssertionError):
        layout.add_to_config("no_such_key", kb_config)


def test_key_geometry_tracks_screen_dims_after_update_dimensions():
    """The resize-gate invariant (triton main loop only recomputes key
    geometry when _resize_dirty is set): update_dimensions must rebuild the
    key rects AND invalidate the gen_key_layouts cache so the first iteration
    after a Screen-construction size change resolves against the new dims.
    Guards the fresh-Screen path where no SDL resize event ever fires (the
    OSK window is non-resizable)."""
    import triton.screen as screen

    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("keys", kb_config)
    kb = kb_config.construct()

    screen.width, screen.height = 1286, 369
    kb.update_dimensions()
    before = [(lay.x, lay.y, lay.w, lay.h) for lay in kb.gen_key_layouts()]

    # Changing the module dims is exactly what a fresh Screen() construction
    # does without firing an SDL resize event.
    screen.width, screen.height = 900, 258  # "small"
    before_after_dims_change = [
        (lay.x, lay.y, lay.w, lay.h) for lay in kb.gen_key_layouts()
    ]
    # The cached layout is stale until update_dimensions runs.
    assert before_after_dims_change == before

    kb.update_dimensions()
    after = [(lay.x, lay.y, lay.w, lay.h) for lay in kb.gen_key_layouts()]
    assert after != before, "geometry must change with the window size"
    # Rebuilding again must be stable (cache is valid post-invalidate).
    assert [
        (lay.x, lay.y, lay.w, lay.h) for lay in kb.gen_key_layouts()
    ] == after


def test_75pct_layout_has_function_row_and_nav_cluster():
    """The 75% expansion contract: a function row (Esc/F1-F12 + the Home/End/
    Ins nav keys) sits on top; Backspace types Backspace always (no Delete
    shortcut). PgUp/PgDn are dropped entirely. Guards the row-shift that an
    F-row introduces (letters move down one row)."""
    kb_config = vkb.VirtualKeyboardConfig()
    layout = config.YamlFile("keyboard-layout.yaml")
    layout.read()
    layout.add_to_config("keys", kb_config)
    kb = kb_config.construct()

    fn_row = kb.keys[0]
    fn_labels = {k.str for k in fn_row}
    assert "Esc" in fn_labels and "F1" in fn_labels and "F12" in fn_labels
    assert "Home" in fn_labels and "End" in fn_labels and "Ins" in fn_labels
    assert "PrtSc" not in fn_labels and "ScrLk" not in fn_labels
    assert "Pause" not in fn_labels

    # The letter rows shifted down one: 'a' is now row 3, col 1.
    assert kb.keys[3][1].str == "a"

    # Backspace carries its label text plus the X shortcut icon (the inline
    # label+glyph keys); it types Backspace always (no Delete shift-mode).
    bs = next(
        k
        for row in kb.keys
        for k in row
        if k.keycode == vkb.sui.Keys.KEY_BACKSPACE
    )
    assert bs.str == "Backspace"
    assert bs.glyph == "glyph_x.png"
    assert bs.callback.__name__ == "on_key_backspace"
    # The label+icon keys render inline (Backspace X, Caps L3, Space Y).
    caps = next(
        k
        for row in kb.keys
        for k in row
        if k.keycode == vkb.sui.Keys.KEY_CAPSLOCK
    )
    space = next(
        k
        for row in kb.keys
        for k in row
        if k.keycode == vkb.sui.Keys.KEY_SPACE
    )
    assert caps.str == "Caps" and caps.glyph == "glyph_l3.png"
    assert space.str == "Space" and space.glyph == "glyph_y.png"

    bottom = kb.keys[-1]
    bottom_by_code = {k.keycode: k for k in bottom}
    for code in (
        "KEY_LEFT",
        "KEY_RIGHT",
        "KEY_UP",
        "KEY_DOWN",
        "KEY_LEFTALT",
        "KEY_LEFTWIN",
    ):
        assert code in bottom_by_code, f"bottom row missing {code}"
    # The nav cluster is NOT on the bottom row anymore.
    for code in (
        "KEY_HOME",
        "KEY_END",
        "KEY_INSERT",
        "KEY_PAGEUP",
        "KEY_PAGEDOWN",
        "KEY_DELETE",
    ):
        assert code not in bottom_by_code, (
            f"{code} should not be on bottom row"
        )
    # And the function row carries the home/end/ins nav keys.
    fn_by_code = {k.keycode: k for k in fn_row}
    for code in ("KEY_HOME", "KEY_END", "KEY_INSERT"):
        assert code in fn_by_code, f"function row missing {code}"
    # Home/End repeat when held (like a real board); Ins does not need to.
    assert vkb.is_repeatable(fn_by_code["KEY_HOME"])
    assert vkb.is_repeatable(fn_by_code["KEY_END"])
