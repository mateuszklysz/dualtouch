"""Tests for launcher startup side effects."""

from tray.launcher import _should_dispatch_startup_restore


def test_startup_restore_does_not_launch_steam_by_default():
    assert _should_dispatch_startup_restore({}, steam_running=False) is False
    assert (
        _should_dispatch_startup_restore(
            {"start_steam_on_startup": False}, steam_running=False
        )
        is False
    )


def test_startup_restore_keeps_running_steam_healthy():
    # The setting controls launching Steam, not whether an already-running
    # client's stale forced appid is restored.
    assert _should_dispatch_startup_restore({}, steam_running=True) is True


def test_startup_restore_can_opt_into_launching_steam():
    assert (
        _should_dispatch_startup_restore(
            {"start_steam_on_startup": True}, steam_running=False
        )
        is True
    )
