"""Layer 1/2: tray single-instance mutex + helper shims."""

import sys

import autostart
import pytest
from tray import helpers


def test_tray_mutex_name_shape():
    name = helpers._tray_mutex_name()
    assert "DualTouch_Tray_SingleInstance" in name
    assert name.startswith("Local\\") or "\\" in name


def test_user_only_dacl_builds_or_falls_back():
    # Real advapi32 path on CI/dev boxes; either outcome is valid, the
    # function must simply not blow up and return bytes-or-None.
    dacl = helpers._user_only_dacl()
    assert dacl is None or len(dacl) > 0  # ACL buffer or graceful fallback


def test_mutex_create_conflict_and_probe():
    handle = helpers._create_tray_mutex()
    if handle is None:
        pytest.skip("mutex unavailable in this environment")
    try:
        # A second create must detect the existing instance.
        assert helpers._create_tray_mutex() is None
        assert helpers._tray_mutex_held() is True
    finally:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(handle)
    assert helpers._tray_mutex_held() is False


def test_steam_running_false_without_psutil(monkeypatch):
    real_import = __import__

    def fake_import(name, *a, **kw):
        if name == "psutil":
            raise ImportError("gone")
        return real_import(name, *a, **kw)

    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr("builtins.__import__", fake_import)
    assert helpers._steam_running() is False


def test_steam_running_detects_process(monkeypatch):
    class _FakeIter:
        def __init__(self, info):
            self.info = info

        def __iter__(self):
            return iter(self.info)

    class _FakePsutil:
        NoSuchProcess = Exception
        AccessDenied = Exception

        @staticmethod
        def process_iter(attrs=None):
            class _Proc:
                def __init__(self, name):
                    self.info = {"name": name}

            return [_Proc("notepad.exe"), _Proc("steam.exe")]

    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    monkeypatch.setattr(helpers, "STEAM_PROC_NAME", "steam.exe")
    assert helpers._steam_running() is True


def test_apply_autostart_delegates(monkeypatch):
    seen = []
    monkeypatch.setattr(
        autostart, "set_enabled", lambda v: seen.append(bool(v))
    )
    helpers._apply_autostart(True)
    helpers._apply_autostart(0)
    assert seen == [True, False]


def test_relaunch_elevated_reports_failure(monkeypatch):
    def fail(*a, **kw):
        raise OSError("cancelled")

    # ShellExecuteW raising (user cancels UAC) -> False, no crash.
    monkeypatch.setattr(
        helpers.ctypes.windll.shell32,
        "ShellExecuteW",
        fail,
        raising=False,
    )
    assert helpers._relaunch_elevated() is False
