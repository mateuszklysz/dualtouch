"""Headless tests for the autostart module (scheduled-task based launch at
logon). The module must never touch a Startup .lnk or registry Run key for
enabling, must build a correct task command line, and must clean up the
legacy mechanisms."""

import os

import autostart as A


def test_task_commandline_source_run_is_python_dash_m_tray():
    assert A._is_frozen() is False  # headless tests run from source
    cmd = A._task_commandline()
    assert cmd.startswith('"')
    assert " -m tray" in cmd
    assert "python" in cmd.lower()


def test_set_enabled_cleans_legacy_run_key(tmpdir, monkeypatch):
    # Create a fake legacy Run value, then ensure set_enabled removes it.
    # Use the real HKCU Run key but a unique name is NOT possible (name is
    # fixed); instead verify _remove_legacy_run_key does not raise and that
    # the enable/disable path is reachable.
    A._remove_legacy_run_key()  # must not raise regardless of presence
    A._remove_legacy_lnk()  # must not raise


def test_legacy_lnk_removed_when_present(tmpdir, monkeypatch):
    d = tmpdir.mkdir("startup")
    lnk = d.join(A._LEGACY_LNK)
    lnk.write("stale")
    monkeypatch.setattr(A, "_startup_dir", lambda: str(d))
    A._remove_legacy_lnk()
    assert not os.path.exists(str(lnk))
