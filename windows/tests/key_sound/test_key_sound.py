"""Layer 1: key_sound resolution, playback and the click debounce."""

import key_sound
import pytest


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    monkeypatch.setattr(key_sound, "_path_cache", {})
    monkeypatch.setattr(key_sound, "_paths_resolved", False)
    monkeypatch.setattr(key_sound, "_last_key_click", 0.0)


@pytest.fixture
def played(monkeypatch):
    calls = []

    def fake_play(sound, flags):
        calls.append((sound, flags))

    monkeypatch.setattr(key_sound.winsound, "PlaySound", fake_play)
    return calls


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(key_sound.time, "monotonic", c)
    return c


def test_play_uses_resolved_steam_path(played, monkeypatch, tmp_path, clock):
    snd = tmp_path / "steamui" / "sounds"
    snd.mkdir(parents=True)
    (snd / "key.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(
        key_sound,
        "_SOUNDS",
        {"key": "steamui/sounds/key.wav"},
        raising=False,
    )
    monkeypatch.setattr(
        key_sound,
        "find_steam_path",
        lambda: str(tmp_path),
        raising=False,
    )
    key_sound.play_key_sound()
    assert len(played) == 1
    path, flags = played[0]
    assert "steamui" in str(path).lower() and str(path).endswith(".wav")
    assert flags & (
        key_sound.winsound.SND_FILENAME | key_sound.winsound.SND_ASYNC
    )
    # Cached: a second call must not re-run find_steam_path.
    n = {"calls": 0}

    def counting():
        n["calls"] += 1
        return str(tmp_path)

    monkeypatch.setattr(key_sound, "find_steam_path", counting, False)
    clock.t += 0.1  # past the debounce
    key_sound.play_key_sound()
    assert n["calls"] == 0  # served from cache
    assert len(played) == 2


def test_no_steam_install_is_silent_and_cached(played, monkeypatch):
    monkeypatch.setattr(
        key_sound, "find_steam_path", lambda: None, raising=False
    )
    key_sound.play_key_sound()
    key_sound.play_open_sound()
    assert played == []
    assert key_sound._path_cache["key"] is None
    assert key_sound._paths_resolved is True


def test_click_debounce_suppresses_rapid_repeats(played, clock, monkeypatch):
    monkeypatch.setattr(
        key_sound,
        "_path_cache",
        {"key": "C:\\x\\key.wav"},
        raising=False,
    )
    monkeypatch.setattr(key_sound, "_paths_resolved", True)
    key_sound.play_key_sound()
    clock.t += 0.01  # under the 25 ms debounce window
    key_sound.play_key_sound()
    assert len(played) == 1
    clock.t += 0.05  # past it: fires again
    key_sound.play_key_sound()
    assert len(played) == 2


def test_open_close_sounds_are_not_debounced(played, clock, monkeypatch):
    monkeypatch.setattr(key_sound, "_paths_resolved", True)
    monkeypatch.setattr(
        key_sound,
        "_path_cache",
        {"open": "C:\\x\\open.wav", "close": "C:\\x\\close.wav"},
        raising=False,
    )
    key_sound.play_open_sound()
    key_sound.play_open_sound()  # same instant: still both fire
    key_sound.play_close_sound()
    names = [str(p) for p, _ in played]
    assert sum("open" in n for n in names) == 2
    assert sum("close" in n for n in names) == 1


def test_resolver_exception_is_swallowed(played, monkeypatch):
    def boom():
        raise RuntimeError("no registry")

    monkeypatch.setattr(key_sound, "find_steam_path", boom, False)
    key_sound.play_key_sound()  # must not raise
    assert played == []
