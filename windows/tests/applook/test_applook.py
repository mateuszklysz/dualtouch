"""Headless tests for the per-app OSK size + skin look (Feature: per-app OSK
size and skin).

Pure logic lives in triton/applook.py (resolution + recording), with thin
glue in the tray (_update_per_app_look records a tray selection against the
current foreground app) and the launcher (_apply_app_look applies an app's
remembered look on open). No SDL window / display / controller needed.
"""

import types
from typing import Any, cast

from triton import applook
from triton import state as triton_state

# --- applook resolution (pure) ----------------------------------------------


def test_size_for_app_falls_back_to_global():
    # No entry -> None = the caller falls back to the global osk_size.
    assert applook._size_for_app("notepad.exe", {}) is None
    # None exe (non-positionable foreground) never resolves a per-app size.
    assert applook._size_for_app(None, {"notepad.exe": "small"}) is None
    # A stored value that isn't a known size name is treated as absent.
    assert applook._size_for_app("x.exe", {"x.exe": "giant"}) is None


def test_size_for_app_restores_each_apps_own_on_switch():
    per_app = {"notepad.exe": "small", "wordpad.exe": "full"}
    assert applook._size_for_app("notepad.exe", per_app) == "small"
    assert applook._size_for_app("wordpad.exe", per_app) == "full"
    # An app that was never set keeps the global fallback.
    assert applook._size_for_app("chrome.exe", per_app) is None


def test_save_size_for_app_records_and_never_mutates():
    per_app = {"notepad.exe": "medium"}
    out = applook._save_size_for_app("notepad.exe", "small", per_app)
    assert out == {"notepad.exe": "small"}
    # Pure: the input map is left untouched.
    assert per_app == {"notepad.exe": "medium"}
    # Hostile / None values are ignored - returns the same map object.
    assert applook._save_size_for_app("x.exe", "giant", out) is out
    assert applook._save_size_for_app(None, "small", out) is out


def test_skin_for_app_falls_back_to_global():
    assert applook._skin_for_app("notepad.exe", {}) is None
    assert applook._skin_for_app(None, {"notepad.exe": "Digital"}) is None
    # Blank stored value is treated as absent.
    assert applook._skin_for_app("x.exe", {"x.exe": ""}) is None


def test_skin_for_app_restores_each_apps_own_on_switch():
    per_app = {"notepad.exe": "Gruvbox", "wordpad.exe": "Digital"}
    assert applook._skin_for_app("notepad.exe", per_app) == "Gruvbox"
    assert applook._skin_for_app("wordpad.exe", per_app) == "Digital"
    assert applook._skin_for_app("chrome.exe", per_app) is None


def test_save_skin_for_app_records_and_never_mutates():
    per_app = {"notepad.exe": "Gruvbox"}
    out = applook._save_skin_for_app("notepad.exe", "Digital", per_app)
    assert out == {"notepad.exe": "Digital"}
    assert per_app == {"notepad.exe": "Gruvbox"}
    assert applook._save_skin_for_app("x.exe", 123, out) is out
    assert applook._save_skin_for_app(None, "Digital", out) is out


# --- tray glue: a tray size/skin selection records the foreground app -------


def _clear_per_app_state():
    # Per-app maps are deliberately NOT reset by reset_session(); clear them
    # so each test starts from a clean slate and leaves none behind.
    triton_state.set_osk_size_per_app({})
    triton_state.set_skin_per_app({})


def test_update_per_app_look_records_foreground_app(monkeypatch):
    import tray.app as tray_app

    _clear_per_app_state()
    fake = types.SimpleNamespace(
        settings={"osk_size_per_app": {}, "skin_per_app": {}}
    )
    method = tray_app.App._update_per_app_look.__get__(fake, tray_app.App)

    monkeypatch.setattr(
        tray_app, "_foreground_exe_name", lambda: "notepad.exe"
    )
    method("osk_size_per_app", "small")
    method("skin_per_app", "Digital")
    assert fake.settings["osk_size_per_app"] == {"notepad.exe": "small"}
    assert fake.settings["skin_per_app"] == {"notepad.exe": "Digital"}
    # The maps are republished to triton state for the launcher to read.
    assert triton_state.get_osk_size_per_app() == {"notepad.exe": "small"}
    assert triton_state.get_skin_per_app() == {"notepad.exe": "Digital"}
    # Recording the same value again is a no-op (no redundant write).
    method("osk_size_per_app", "small")
    assert fake.settings["osk_size_per_app"] == {"notepad.exe": "small"}

    # None foreground (no positionable app): only the global is set by the
    # caller — no per-app entry is recorded.
    monkeypatch.setattr(tray_app, "_foreground_exe_name", lambda: None)
    method("skin_per_app", "Grape")
    assert fake.settings["skin_per_app"] == {"notepad.exe": "Digital"}
    _clear_per_app_state()


# --- launcher glue: an open applies the foreground app's remembered look -----


def test_apply_app_look_applies_per_app_size_and_skin(monkeypatch):
    import triton.screen as Tscreen
    import triton.skins as Tskins
    from tray import launcher as L

    _clear_per_app_state()
    calls = {"size": [], "skin": [], "rebuild": 0}
    fake = types.SimpleNamespace(
        settings={"osk_size": "medium", "skin": "Gruvbox"}, _cached_screen=None
    )
    fake._rebuild_cached_screen = lambda: calls.__setitem__(
        "rebuild", calls["rebuild"] + 1
    )
    monkeypatch.setattr(L, "_foreground_exe_name", lambda: "notepad.exe")
    monkeypatch.setattr(Tscreen, "get_osk_size", lambda: "medium")
    monkeypatch.setattr(
        Tscreen, "set_osk_size", lambda n: calls["size"].append(n)
    )
    monkeypatch.setattr(
        Tskins, "available_skins", lambda: ["Gruvbox", "Digital"]
    )
    monkeypatch.setattr(
        Tskins, "set_active_skin", lambda n: calls["skin"].append(n)
    )

    triton_state.set_osk_size_per_app({"notepad.exe": "small"})
    triton_state.set_skin_per_app({"notepad.exe": "Digital"})
    L._LauncherMixin._apply_app_look(cast(Any, fake))
    # Per-app size differs from the active one -> republish + rebuild the
    # cached Screen (size is baked in at construction).
    assert calls["size"] == ["small"]
    assert calls["rebuild"] == 1
    assert calls["skin"] == ["Digital"]
    _clear_per_app_state()


def test_apply_app_look_falls_back_to_global_when_no_entry(monkeypatch):
    import triton.screen as Tscreen
    import triton.skins as Tskins
    from tray import launcher as L

    _clear_per_app_state()
    calls = {"size": [], "skin": [], "rebuild": 0}
    fake = types.SimpleNamespace(
        settings={"osk_size": "medium", "skin": "Gruvbox"}, _cached_screen=None
    )
    fake._rebuild_cached_screen = lambda: calls.__setitem__(
        "rebuild", calls["rebuild"] + 1
    )
    monkeypatch.setattr(L, "_foreground_exe_name", lambda: "notepad.exe")
    monkeypatch.setattr(Tscreen, "get_osk_size", lambda: "medium")
    monkeypatch.setattr(
        Tscreen, "set_osk_size", lambda n: calls["size"].append(n)
    )
    monkeypatch.setattr(
        Tskins, "available_skins", lambda: ["Gruvbox", "Digital"]
    )
    monkeypatch.setattr(
        Tskins, "set_active_skin", lambda n: calls["skin"].append(n)
    )

    # No per-app entries: the global osk_size / skin win, and a matching size
    # does not rebuild the cached Screen.
    triton_state.set_osk_size_per_app({})
    triton_state.set_skin_per_app({})
    L._LauncherMixin._apply_app_look(cast(Any, fake))
    assert calls["size"] == []
    assert calls["rebuild"] == 0
    assert calls["skin"] == ["Gruvbox"]

    # A saved per-app skin that no longer matches a bundled skin (removed or
    # renamed) falls back to the default rather than a no-palette skin.
    triton_state.set_skin_per_app({"notepad.exe": "RemovedSkin"})
    calls["skin"].clear()
    L._LauncherMixin._apply_app_look(cast(Any, fake))
    assert calls["skin"] == [Tskins.DEFAULT_SKIN]
    _clear_per_app_state()
