"""Headless tests for triton/state.py — the thread-safe session state the
controller thread and the SDL render thread communicate through.

These are pure-logic tests: no SDL window, no HID device, no Steam.
"""

from triton import state


def test_close_and_reset_session():
    state.reset_session()
    assert state.should_close() is False
    state.close()
    assert state.should_close() is True
    state.reset_session()
    assert state.should_close() is False


def test_focus_restore_target_roundtrip():
    state.reset_session()
    assert state.get_focus_restore_target() is None
    state.set_focus_restore_target(0x12345678)
    assert state.get_focus_restore_target() == 0x12345678
    state.set_focus_restore_target(None)
    assert state.get_focus_restore_target() is None


def test_key_press_queue_order_and_repeat_flag():
    state.reset_session()
    state.queue_key_press(1, 2)
    state.queue_key_press(3, 4, repeat=True)
    state.queue_key_press(5, 6, silent=True)
    assert state.drain_key_press_queue() == [
        (1, 2, False, False),
        (3, 4, True, False),
        (5, 6, False, True),
    ]
    # Draining empties the queue.
    assert state.drain_key_press_queue() == []


def test_key_press_queue_coerces_to_ints():
    state.reset_session()
    # Deliberately passing strings: the point is that int()/bool() coercion
    # normalizes them.
    state.queue_key_press("2", "5", "yes")  # type: ignore[reportArgumentType]
    assert state.drain_key_press_queue() == [(2, 5, True, False)]


def test_dpad_queue_order_and_haptic_flag():
    state.reset_session()
    state.queue_dpad("up")
    state.queue_dpad("left", haptic=True)
    assert state.drain_dpad_queue() == [("up", False), ("left", True)]


def test_shift_hold_and_latch_are_independent():
    # A connected controller rewrites _shift_held every input frame; the
    # mouse-click latch must survive that (or it would never turn back off).
    state.reset_session()
    assert state.is_shift_held() is False
    assert state.is_shift_latched() is False
    state.set_shift_held(True)
    state.set_shift_latched(True)
    assert state.is_shift_held() is True
    assert state.is_shift_latched() is True
    # Controller clears the held state...
    state.set_shift_held(False)
    assert state.is_shift_held() is False
    # ...but the latch stays until the toggle path clears it.
    assert state.is_shift_latched() is True


def test_position_and_window_requests_are_one_shot():
    state.reset_session()
    assert state.take_position_cycle_request() is False
    state.request_position_cycle()
    assert state.take_position_cycle_request() is True
    assert state.take_position_cycle_request() is False

    assert state.take_window_position_request() is None


def test_controller_activity_timestamp():
    state.reset_session()
    assert state.get_last_controller_activity() == 0.0
    state.set_last_controller_activity(1234.5)
    assert state.get_last_controller_activity() == 1234.5


def test_osk_mouse_inject_timestamp():
    state.reset_session()
    assert state.get_osk_mouse_inject_t() == 0.0
    state.set_osk_mouse_inject(77.0)
    assert state.get_osk_mouse_inject_t() == 77.0


def test_emoji_open_roundtrip():
    state.reset_session()
    assert state.is_emoji_open() is False
    state.set_emoji_open(True)
    assert state.is_emoji_open() is True


def test_sc_settings_roundtrips():
    state.reset_session()
    state.set_sc_pad_press(2500, 1000, 2000, 0.35)
    assert state.get_sc_pad_press() == (2500, 1000, 2000, 0.35)

    state.set_sc_pad_click_enter(True)
    assert state.get_sc_pad_click_enter() is True

    state.set_sc_click_button(0b11)
    assert state.get_sc_click_button() == 0b11

    state.set_sc_trigger_focus_pull(16384)
    assert state.get_sc_trigger_focus_pull() == 16384

    state.set_sc_kbd_stick_nav(False)
    assert state.is_sc_kbd_stick_nav_enabled() is False

    state.set_sc_osk_trigger_threshold(0.5)
    assert state.get_sc_osk_trigger_threshold() == 0.5


def test_ctrl_latch_roundtrip_and_reset():
    state.reset_session()
    assert state.is_ctrl_latched() is False
    state.set_ctrl_latched(True)
    assert state.is_ctrl_latched() is True
    state.reset_session()
    assert state.is_ctrl_latched() is False


def test_alt_latch_roundtrip_and_reset():
    state.reset_session()
    assert state.is_alt_latched() is False
    state.set_alt_latched(True)
    assert state.is_alt_latched() is True
    assert "KEY_LEFTALT" in state.get_latched_modifier_keys()
    state.reset_session()
    assert state.is_alt_latched() is False
    assert state.get_latched_modifier_keys() == set()


def test_cursor_used_only_after_navigation():
    """The DPAD/stick cursor highlight must not appear from its default until
    the user actually navigates with the stick/DPAD (touchpad-only users must
    not see an arbitrary key lit up when both fingers lift)."""
    state.reset_session()
    assert state.is_cursor_used() is False
    # Mouse/touchpad pointing sets the cursor but must NOT arm the persistent
    # highlight.
    state.set_cursor(2, 5)
    assert state.is_cursor_used() is False
    # Stick/DPAD navigation arms it.
    state.mark_cursor_used()
    assert state.is_cursor_used() is True
    # A new session resets it.
    state.reset_session()
    assert state.is_cursor_used() is False


def test_window_position_per_app_roundtrip_and_reset_survival():
    state.reset_session()
    assert state.get_window_position_per_app() == {}
    state.set_window_position_per_app({"notepad.exe": 3, "wordpad.exe": 5})
    assert state.get_window_position_per_app() == {
        "notepad.exe": 3,
        "wordpad.exe": 5,
    }
    # Session-independent config: reset_session() must NOT wipe it.
    state.reset_session()
    assert state.get_window_position_per_app() == {
        "notepad.exe": 3,
        "wordpad.exe": 5,
    }
    # The setter snapshots the caller's dict - later mutation must not leak in.
    m = {"chrome.exe": 1}
    state.set_window_position_per_app(m)
    m["chrome.exe"] = 2
    assert state.get_window_position_per_app() == {"chrome.exe": 1}
    state.set_window_position_per_app(None)
    assert state.get_window_position_per_app() == {}


def test_per_app_size_skin_maps_roundtrip_and_reset_survival():
    state.reset_session()
    assert state.get_osk_size_per_app() == {}
    assert state.get_skin_per_app() == {}
    state.set_osk_size_per_app({"notepad.exe": "small"})
    state.set_skin_per_app({"notepad.exe": "Digital"})
    assert state.get_osk_size_per_app() == {"notepad.exe": "small"}
    assert state.get_skin_per_app() == {"notepad.exe": "Digital"}
    # Session-independent config: reset_session() must NOT wipe it.
    state.reset_session()
    assert state.get_osk_size_per_app() == {"notepad.exe": "small"}
    assert state.get_skin_per_app() == {"notepad.exe": "Digital"}
    # The setters snapshot the caller's dict - later mutation must not leak in.
    m = {"chrome.exe": "full"}
    state.set_osk_size_per_app(m)
    m["chrome.exe"] = "small"
    assert state.get_osk_size_per_app() == {"chrome.exe": "full"}
    state.set_osk_size_per_app(None)
    assert state.get_osk_size_per_app() == {}


def test_diacritic_session_open_select_commit_close():
    state.reset_session()
    assert state.is_diacritic_open() is False
    assert state.get_diacritic_selected_char() is None
    state.open_diacritic("a", ["á", "à", "â"], (10, 20, 92, 40), "pad")
    assert state.is_diacritic_open() is True
    assert state.get_diacritic_index() == -1
    assert state.get_diacritic_selected_char() is None  # base selected
    assert state.get_diacritic_variant_count() == 3
    assert state.get_diacritic_rect() == (10, 20, 92, 40)
    assert state.get_diacritic_source() == "pad"
    state.set_diacritic_index(1)
    assert state.get_diacritic_selected_char() == "à"
    # set index out of range is stored raw; lookups clamp in the caller.
    state.set_diacritic_index(0)
    assert state.get_diacritic_selected_char() == "á"
    state.close_diacritic()
    assert state.is_diacritic_open() is False
    assert state.get_diacritic_selected_char() is None


def test_diacritic_session_is_reset_by_reset_session():
    state.reset_session()
    state.open_diacritic("e", ["é", "è"], (10, 10, 62, 40), "a")
    state.set_diacritic_index(1)
    state.reset_session()
    assert state.is_diacritic_open() is False


def test_diacritic_config_survives_reset_and_snapshots():
    state.reset_session()
    state.set_diacritic_variants({"de": {"a": ["ä"]}})
    state.set_active_locale("de")
    state.set_diacritics_enabled(False)
    # Config is session-independent: reset_session must NOT wipe it (the tray
    # re-publishes at startup, but it must also survive in-session resets).
    state.reset_session()
    assert state.get_diacritic_variants() == {"de": {"a": ["ä"]}}
    assert state.get_active_locale() == "de"
    assert state.is_diacritics_enabled() is False
    # The setter snapshots the caller's dict - later mutation must not leak in.
    m = {"fr": {"e": ["é", "è"]}}
    state.set_diacritic_variants(m)
    m["fr"]["e"].append("ê")
    assert state.get_diacritic_variants() == {"fr": {"e": ["é", "è"]}}
    state.set_diacritics_enabled(True)
    assert state.is_diacritics_enabled() is True
