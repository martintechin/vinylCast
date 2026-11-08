import subprocess
import sys
from pathlib import Path

import pytest

from vinylcast.cli import COMMANDS, EXIT_NOT_IMPLEMENTED, build_parser, config_from_args, main


def test_help_lists_all_subcommands() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "vinylcast", "--help"], capture_output=True, text=True, check=True
    ).stdout
    for name in ("devices", "levels", "serve", "cast", "stop", "run"):
        assert name in out
    assert len(COMMANDS) == 6


def test_stub_returns_not_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["levels"]) == EXIT_NOT_IMPLEMENTED
    assert "not implemented" in capsys.readouterr().err


def test_flags_override_toml(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text('codec = "flac"\nhttp_port = 9000\n')
    args = build_parser().parse_args(
        ["run", "--config", str(path), "--codec", "mp3", "--to", "Kitchen"]
    )
    cfg = config_from_args(args)
    assert (cfg.codec, cfg.http_port, cfg.cast_target) == ("mp3", 9000, "Kitchen")


def test_unset_flags_do_not_override(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text('source = "tone"\n')
    cfg = config_from_args(build_parser().parse_args(["serve", "--config", str(path)]))
    assert cfg.source == "tone"


def test_invalid_config_is_usage_error() -> None:
    with pytest.raises(SystemExit) as e:
        main(["serve", "--http-port", "0"])
    assert e.value.code == 2
