"""Headless tests for appsettings persistence (appsettings.py).

Guards the settings.json loader against the two hand-edit footguns that
silently reverted every focus-flash A/B toggle during live testing:

* a UTF-8 BOM (Notepad / PowerShell Set-Content -Encoding UTF8 both add one)
  used to make json.load under plain "utf-8" raise, which the loader caught
  and swallowed by returning defaults;
* a stray trailing comma / missing quote (invalid JSON) which did the same.

Both must load successfully and preserve the hand-edited value."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import appsettings


def _write(tmpdir, name, raw_bytes):
    p = os.path.join(str(tmpdir), name)
    with open(p, "wb") as f:
        f.write(raw_bytes)
    return p


def _load_from(tmpdir):
    # Settings now live in the per-user appdata dir; point user_data_dir at the
    # temp dir so tests never touch the real %APPDATA%.
    old_ud = appsettings.user_data_dir
    appsettings.user_data_dir = lambda: str(tmpdir)
    try:
        return appsettings._load_settings()
    finally:
        appsettings.user_data_dir = old_ud


def test_migrates_old_exe_dir_settings(tmpdir):
    """A settings.json left next to the exe (pre-appdata layout) is copied into
    the appdata dir on first load, so existing users keep their settings."""
    data = {"skin": "Gruvbox", "rumble_enabled_sc": True}
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))

    # Simulate the appdata dir being elsewhere: the old file is in tmpdir
    # (exe dir), the new location is a subdir.
    appdata_dir = os.path.join(str(tmpdir), "appdata")
    os.makedirs(appdata_dir, exist_ok=True)
    old_ud = appsettings.user_data_dir
    old_exe = appsettings._exe_dir
    appsettings.user_data_dir = lambda: appdata_dir
    appsettings._exe_dir = lambda: str(tmpdir)
    try:
        s = appsettings._load_settings()
        assert s["skin"] == "Gruvbox"
        # The migrated copy now lives in appdata.
        assert os.path.isfile(os.path.join(appdata_dir, "settings.json"))
    finally:
        appsettings.user_data_dir = old_ud
        appsettings._exe_dir = old_exe


def test_loads_settings_with_utf8_bom(tmpdir):
    data = {"steam_input_nudge": "event", "skin": "Gruvbox"}
    raw = b"\xef\xbb\xbf" + json.dumps(data, indent=2).encode("utf-8")
    _write(tmpdir, "settings.json", raw)
    s = _load_from(tmpdir)
    assert s["steam_input_nudge"] == "event"
    assert s["skin"] == "Gruvbox"


def test_loads_settings_plain_utf8(tmpdir):
    data = {"steam_input_nudge": "hop-noactivate"}
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["steam_input_nudge"] == "hop-noactivate"


def test_invalid_json_falls_back_to_defaults(tmpdir):
    _write(
        tmpdir, "settings.json", b'"steam_input_nudge: "hop-fast",\n'
    )  # missing closing quote
    s = _load_from(tmpdir)
    assert (
        s["steam_input_nudge"]
        == appsettings.DEFAULT_SETTINGS["steam_input_nudge"]
    )


def test_missing_settings_file_falls_back_to_defaults(tmpdir):
    s = _load_from(tmpdir)
    assert (
        s["steam_input_nudge"]
        == appsettings.DEFAULT_SETTINGS["steam_input_nudge"]
    )


def test_known_keys_coerce_to_default_types(tmpdir):
    # A hand-edited bool stored as 0/1 must come back as a real bool, and a
    # bogus numeric string must not crash the loader.
    data = {"rumble_enabled_sc": 0, "sc_trigger_focus_pull": "not-a-number"}
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["rumble_enabled_sc"] is False
    assert (
        s["sc_trigger_focus_pull"]
        == appsettings.DEFAULT_SETTINGS["sc_trigger_focus_pull"]
    )


def test_bool_key_rejects_truthy_strings(tmpdir):
    # bool("false") is True — the coercion bug this guards. Any string value
    # for a bool key (including "false"/"0") must fall back to the default.
    data = {
        "rumble_enabled_sc": "false",
        "key_sound_enabled_sc": "0",
        "logging_enabled": "yes",
    }
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["rumble_enabled_sc"] is True  # default
    assert s["key_sound_enabled_sc"] is True  # default
    assert s["logging_enabled"] is False  # default
    # Real bools still pass through (including False).
    data = {"rumble_enabled_sc": False, "key_sound_enabled_sc": True}
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["rumble_enabled_sc"] is False
    assert s["key_sound_enabled_sc"] is True


def test_numeric_key_rejects_numeric_strings(tmpdir):
    # A numeric string ("9999") is still a string: it must NOT be cast into
    # the input-thread tuning (a hostile file could otherwise push a bogus
    # calibration value); fall back to the default.
    data = {
        "sc_pad_click_engage": "9999",
        "sc_pad_click_release": "1000",
        "sc_pad_lock_glide_alpha": "0.5",
    }
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["sc_pad_click_engage"] == 2500  # default
    assert s["sc_pad_click_release"] == 1000  # default
    assert s["sc_pad_lock_glide_alpha"] == 0.35  # default
    # Real numbers still pass (bool must NOT — it's an int subclass).
    data = {"sc_pad_click_engage": 1234, "sc_pad_click_release": True}
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["sc_pad_click_engage"] == 1234
    assert s["sc_pad_click_release"] == 1000  # default


def test_skin_rejects_path_traversal_and_absolute(tmpdir):
    # "skins/" + name + ".css" is a read path; a hostile name must not escape
    # data/skins/. All of these must fall back to the default skin.
    hostile = [
        "../evil",
        "..\\..\\evil",
        "../../evil",
        "a/b",
        "a\\b",
        "/abs/evil",
        "\\abs\\evil",
        "C:/windows/evil",
        "C:\\windows\\evil",
        "D:evil",
        "..",
    ]
    for name in hostile:
        data = {"skin": name}
        _write(
            tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8")
        )
        s = _load_from(tmpdir)
        assert s["skin"] == appsettings.DEFAULT_SETTINGS["skin"], name
    # Non-string values are rejected too.
    data = {"skin": 123}
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["skin"] == appsettings.DEFAULT_SETTINGS["skin"]
    # A plain bundled skin name still passes through.
    data = {"skin": "Gruvbox"}
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["skin"] == "Gruvbox"


def test_window_position_per_app_filters_hostile_entries(tmpdir):
    data = {
        "window_position_per_app": {
            "notepad.exe": 3,  # valid — kept
            "wordpad.exe": "5",  # numeric string — dropped
            "chrome.exe": 9,  # out of range — dropped
            "firefox.exe": -1,  # out of range — dropped
            "editor.exe": True,  # bool — dropped
            "": 2,  # empty key — dropped
            "  ": 4,  # whitespace key — dropped
            "2": 1,  # int key -> JSON makes it a string; still a
            # numeric key name is a valid exe-ish string,
            # kept (not a traversal risk)
        }
    }
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["window_position_per_app"] == {"notepad.exe": 3, "2": 1}
    # A non-dict value collapses to the empty default without raising.
    data = {"window_position_per_app": ["evil", 7]}
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["window_position_per_app"] == {}


def test_valid_skin_name_rejects_non_strings_and_blank():
    assert appsettings._valid_skin_name("Gruvbox") == "Gruvbox"
    assert appsettings._valid_skin_name("Digital") == "Digital"
    assert appsettings._valid_skin_name(None) is None
    assert appsettings._valid_skin_name(123) is None
    assert appsettings._valid_skin_name("") is None
    assert appsettings._valid_skin_name("  ") is None
    assert appsettings._valid_skin_name(" Gruvbox") is None
    assert appsettings._valid_skin_name("../evil") is None
    assert appsettings._valid_skin_name("C:\\evil") is None


def test_valid_position_per_app_drops_non_string_keys():
    # _load_settings can't see non-string keys (JSON object keys are always
    # strings), but the validator itself must not crash on them.
    out = appsettings._valid_position_per_app({42: 3, "x.exe": 5})
    assert out == {"x.exe": 5}
    assert appsettings._valid_position_per_app("junk") == {}
    assert appsettings._valid_position_per_app(None) == {}


def test_osk_size_per_app_filters_hostile_entries(tmpdir):
    # Only {exe name: known size name} entries survive; unknown sizes, empty
    # keys and non-dict values are dropped without raising.
    data = {
        "osk_size_per_app": {
            "notepad.exe": "small",  # valid — kept
            "wordpad.exe": "full",  # valid — kept
            "chrome.exe": "giant",  # not a known size — dropped
            "firefox.exe": 3,  # not a string — dropped
            "": "medium",  # empty key — dropped
        }
    }
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["osk_size_per_app"] == {
        "notepad.exe": "small",
        "wordpad.exe": "full",
    }
    # A non-dict value collapses to the empty default without raising.
    data = {"osk_size_per_app": ["small"]}
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["osk_size_per_app"] == {}


def test_skin_per_app_filters_hostile_entries(tmpdir):
    # Each skin name goes through _valid_skin_name (path/traversal check), so
    # a hostile file can't smuggle a read path out of data/skins/.
    data = {
        "skin_per_app": {
            "notepad.exe": "Digital",  # valid — kept
            "wordpad.exe": "../evil",  # traversal — dropped
            "chrome.exe": "C:\\evil",  # absolute path — dropped
            "firefox.exe": " Gruvbox",  # padded — dropped
            "": "Gruvbox",  # empty key — dropped
        }
    }
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["skin_per_app"] == {"notepad.exe": "Digital"}
    # A non-dict value collapses to the empty default without raising.
    data = {"skin_per_app": "Gruvbox"}
    _write(tmpdir, "settings.json", json.dumps(data, indent=2).encode("utf-8"))
    s = _load_from(tmpdir)
    assert s["skin_per_app"] == {}


def test_valid_osk_size_per_app_drops_non_string_keys():
    out = appsettings._valid_osk_size_per_app({42: "small", "x.exe": "full"})
    assert out == {"x.exe": "full"}
    assert appsettings._valid_osk_size_per_app("junk") == {}
    assert appsettings._valid_osk_size_per_app(None) == {}


def test_valid_skin_per_app_drops_non_string_keys():
    out = appsettings._valid_skin_per_app({42: "Gruvbox", "x.exe": "Digital"})
    assert out == {"x.exe": "Digital"}
    assert appsettings._valid_skin_per_app(None) == {}
    assert appsettings._valid_skin_per_app([]) == {}
