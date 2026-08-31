"""Tests for Windows shutdown/session-end tray handling."""

from tray.icon import SessionAwareIcon


def test_query_end_session_is_approved():
    assert SessionAwareIcon._on_query_end_session(None, 0, 0) == 1


def test_end_session_invokes_cleanup_callback_only_when_committed():
    calls = []
    icon = type("IconStub", (), {})()
    icon._on_session_end_callback = lambda received: calls.append(received)

    assert SessionAwareIcon._on_end_session(icon, 0, 0) == 0
    assert calls == []
    assert SessionAwareIcon._on_end_session(icon, 1, 0) == 0
    assert calls == [icon]
