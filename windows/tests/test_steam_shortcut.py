"""Headless tests for the bundled default Steam Controller config install
(steam_shortcut.install_default_controller_config).

The shortcut's controller config lives at
`Steam Controller Configs/<userid>/config/<appname-lower>/controller_neptune.vdf`
and is installed ONCE (never overwriting an existing/user-edited config), so
first-run users get the perfected OSK layout without configuring the
controller in the Steam UI.
"""

import os

import pytest
from steam_shortcut import (
    _DEFAULT_CONFIG_FILENAME,
    SHORTCUT_APPNAME,
    _braces_balanced,
    _shortcuts_blob_valid,
    _shortcuts_blob_valid_without_entry,
    default_controller_config_target,
    install_default_artwork,
    install_default_controller_config,
    remove_shortcut,
)


@pytest.fixture
def fake_steam(tmpdir, monkeypatch):
    """A fake steam tree with the active userdata dir, so the install target
    resolves inside tmpdir without touching the real Steam install."""
    steam = tmpdir.mkdir("steam")
    ud = steam.mkdir("userdata").mkdir("12345")
    ud.join("config").mkdir()
    monkeypatch.setattr("steam_shortcut.find_steam_path", lambda: str(steam))
    monkeypatch.setattr(
        "steam_shortcut.find_active_userdata_dir", lambda *a: str(ud)
    )
    return steam, ud


def _target(steam, ud):
    return os.path.join(
        str(steam),
        "steamapps",
        "common",
        "Steam Controller Configs",
        "12345",
        "config",
        SHORTCUT_APPNAME.lower(),
        _DEFAULT_CONFIG_FILENAME,
    )


def test_target_path_uses_lowercased_appname(fake_steam):
    steam, ud = fake_steam
    t = default_controller_config_target(str(steam))
    assert t is not None
    assert t == _target(steam, ud)
    assert os.path.basename(os.path.dirname(t)) == SHORTCUT_APPNAME.lower()


def test_install_creates_config_from_bundle(fake_steam):
    steam, ud = fake_steam
    t = _target(steam, ud)
    assert not os.path.exists(t)
    result = install_default_controller_config(str(steam))
    assert result is True
    assert os.path.isfile(t)
    with open(t, encoding="utf-8") as f:
        text = f.read()
    assert _braces_balanced(text)
    # The rewritten url points at the CURRENT fake user's config path: the
    # userid (12345) appears under Steam Controller Configs, not the old
    # bundle's 1559953310.
    assert "1559953310" not in text
    assert (
        os.path.join(
            "Steam Controller Configs",
            "12345",
            "config",
            SHORTCUT_APPNAME.lower(),
        )
        in text
    )


def test_install_never_overwrites_existing_config(fake_steam):
    steam, ud = fake_steam
    t = _target(steam, ud)
    os.makedirs(os.path.dirname(t), exist_ok=True)
    with open(t, "w", encoding="utf-8") as f:
        f.write('"user edited config"')
    result = install_default_controller_config(str(steam))
    assert result is True
    with open(t, encoding="utf-8") as f:
        assert f.read() == '"user edited config"'


def test_install_no_steam_returns_false(tmpdir, monkeypatch):
    monkeypatch.setattr(
        "steam_shortcut.find_steam_path", lambda: str(tmpdir.join("missing"))
    )
    assert install_default_controller_config() is False


# --- Library artwork (grid art for the non-Steam shortcut) -----------------


@pytest.fixture
def fake_art(tmpdir, monkeypatch):
    """A fake steam tree with a grid dir + a fake bundled data/images tree."""
    steam = tmpdir.mkdir("steam2")
    ud = steam.mkdir("userdata").mkdir("12345")
    ud.join("config").mkdir()
    grid = ud.join("config").mkdir("grid")
    images = tmpdir.mkdir("data").mkdir("images")
    # Minimal non-empty PNGs for each grid slot (dimensions don't matter to
    # the install logic — it maps by filename).
    from PIL import Image

    for name, size in [
        ("icon.png", (8, 8)),
        ("capsule.png", (8, 8)),
        ("wide_capsule.png", (8, 8)),
        ("hero.png", (8, 8)),
    ]:
        Image.new("RGBA", size, (255, 0, 0, 255)).save(str(images.join(name)))
    monkeypatch.setattr("steam_shortcut.find_steam_path", lambda: str(steam))
    monkeypatch.setattr(
        "steam_shortcut.find_active_userdata_dir", lambda *a: str(ud)
    )
    monkeypatch.setattr(
        "steam_shortcut.load_state", lambda: {"appid": -1757952265}
    )
    monkeypatch.setattr("applog._bundle_dir", lambda: str(tmpdir))
    return steam, ud, grid


def test_install_artwork_uses_unsigned_appid_names(fake_art):
    steam, ud, grid = fake_art
    result = install_default_artwork(str(steam))
    assert result is True
    # -1757952265 & 0xFFFFFFFF = 2537015031
    for f in (
        "2537015031_icon.png",
        "2537015031p.png",
        "2537015031.png",
        "2537015031_hero.png",
    ):
        assert os.path.isfile(str(grid.join(f))), f"missing {f}"


def test_install_artwork_never_overwrites_existing(fake_art):
    steam, ud, grid = fake_art
    # Simulate the user's own art already present for one slot.
    existing = grid.join("2537015031.png")
    existing.write("user art")
    result = install_default_artwork(str(steam))
    assert result is True
    assert existing.read() == "user art"
    # The other three are installed.
    for f in ("2537015031_icon.png", "2537015031p.png", "2537015031_hero.png"):
        assert os.path.isfile(str(grid.join(f)))


def test_install_artwork_no_appid_returns_false(fake_art, monkeypatch):
    steam, ud, grid = fake_art
    monkeypatch.setattr("steam_shortcut.load_state", lambda: {})
    assert install_default_artwork(str(steam)) is False


# --- Shortcut removal (cleanup / test-from-scratch) -------------------------


@pytest.fixture
def fake_shortcuts(tmpdir, monkeypatch):
    """A fake steam tree with a shortcuts.vdf containing our entry + two
    other shortcuts."""
    import vdf

    steam = tmpdir.mkdir("steam3")
    ud = steam.mkdir("userdata").mkdir("12345")
    cfg = ud.mkdir("config")
    data = {
        "shortcuts": {
            "0": {"appname": "Other Game", "appid": 100},
            "1": {"appname": SHORTCUT_APPNAME, "appid": -1757952265},
            "2": {"appname": "Another", "appid": 200},
        }
    }
    cfg.join("shortcuts.vdf").write_binary(vdf.binary_dumps(data))
    monkeypatch.setattr("steam_shortcut.find_steam_path", lambda: str(steam))
    monkeypatch.setattr(
        "steam_shortcut.find_active_userdata_dir", lambda *a: str(ud)
    )
    return steam, ud, cfg


def test_remove_shortcut_drops_only_dualtouch(fake_shortcuts):
    steam, ud, cfg = fake_shortcuts
    assert remove_shortcut(str(steam)) is True
    import vdf

    parsed = vdf.binary_loads(cfg.join("shortcuts.vdf").read_binary())
    names = [e["appname"] for e in parsed["shortcuts"].values()]
    assert SHORTCUT_APPNAME not in names
    assert sorted(names) == ["Another", "Other Game"]


def test_remove_shortcut_idempotent_when_absent(fake_shortcuts, monkeypatch):
    steam, ud, cfg = fake_shortcuts
    # Remove once, then again — second call is a no-op success.
    assert remove_shortcut(str(steam)) is True
    assert remove_shortcut(str(steam)) is True


def test_shortcut_blob_guards_are_complementary():
    import vdf

    data = {
        "shortcuts": {
            "0": {"appname": "A", "appid": 1},
            "1": {"appname": SHORTCUT_APPNAME, "appid": 2},
        }
    }
    with_entry = vdf.binary_dumps(data)
    dropped = {"shortcuts": {"0": {"appname": "A", "appid": 1}}}
    without_entry = vdf.binary_dumps(dropped)
    assert _shortcuts_blob_valid(with_entry) is True
    assert _shortcuts_blob_valid_without_entry(with_entry) is False
    assert _shortcuts_blob_valid(without_entry) is False
    assert _shortcuts_blob_valid_without_entry(without_entry) is True


def test_shortcut_targets_running_exe_not_cmd():
    """The shortcut must point at the DualTouch executable (resolved from the
    running process), never the old hardcoded cmd.exe dummy."""
    import steam_shortcut as ssc

    exe = ssc._shortcut_exe()
    assert '"cmd.exe"' not in exe
    assert "System32" not in exe
    assert os.path.basename(exe.strip('"')).lower() in (
        "python.exe",
        "pythonw.exe",
        "dualtouch-windows.exe",
    )
    # StartDir is the exe's own directory.
    startdir = ssc._shortcut_startdir().strip('"')
    assert os.path.isdir(startdir)
    assert os.path.basename(os.path.normpath(startdir)) == "windows"


def test_apply_entry_migrates_existing_target_fields():
    """An entry created by an older build (pointing at cmd.exe) must have its
    Exe/StartDir/LaunchOptions refreshed to the real app on re-verify."""
    import steam_shortcut as ssc

    data = {
        "shortcuts": {
            "0": {
                "appname": SHORTCUT_APPNAME,
                "appid": 123,
                "Exe": '"C:\\\\Windows\\\\System32\\\\cmd.exe"',
                "StartDir": '"C:\\\\Windows\\\\System32\\\\"',
                "LaunchOptions": "",
            }
        }
    }
    ssc._apply_entry(data)
    entry = data["shortcuts"]["0"]
    assert '"cmd.exe"' not in entry["Exe"]
    assert entry["Exe"] == ssc._shortcut_exe()
    assert entry["StartDir"] == ssc._shortcut_startdir()
    assert entry["LaunchOptions"] == ssc._shortcut_launch_options()
    # Steam-owned fields stay untouched.
    assert entry["appid"] == 123
