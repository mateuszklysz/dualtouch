"""Tests for Windows shutdown/session-end tray handling."""

from collections.abc import Callable
from typing import cast

from tray.icon import SessionAwareIcon


class _IconStub:
    _on_session_end_callback: Callable[[SessionAwareIcon], None] | None = None


def test_query_end_session_is_approved():
    icon = cast(SessionAwareIcon, _IconStub())
    assert SessionAwareIcon._on_query_end_session(icon, 0, 0) == 1


def test_end_session_invokes_cleanup_callback_only_when_committed():
    calls = []
    icon_stub = _IconStub()
    icon_stub._on_session_end_callback = lambda received: calls.append(
        received
    )
    icon = cast(SessionAwareIcon, icon_stub)

    assert SessionAwareIcon._on_end_session(icon, 0, 0) == 0
    assert calls == []
    assert SessionAwareIcon._on_end_session(icon, 1, 0) == 0
    assert calls == [icon]
