"""Layer 1/2: cursor_helper — marker parsing, token handling, trust set
and main() exit codes (win32 cursor internals are not touched)."""

import sys

import cursor_helper


def test_load_token_from_args():
    # Sets the module-global token; returns nothing.
    cursor_helper._load_token_from_args(
        ["prog", "--daemon", "--token", "abc123"]
    )
    assert cursor_helper._TOKEN == "abc123"
    # Missing value / absent flag -> None.
    cursor_helper._load_token_from_args(["p", "--token"])
    assert cursor_helper._TOKEN is None
    cursor_helper._load_token_from_args(["p"])
    assert cursor_helper._TOKEN is None
    cursor_helper._load_token_from_args(["p", "--token", " tok "])
    assert cursor_helper._TOKEN == "tok"


def test_parse_marker_matrix():
    ok = cursor_helper._parse_marker("hide|4242|sekrit", "sekrit")
    assert ok == ("hide", 4242)
    assert cursor_helper._parse_marker("show|1|x", "y") is None  # token
    assert cursor_helper._parse_marker("hide|4242|sekrit", None) is None
    assert cursor_helper._parse_marker("", "t") is None
    assert cursor_helper._parse_marker(None, "t") is None
    assert cursor_helper._parse_marker("hide|4242", "t") is None
    assert cursor_helper._parse_marker("hide|4242|a|b", "a") is None
    assert cursor_helper._parse_marker("sleep|4242|t", "t") is None
    assert cursor_helper._parse_marker("hide|notapint|t", "t") is None
    assert cursor_helper._parse_marker("hide|0|t", "t") is None  # pid<=1
    assert cursor_helper._parse_marker("hide|-3|t", "t") is None


def test_allowed_image_names_frozen_vs_source(monkeypatch):
    names = cursor_helper._allowed_image_names(frozen=True)
    lowered = {n for n in names}
    assert "dualtouch-cursor-helper.exe" in lowered
    assert "dualtouch-windows.exe" in lowered
    monkeypatch.delattr(sys, "frozen", raising=False)
    src = cursor_helper._allowed_image_names(frozen=False)
    assert src == {sys.executable.lower().rsplit("\\", 1)[-1]}


class _FakePsutil:
    @staticmethod
    def Process(_pid):
        raise _Boom("no such process")


def test_pid_is_trusted_rejects_junk(monkeypatch):
    import types

    monkeypatch.setitem(sys.modules, "psutil", types.ModuleType("psutil_fake"))
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    assert not cursor_helper._pid_is_trusted(0)
    assert not cursor_helper._pid_is_trusted(-5)
    assert not cursor_helper._pid_is_trusted(99999)  # any error -> False


class _Boom(Exception):
    pass


def _boom(_pid):
    raise _Boom("no such process")


def test_read_marker_strips_bom(tmp_path, monkeypatch):
    marker = tmp_path / "cursor_action.txt"
    marker.write_text("﻿show|1|t", encoding="utf-8")
    monkeypatch.setattr(cursor_helper, "_MARKER", str(marker))
    assert cursor_helper._read_marker() == "show|1|t"
    marker.unlink()
    assert cursor_helper._read_marker() is None


def test_main_one_shot_paths(tmp_path, monkeypatch, capsys):
    marker = tmp_path / "cursor_action.txt"
    monkeypatch.setattr(cursor_helper, "_MARKER", str(marker))
    monkeypatch.setattr(sys, "argv", ["helper"])

    assert cursor_helper.main() == 1  # no --token at all

    monkeypatch.setattr(sys, "argv", ["helper", "--token", "t"])
    assert cursor_helper.main() == 0  # no marker: nothing to do

    marker.write_text("garbage")
    assert cursor_helper.main() == 1  # unparseable marker

    marker.write_text("hide|999999|wrong-token")
    assert cursor_helper.main() == 1  # bad token refused


def test_daemon_loop_fails_closed_without_token(monkeypatch):
    shown = []
    monkeypatch.setattr(cursor_helper, "_show", lambda *a: shown.append(1))
    monkeypatch.setattr(cursor_helper, "_TOKEN", None)
    rc = cursor_helper._daemon_loop()
    assert rc == 1
    assert shown == [1]  # cursor restored before bailing out
