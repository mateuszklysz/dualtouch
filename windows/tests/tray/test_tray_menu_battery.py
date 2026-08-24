"""Layer 1/2: tray battery notification matrix + menu wiring smoke."""

from threading import Event
from typing import Any
from unittest.mock import MagicMock

import pytest
from tray.battery import _BatteryMixin
from triton import state as triton_state


class _Batt:
    def __init__(self, percent, charging, charge_complete=False):
        self.percent = percent
        self.charging = charging
        self.charge_complete = charge_complete


class _Host(_BatteryMixin):
    _battery: Any
    _current_sc: Any

    def __init__(self):
        self._stop_event = Event()
        self._battery = None
        self._battery_label = None
        self._icon_ref = None
        self._current_sc = None
        self._notify_calls = []
        self._refreshed = 0
        self._was_charging = False
        self._low_warned_at = None
        self._charge_complete_notified = False

    def _notify(self, msg, title=None):
        self._notify_calls.append(msg)

    def _refresh_menu(self):
        self._refreshed += 1


@pytest.fixture(autouse=True)
def _rumble_on():
    triton_state.set_rumble_enabled(True)
    yield
    triton_state.set_rumble_enabled(False)


def _no_puck(monkeypatch):
    import steamcontroller

    monkeypatch.setattr(
        steamcontroller, "present_product_ids", lambda: set(), False
    )


def test_update_battery_ui_labels_and_refresh():
    h = _Host()
    for batt, want in [
        (_Batt(100, False, True), "100% (charged)"),
        (_Batt(55, True), "55% (charging)"),
        (_Batt(23, False), "23%"),
    ]:
        h._update_battery_ui(batt)
        assert h._battery_label == f"Steam Controller: {want}"
    assert h._refreshed >= 3
    h._battery = batt  # the polling thread sets this on real frames
    assert h.is_battery_known(None) is True  # pystray item arg ignored


def test_low_battery_bands_escalate_once(monkeypatch):
    _no_puck(monkeypatch)
    h = _Host()
    h._battery_notifications(_Batt(40, False))  # arm baseline (>35)
    assert h._notify_calls == []
    h._battery_notifications(_Batt(25, False))  # entering 30 band warns
    n_first = len(h._notify_calls)
    assert n_first >= 1
    # Same band again: no duplicate.
    h._battery_notifications(_Batt(22, False))
    assert len(h._notify_calls) == n_first
    # Deeper bands escalate (10, then critical <=5).
    h._battery_notifications(_Batt(8, False))
    assert len(h._notify_calls) > n_first
    n_crit = len(h._notify_calls)
    h._battery_notifications(_Batt(4, False))
    assert len(h._notify_calls) > n_crit


def test_recovery_resets_latch(monkeypatch):
    _no_puck(monkeypatch)
    h = _Host()
    h._battery_notifications(_Batt(20, False))
    n1 = len(h._notify_calls)
    assert n1 >= 1
    h._battery_notifications(_Batt(50, False))  # above recover threshold
    h._battery_notifications(_Batt(20, False))  # same band warns again
    assert len(h._notify_calls) > n1


def test_charging_never_warns_and_puck_suppresses_charged_toast(
    monkeypatch,
):
    """Controller on the USB dongle's wired pass-through ('the puck'):
    charging there is docking, not charging a battery — stay silent."""
    import steamcontroller as sc

    wired = sc.PRODUCT_ID_WIRED
    monkeypatch.setattr(sc, "present_product_ids", lambda: {wired}, False)
    h = _Host()
    h._battery_notifications(_Batt(10, True))
    h._battery_notifications(_Batt(12, True))
    assert h._notify_calls == []


def test_unplug_toasts_once(monkeypatch):
    _no_puck(monkeypatch)
    h = _Host()
    h._update_battery_ui(_Batt(60, True))  # seed _was_charging state
    h._battery_notifications(_Batt(60, True))
    n = len(h._notify_calls)
    h._battery_notifications(_Batt(58, False))  # unplug edge
    assert len(h._notify_calls) == n + 1
    h._battery_notifications(_Batt(57, False))
    assert len(h._notify_calls) == n + 1  # not repeated


def test_charge_complete_toasts_once(monkeypatch):
    _no_puck(monkeypatch)
    h = _Host()
    h._battery_notifications(_Batt(99, True))
    h._battery_notifications(_Batt(100, False))
    n = len(h._notify_calls)
    assert n >= 1
    h._battery_notifications(_Batt(100, False))
    assert len(h._notify_calls) == n


def test_haptic_on_low_warning_when_controller_present():
    h = _Host()

    class _SC:
        def __init__(self):
            self.clicks = 0

        def haptic_click(self):
            self.clicks += 1

    sc = _SC()
    h._current_sc = sc
    h._battery_notifications(_Batt(15, False))
    assert sc.clicks >= 1


def test_menu_builds_with_stub_app():
    from tray.menu import build_menu

    app = MagicMock()
    app.diacritic_locale_options.return_value = ["en", "de"]
    menu = build_menu(app)
    assert menu is not None
