from pathlib import Path

import pytest

from vinylcast.config import Config, ConfigError, load_config


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "vinylcast.toml"
    p.write_text(text)
    return p


def test_defaults() -> None:
    cfg = load_config()
    assert cfg == Config()
    assert cfg.source == "alsa"
    assert cfg.codec == "mp3"
    assert cfg.http_port == 8800
    assert cfg.advertise_ip is None
    assert cfg.client_max_buffer_s == 10.0


def test_toml_values(tmp_path: Path) -> None:
    path = write(
        tmp_path, 'source = "tone"\ncodec = "flac"\nhttp_port = 9000\nadvertise_ip = "auto"\n'
    )
    cfg = load_config(path)
    assert (cfg.source, cfg.codec, cfg.http_port, cfg.advertise_ip) == ("tone", "flac", 9000, None)


def test_override_beats_toml(tmp_path: Path) -> None:
    path = write(tmp_path, 'codec = "flac"\nhttp_port = 9000\n')
    cfg = load_config(path, {"codec": "mp3"})
    assert cfg.codec == "mp3"
    assert cfg.http_port == 9000


def test_example_config_loads() -> None:
    example = Path(__file__).parent.parent / "config.example.toml"
    assert load_config(example) == Config()


@pytest.mark.parametrize(
    "text",
    [
        "bogus = 1\n",
        'source = "usb"\n',
        'codec = "ogg"\n',
        'http_port = "8800"\n',
        "http_port = 70000\n",
        "client_max_buffer_s = 0\n",
        "sample_rate = true\n",
        'source = "file"\n',
        "codec = \n",
    ],
)
def test_invalid(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_file_source_with_path() -> None:
    cfg = load_config(overrides={"source": "file", "source_file": "x.flac"})
    assert cfg.source_file == "x.flac"
