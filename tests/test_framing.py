import random
import subprocess
from pathlib import Path

import pytest
from conftest import requires_ffmpeg

from vinylcast.framing import (
    FlacFramer,
    Frame,
    Mp3Framer,
    crc8,
    crc16,
    flac_header_len,
    id3v2_size,
    parse_flac_frame_header,
    parse_mp3_header,
    parse_streaminfo,
    split_mp3,
)

FIX = Path(__file__).parent / "fixtures"
MP3_ID3 = (FIX / "mp3_44k_320_id3.mp3").read_bytes()
MP3_V2 = (FIX / "mp3_22k_64.mp3").read_bytes()
FLAC_44 = (FIX / "flac_44k.flac").read_bytes()
FLAC_48 = (FIX / "flac_48k.flac").read_bytes()


def feed_in_chunks(framer: Mp3Framer | FlacFramer, data: bytes, sizes: list[int]) -> list[Frame]:
    out: list[Frame] = []
    pos = i = 0
    while pos < len(data):
        n = sizes[i % len(sizes)]
        out += framer.feed(data[pos : pos + n])
        pos += n
        i += 1
    return out


def decode_errors(data: bytes, tmp_path: Path, suffix: str) -> str:
    path = tmp_path / f"out.{suffix}"
    path.write_bytes(data)
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
    ).stderr


CHUNKINGS = [[1], [3], [7, 1, 300], [1045], [4096], [8192], [100_000]]

# ----------------------------------------------------------------------------- MP3


def test_mp3_header_fields() -> None:
    body = MP3_ID3[id3v2_size(MP3_ID3) :]
    h = parse_mp3_header(body)
    assert h is not None
    assert (h.version, h.layer, h.bitrate_kbps, h.sample_rate, h.samples) == (
        3,
        3,
        320,
        44100,
        1152,
    )
    assert h.frame_len in (1044, 1045)
    h2 = parse_mp3_header(MP3_V2)
    assert h2 is not None and (h2.version, h2.sample_rate, h2.samples) == (2, 22050, 576)
    assert h2.frame_len == 72 * 64000 // 22050 + h2.padding


@pytest.mark.parametrize(
    "header",
    [
        b"\xff\xfb\x00\x00",  # bitrate index 0 (free format)
        b"\xff\xfb\xf0\x00",  # bitrate index 15
        b"\xff\xfb\xec\x00",  # sample-rate index 3
        b"\xff\xeb\xe0\x00",  # version reserved
        b"\xff\xf9\xe0\x00",  # layer reserved
        b"\xff\xfb\xe0\x02",  # emphasis reserved
        b"\xfe\xfb\xe0\x00",  # no sync
        b"\xff\xfb\xe0",  # too short
    ],
)
def test_mp3_header_rejects(header: bytes) -> None:
    assert parse_mp3_header(header) is None


def test_id3v2_size() -> None:
    size = id3v2_size(MP3_ID3)
    assert size is not None and size > 10
    assert parse_mp3_header(MP3_ID3, size) is not None
    assert id3v2_size(b"\xff\xfb\xe0\x00" * 3) == 0
    assert id3v2_size(b"ID3\x04") is None  # need more bytes


@pytest.mark.parametrize("data", [MP3_ID3, MP3_V2], ids=["mpeg1-id3", "mpeg2"])
def test_mp3_split_whole_file(data: bytes) -> None:
    frames = Mp3Framer().feed(data)
    body = data[id3v2_size(data) :]
    # Everything but the last frame (held back until the next header confirms it... or not:
    # once synced the last complete frame is emitted) is covered, in order, without gaps.
    joined = b"".join(f.data for f in frames)
    assert body.startswith(joined)
    assert len(body) - len(joined) < 1045
    assert all(parse_mp3_header(f.data) is not None for f in frames)
    total = sum(f.duration_s for f in frames)
    assert 0.9 < total <= 1.1


@pytest.mark.parametrize("sizes", CHUNKINGS, ids=str)
def test_mp3_chunk_boundaries_anywhere(sizes: list[int]) -> None:
    """Chunks ending mid-header and mid-frame give exactly the same frames."""
    assert feed_in_chunks(Mp3Framer(), MP3_ID3, sizes) == Mp3Framer().feed(MP3_ID3)


def test_mp3_resync_after_garbage() -> None:
    body = MP3_ID3[id3v2_size(MP3_ID3) :]
    reference = Mp3Framer().feed(body)
    rng = random.Random(1)
    junk = bytes(rng.randrange(256) for _ in range(3000)) + b"\xff\xfb\x90\x00" + body[500:]
    framer = Mp3Framer()
    frames = framer.feed(junk + body)
    assert framer.skipped_bytes >= 3000
    # After resync the framer emits exactly the reference frames of the clean copy.
    assert frames[-len(reference) :] == reference


def test_mp3_false_sync_needs_confirmation() -> None:
    # A valid-looking header whose "next frame" is not a header must not be accepted.
    fake = b"\xff\xfb\xe0\x00" + b"\x00" * 2000
    frames, consumed, synced, skipped = split_mp3(fake, synced=False)
    assert frames == [] and not synced and skipped >= 1


@requires_ffmpeg
def test_mp3_mid_stream_join_decodes(tmp_path: Path) -> None:
    frames = Mp3Framer().feed(MP3_ID3)
    joined = b"".join(f.data for f in frames[17:])
    assert decode_errors(joined, tmp_path, "mp3") == ""


# ---------------------------------------------------------------------------- FLAC


def test_crc_check_values() -> None:
    assert crc8(b"123456789") == 0xF4  # CRC-8, poly 0x07, init 0
    assert crc16(b"123456789") == 0xFEE8  # CRC-16/BUYPASS, poly 0x8005, init 0


def test_flac_header_capture() -> None:
    hlen = flac_header_len(FLAC_44)
    assert hlen is not None
    assert parse_flac_frame_header(FLAC_44, hlen) is not None  # frames start right after
    info = parse_streaminfo(FLAC_44[:hlen])
    assert (info.sample_rate, info.channels, info.bits_per_sample) == (44100, 2, 16)
    for cut in (0, 3, 4, 7, 8, 42, hlen - 1):
        assert flac_header_len(FLAC_44[:cut]) is None
    with pytest.raises(ValueError):
        flac_header_len(b"ID3\x04\x00")


def test_flac_frame_header_crc_is_checked() -> None:
    hlen = flac_header_len(FLAC_44)
    assert hlen is not None
    h = parse_flac_frame_header(FLAC_44, hlen)
    assert h is not None and h.block_size == 4608 and h.sample_rate == 44100
    broken = bytearray(FLAC_44[hlen : hlen + 32])
    broken[h.header_len - 1] ^= 0x01  # flip a CRC-8 bit
    assert parse_flac_frame_header(bytes(broken)) is None


@pytest.mark.parametrize("data", [FLAC_44, FLAC_48], ids=["44k", "48k"])
def test_flac_split_whole_file(data: bytes) -> None:
    framer = FlacFramer()
    frames = framer.feed(data)
    hlen = flac_header_len(data)
    assert framer.stream_header == data[:hlen]
    joined = b"".join(f.data for f in frames)
    assert data[hlen:].startswith(joined)
    for f in frames:
        assert crc16(f.data[:-2]) == int.from_bytes(f.data[-2:], "big")
    # The last frame is held until the next header arrives.
    held = len(data) - hlen - len(joined)
    assert parse_flac_frame_header(data, hlen + len(joined)) is not None and held > 0
    rate = parse_streaminfo(data[:hlen]).sample_rate
    assert sum(f.duration_s for f in frames) == pytest.approx(len(frames) * 4608 / rate)
    assert framer.skipped_bytes == 0


@pytest.mark.parametrize("sizes", CHUNKINGS, ids=str)
def test_flac_chunk_boundaries_anywhere(sizes: list[int]) -> None:
    ref = FlacFramer()
    expected = ref.feed(FLAC_44)
    framer = FlacFramer()
    assert feed_in_chunks(framer, FLAC_44, sizes) == expected
    assert framer.stream_header == ref.stream_header


def test_flac_resync_after_garbage() -> None:
    hlen = flac_header_len(FLAC_44)
    assert hlen is not None
    reference = FlacFramer().feed(FLAC_44)
    rng = random.Random(2)
    junk = bytes(rng.randrange(256) for _ in range(5000))
    framer = FlacFramer()
    frames = framer.feed(FLAC_44[:hlen] + junk + b"\xff\xf8" + FLAC_44[hlen + 777 :])
    assert framer.skipped_bytes >= 5000
    assert frames[-(len(reference) - 2) :] == reference[2:]


@requires_ffmpeg
def test_flac_mid_stream_join_decodes(tmp_path: Path) -> None:
    framer = FlacFramer()
    frames = framer.feed(FLAC_44)
    assert framer.stream_header is not None
    joined = framer.stream_header + b"".join(f.data for f in frames[3:])
    assert decode_errors(joined, tmp_path, "flac") == ""


def test_flac_header_recapture_after_reset() -> None:
    calls: list[tuple[bool, bool]] = []
    framer = FlacFramer(lambda new, old: calls.append((old is None, new == old)))
    framer.feed(FLAC_44)
    framer.reset()
    framer.feed(FLAC_44[:5000])
    framer.feed(FLAC_44[5000:])
    framer.reset()
    framer.feed(FLAC_48)
    assert calls == [(True, False), (False, True), (False, False)]
    assert framer.info is not None and framer.info.sample_rate == 48000
