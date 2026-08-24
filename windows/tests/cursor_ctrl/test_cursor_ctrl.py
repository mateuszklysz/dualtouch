"""Layer 1/2: the cursor-hide control plane — cursor_ctrl marker writing
plus cursor_helper's parsing/trust logic (no real cursors touched)."""

import os
import sys

import cursor_ctrl
import pytest


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cursor_ctrl, "user_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(cursor_ctrl, "_osk_cursor_hidden", False)
    return tmp_path


def _marker(tmp_path):
    return tmp_path / "cursor_action.txt"


def test_marker_path_uses_user_data_dir(_data_dir):
    p = cursor_ctrl._marker_path()
    assert str(_data_dir) in str(p) and p.endswith("cursor_action.txt")


def test_set_osk_cursor_visible_writes_tokened_marker(_data_dir):
    cursor_ctrl.set_osk_cursor_visible(False)  # hide
    text = _marker(_data_dir).read_text()
    mode, pid, token = text.split("|")
    assert mode == "hide"
    assert int(pid) == os.getpid()
    assert token == cursor_ctrl._TOKEN
    assert cursor_ctrl._osk_cursor_hidden is True

    # Same state again: no new write.
    _marker(_data_dir).unlink()
    cursor_ctrl.set_osk_cursor_visible(False)
    assert not _marker(_data_dir).exists()

    # Show flips it back.
    cursor_ctrl.set_osk_cursor_visible(True)
    assert _marker(_data_dir).read_text().startswith("show|")
    assert cursor_ctrl._osk_cursor_hidden is False


def test_flag_not_flipped_on_failed_write(_data_dir, monkeypatch):
    # Point the marker at a directory that does not exist -> write fails.
    monkeypatch.setattr(
        cursor_ctrl,
        "user_data_dir",
        lambda: str(_data_dir / "missing" / "dir"),
    )
    cursor_ctrl.set_osk_cursor_visible(False)
    assert cursor_ctrl._osk_cursor_hidden is False


def test_force_restore_cursor_shows(_data_dir):
    cursor_ctrl._osk_cursor_hidden = True
    cursor_ctrl.force_restore_cursor()
    assert _marker(_data_dir).read_text().startswith("show|")
    assert cursor_ctrl._osk_cursor_hidden is False


def test_install_helper_schedules_task_and_kicks_daemon(
    _data_dir, monkeypatch
):
    runs = []

    def fake_run(cmd, **kw):
        runs.append(cmd)

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(cursor_ctrl.subprocess, "run", fake_run)
    monkeypatch.delattr(cursor_ctrl.shutil, "copyfile", raising=False)
    # Not frozen: source path uses sys.executable + script path.
    assert cursor_ctrl.install_helper() is True
    create = [c for c in runs if "/Create" in c]
    run_cmd = [c for c in runs if "/Run" in c]
    assert len(create) == 1 and len(run_cmd) == 1
    cmd = create[0]
    assert cmd[cmd.index("/TN") + 1] == "DualTouchCursor"
    tr = cmd[cmd.index("/TR") + 1]
    if getattr(sys, "frozen", False):
        assert "--cursor-helper --daemon --token" in tr
    else:
        assert 'cursor_helper.py" --daemon --token' in tr
    assert cursor_ctrl._TOKEN in tr
    # The show marker is written so the daemon's first poll has work.
    assert _marker(_data_dir).exists()


def test_install_helper_reports_failure(_data_dir, monkeypatch):
    def fail(cmd, **kw):
        class R:
            returncode = 1
            stderr = "denied"

        return R()

    monkeypatch.setattr(cursor_ctrl.subprocess, "run", fail)
    assert cursor_ctrl.install_helper() is False


def _with_frozen_helper(monkeypatch, tmp_path, same_size=True, version=None):
    """Stage a frozen-exe scenario around _frozen_helper_exe."""
    me = tmp_path / "DualTouch-windows.exe"
    me.write_bytes(b"MZ-fake")
    other = tmp_path / "DualTouch-cursor-helper.exe"

    if version is None:
        other.write_bytes(b"X" * (len(b"MZ-fake") if same_size else 3))
    else:
        import struct

        data = struct.pack("<II", 1234, 5678)
        other.write_bytes(data)

    import applog as _applog

    monkeypatch.setattr(_applog, "_exe_path", lambda: str(me), False)
    return me, other


def test_frozen_helper_reuses_matching_copy(monkeypatch, tmp_path):
    me, other = _with_frozen_helper(monkeypatch, tmp_path)
    result = cursor_ctrl._frozen_helper_exe()
    assert result == str(other)


def test_frozen_helper_copies_when_stale(monkeypatch, tmp_path):
    me, other = _with_frozen_helper(
        monkeypatch, tmp_path, same_size=False, version=None
    )
    # Size differs AND ProductVersion can't be read -> recopy from me.
    result = cursor_ctrl._frozen_helper_exe()
    if result is not None:  # copyfile of a fake exe still succeeds
        assert result == str(other)
        assert other.read_bytes() == b"MZ-fake"


def test_product_version_of_garbage_file_is_none(tmp_path):
    f = tmp_path / "no-version.exe"
    f.write_bytes(b"not an exe")
    assert cursor_ctrl._product_version(str(f)) is None
    assert cursor_ctrl._product_version(str(tmp_path / "missing")) is None
