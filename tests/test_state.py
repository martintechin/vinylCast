import json
from pathlib import Path

import pytest

from vinylcast.config import ConfigError, load_config
from vinylcast.state import PersistentState, StateStore


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert StateStore(tmp_path / "state.json").load() == PersistentState()


def test_round_trip_and_atomic_write(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "sub" / "state.json")
    store.save(PersistentState("Tvättstugan", {"codec": "flac", "auto_cast": False}))
    assert store.load() == PersistentState("Tvättstugan", {"codec": "flac", "auto_cast": False})
    assert [p.name for p in store.path.parent.iterdir()] == ["state.json"]  # no temp files left
    assert "Tvättstugan" in store.path.read_text(encoding="utf-8")


def test_corrupt_file_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert StateStore(path).load() == PersistentState()
    assert (tmp_path / "state.json.bad").exists() and not path.exists()


def test_invalid_settings_moved_aside(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"last_target": "X", "settings": {"alsa_device": "hw:0"}}))
    assert StateStore(path).load() == PersistentState()
    assert (tmp_path / "state.json.bad").exists()


def test_save_rejects_non_ui_keys(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        StateStore(tmp_path / "s.json").save(PersistentState(None, {"http_port": 1}))


def test_precedence_defaults_toml_settings_cli(tmp_path: Path) -> None:
    toml = tmp_path / "v.toml"
    toml.write_text('codec = "flac"\nstop_after_silence_s = 120\nauto_cast = true\n')
    cfg = load_config(toml, overrides={"auto_cast": True},
                      settings={"codec": "mp3", "auto_cast": False})  # fmt: skip
    assert cfg.codec == "mp3"  # UI setting beats TOML
    assert cfg.stop_after_silence_s == 120.0  # TOML beats default
    assert cfg.auto_cast is True  # CLI beats UI setting


def test_bool_type_checked(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(settings={"auto_cast": 1})
    with pytest.raises(ConfigError):
        load_config(settings={"detect_on_s": True})
