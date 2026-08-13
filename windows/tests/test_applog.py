"""Headless tests for applog.py — the logging gate + per-user log path.

dualtouch.log must only be written while logging is enabled (tray toggle),
and it must live in the per-user appdata dir (user_data_dir), never next to
the exe.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import applog


def _tmp_user_dir(tmpdir):
    d = os.path.join(str(tmpdir), "DualTouch")
    os.makedirs(d, exist_ok=True)
    return d


def test_log_gate_disabled_writes_nothing(tmpdir):
    d = _tmp_user_dir(tmpdir)
    old_ud = applog.user_data_dir
    applog.user_data_dir = lambda: d
    applog.set_logging_enabled(False)
    try:
        applog._log("should not appear")
        assert not os.path.isfile(os.path.join(d, "dualtouch.log"))
    finally:
        applog.set_logging_enabled(True)
        applog.user_data_dir = old_ud


def test_log_gate_enabled_writes_to_user_dir(tmpdir):
    d = _tmp_user_dir(tmpdir)
    old_ud = applog.user_data_dir
    applog.user_data_dir = lambda: d
    applog.set_logging_enabled(True)
    try:
        applog._LOG_PATH = None
        applog._log("hello logging")
        p = os.path.join(d, "dualtouch.log")
        assert os.path.isfile(p)
        with open(p, encoding="utf-8") as f:
            assert "hello logging" in f.read()
    finally:
        applog.set_logging_enabled(True)
        applog.user_data_dir = old_ud


def test_log_gate_disabled_creates_no_file_no_dir(tmpdir):
    """With logging disabled, even a write attempt must create NEITHER the
    log file NOR its parent directory — and every module that mirrors
    diagnostics (applog._log, log_line, steam_shortcut, triton focus log)
    funnels through the same gate."""
    # user_data_dir points at a path that does NOT exist yet.
    d = os.path.join(str(tmpdir), "not-created", "DualTouch")
    old_ud = applog.user_data_dir
    applog.user_data_dir = lambda: d
    applog.set_logging_enabled(False)
    try:
        import steam_shortcut
        import triton.win32

        applog._log("applog line")
        applog.log_line("applog", "tagged line")
        steam_shortcut._log("steam_shortcut line")
        triton.win32._focus_log("triton focus line")
        assert not os.path.exists(d)
    finally:
        applog.set_logging_enabled(True)
        applog.user_data_dir = old_ud


def test_log_line_enabled_writes_tagged_line(tmpdir):
    """The public log_line writer tags the line it appends (diagnostics grep
    for e.g. "[triton] ..."), and only when logging is enabled."""
    d = _tmp_user_dir(tmpdir)
    old_ud = applog.user_data_dir
    applog.user_data_dir = lambda: d
    applog.set_logging_enabled(True)
    try:
        applog._LOG_PATH = None
        applog.log_line("triton", "open fg=settled")
        p = os.path.join(d, "dualtouch.log")
        assert os.path.isfile(p)
        with open(p, encoding="utf-8") as f:
            assert "[triton] open fg=settled" in f.read()
    finally:
        applog.set_logging_enabled(True)
        applog.user_data_dir = old_ud


def test_resolve_log_action_open_when_enabled_and_present(tmpdir):
    """Tray "View Log": logging enabled AND the file exists -> open it."""
    d = _tmp_user_dir(tmpdir)
    old_ud = applog.user_data_dir
    applog.user_data_dir = lambda: d
    applog.set_logging_enabled(True)
    try:
        p = os.path.join(d, "dualtouch.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("hello logging")
        action, path = applog.resolve_log_action()
        assert action == "open"
        assert path == p
    finally:
        applog.set_logging_enabled(True)
        applog.user_data_dir = old_ud


def test_resolve_log_action_enabled_but_file_absent(tmpdir):
    """Logging ON but the file never written (enabled-but-never-written) must
    NOT silently open nothing: it is an "enable first" prompt like
    logging-off, and the decision must not create the file."""
    d = _tmp_user_dir(tmpdir)
    old_ud = applog.user_data_dir
    applog.user_data_dir = lambda: d
    applog.set_logging_enabled(True)
    try:
        action, path = applog.resolve_log_action()
        assert action == "enable-first"
        assert path == os.path.join(d, "dualtouch.log")
        # resolve_log_action itself must never create the file.
        assert not os.path.exists(path)
    finally:
        applog.set_logging_enabled(True)
        applog.user_data_dir = old_ud


def test_resolve_log_action_disabled_no_file(tmpdir):
    d = _tmp_user_dir(tmpdir)
    old_ud = applog.user_data_dir
    applog.user_data_dir = lambda: d
    applog.set_logging_enabled(False)
    try:
        action, path = applog.resolve_log_action()
        assert action == "enable-first"
        assert path == os.path.join(d, "dualtouch.log")
    finally:
        applog.set_logging_enabled(True)
        applog.user_data_dir = old_ud


# --- HIGH-3: cursor marker authentication -----------------------------------
# cursor_helper._parse_marker is the pure validation of the cursor marker
# ("mode|pid|token") against the per-session token. The tray stamps every
# marker with a fresh random token and the helper only honors markers that
# carry the exact token it was launched with — this is what stops a
# same-user process from writing "hide|1" (PID 1 = System, always "alive")
# and blanking every system cursor forever.


def _cursor_helper(tmpdir):
    """Import cursor_helper with applog.user_data_dir pointed at a tmp dir
    (the module computes its marker/log paths at import time), then return
    it. Imported lazily so headless tests never touch the real appdata."""
    d = _tmp_user_dir(tmpdir)
    old_ud = applog.user_data_dir
    applog.user_data_dir = lambda: d
    try:
        import cursor_helper

        return cursor_helper
    finally:
        applog.user_data_dir = old_ud


def test_parse_marker_valid(tmpdir):
    ch = _cursor_helper(tmpdir)
    tok = "deadbeef" * 4  # token_hex(16) -> 32 hex chars
    assert ch._parse_marker(f"hide|1234|{tok}", tok) == ("hide", 1234)
    assert ch._parse_marker(f"show|9999|{tok}", tok) == ("show", 9999)


def test_parse_marker_rejects_wrong_or_missing_token(tmpdir):
    ch = _cursor_helper(tmpdir)
    tok = "a" * 32
    # Different token: an attacker's "hide|1" forged without the session key.
    assert ch._parse_marker("hide|1|{}".format("f" * 32), tok) is None
    # No token at all (legacy / pre-hardening marker format).
    assert ch._parse_marker("hide|1", tok) is None
    assert ch._parse_marker("hide|1|", tok) is None
    # Unknown helper token: fail closed.
    assert ch._parse_marker(f"hide|1234|{tok}", None) is None


def test_parse_marker_rejects_system_or_invalid_pid(tmpdir):
    ch = _cursor_helper(tmpdir)
    tok = "a" * 32
    # PID 1 = System (always "alive") and other degenerate pids are refused.
    assert ch._parse_marker(f"hide|1|{tok}", tok) is None
    assert ch._parse_marker(f"hide|0|{tok}", tok) is None
    assert ch._parse_marker(f"hide|-5|{tok}", tok) is None


def test_parse_marker_rejects_malformed(tmpdir):
    ch = _cursor_helper(tmpdir)
    tok = "a" * 32
    assert ch._parse_marker("", tok) is None
    assert ch._parse_marker(None, tok) is None
    assert ch._parse_marker("hide|1234", tok) is None  # 2 parts
    assert ch._parse_marker(f"hide|1234|{tok}|extra", tok) is None
    assert ch._parse_marker(f"hide|abc|{tok}", tok) is None  # non-int pid
    assert ch._parse_marker(f"frobnicate|1234|{tok}", tok) is None
    assert ch._parse_marker(f"HIDE|1234|{tok}", tok) is None
