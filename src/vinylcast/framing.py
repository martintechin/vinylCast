"""MP3 / FLAC frame-boundary detection and FLAC stream-header capture.

The parsing functions are pure functions over bytes. `Mp3Framer` and `FlacFramer` are
thin stateful wrappers that keep the unconsumed tail between encoder chunks, so a chunk
boundary may fall anywhere (inside a header, a frame or the FLAC metadata).

Python only inspects headers here; no audio is decoded.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple


class Frame(NamedTuple):
    data: bytes
    duration_s: float


# --------------------------------------------------------------------------- MP3

_MP3_BITRATES_KBPS = {
    # (version is MPEG-1, layer) -> table indexed by the 4-bit bitrate index (0 = free, 15 = bad)
    (True, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (True, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (True, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (False, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (False, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    (False, 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MP3_SAMPLE_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


@dataclass(frozen=True)
class Mp3Header:
    version: int  # 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5 (raw 2-bit field)
    layer: int  # 1, 2 or 3
    bitrate_kbps: int
    sample_rate: int
    padding: int
    frame_len: int
    samples: int

    @property
    def duration_s(self) -> float:
        return self.samples / self.sample_rate


def parse_mp3_header(b: bytes | memoryview, pos: int = 0) -> Mp3Header | None:
    """Parse the 4-byte MP3 frame header at `pos`. None unless sync word *and* every field
    is valid (free-format bitrate is rejected: its frame length cannot be derived)."""
    if len(b) - pos < 4:
        return None
    b0, b1, b2, b3 = b[pos], b[pos + 1], b[pos + 2], b[pos + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version = (b1 >> 3) & 0x3
    layer_bits = (b1 >> 1) & 0x3
    bitrate_idx = b2 >> 4
    rate_idx = (b2 >> 2) & 0x3
    if version == 1 or layer_bits == 0 or bitrate_idx in (0, 15) or rate_idx == 3:
        return None
    if (b3 & 0x3) == 2:  # reserved emphasis
        return None
    layer = 4 - layer_bits
    mpeg1 = version == 3
    bitrate = _MP3_BITRATES_KBPS[(mpeg1, layer)][bitrate_idx]
    rate = _MP3_SAMPLE_RATES[version][rate_idx]
    padding = (b2 >> 1) & 0x1
    if layer == 1:
        frame_len = (12 * bitrate * 1000 // rate + padding) * 4
        samples = 384
    elif layer == 2 or mpeg1:
        frame_len = 144 * bitrate * 1000 // rate + padding
        samples = 1152
    else:  # MPEG-2/2.5 Layer III
        frame_len = 72 * bitrate * 1000 // rate + padding
        samples = 576
    return Mp3Header(version, layer, bitrate, rate, padding, frame_len, samples)


def id3v2_size(b: bytes | memoryview) -> int | None:
    """Total size of an ID3v2 tag at the start of `b`, 0 if there is none, None if more
    bytes are needed to decide."""
    if len(b) < 10:
        return None if bytes(b[:3]) == b"ID3"[: len(b)] else 0
    if bytes(b[:3]) != b"ID3":
        return 0
    size = (b[6] & 0x7F) << 21 | (b[7] & 0x7F) << 14 | (b[8] & 0x7F) << 7 | (b[9] & 0x7F)
    footer = 10 if b[5] & 0x10 else 0
    return 10 + size + footer


def split_mp3(buf: bytes, synced: bool) -> tuple[list[Frame], int, bool, int]:
    """Split `buf` into whole MP3 frames.

    Returns (frames, consumed, synced, skipped). Unconsumed bytes must be passed again with
    more data appended. While not synced, a candidate header is only accepted when the
    header at the end of its frame is valid as well; once synced, each next header is
    checked and a bad one drops sync again.
    """
    frames: list[Frame] = []
    pos = skipped = 0
    n = len(buf)
    while pos < n:
        if pos == 0 or not synced:
            tag = id3v2_size(memoryview(buf)[pos:])
            if tag is None:
                break
            if tag:
                if n - pos < tag:
                    break
                pos += tag
                skipped += tag
                synced = False
                continue
        hdr = parse_mp3_header(buf, pos)
        if hdr is None:
            if n - pos < 4:
                break
            synced = False
            pos += 1
            skipped += 1
            continue
        end = pos + hdr.frame_len
        if not synced:
            if n < end + 4:
                break  # need the next header to confirm
            if parse_mp3_header(buf, end) is None:
                pos += 1
                skipped += 1
                continue
            synced = True
        if n < end:
            break
        frames.append(Frame(buf[pos:end], hdr.duration_s))
        pos = end
    return frames, pos, synced, skipped


# -------------------------------------------------------------------------- FLAC

FLAC_MARKER = b"fLaC"


def _crc8_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        table.append(c)
    return table


def _crc16_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x8005) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
        table.append(c)
    return table


_CRC8 = _crc8_table()
_CRC16 = _crc16_table()


def crc8(data: bytes | memoryview) -> int:
    """FLAC frame-header CRC-8 (polynomial 0x07, init 0)."""
    c = 0
    for byte in data:
        c = _CRC8[c ^ byte]
    return c


def crc16(data: bytes | memoryview) -> int:
    """FLAC frame CRC-16 (polynomial 0x8005, init 0)."""
    c = 0
    for byte in data:
        c = ((c << 8) & 0xFFFF) ^ _CRC16[(c >> 8) ^ byte]
    return c


@dataclass(frozen=True)
class FlacStreamInfo:
    sample_rate: int
    channels: int
    bits_per_sample: int


def flac_header_len(buf: bytes) -> int | None:
    """Length of the FLAC stream header (marker + all metadata blocks up to and including
    the one flagged last), or None if `buf` does not hold all of it yet.
    Raises ValueError if `buf` does not start with the fLaC marker."""
    if len(buf) < 4:
        if not FLAC_MARKER.startswith(buf):
            raise ValueError("not a FLAC stream")
        return None
    if buf[:4] != FLAC_MARKER:
        raise ValueError("not a FLAC stream")
    pos = 4
    while True:
        if len(buf) < pos + 4:
            return None
        flags = buf[pos]
        length = int.from_bytes(buf[pos + 1 : pos + 4], "big")
        pos += 4 + length
        if flags & 0x80:
            return pos if len(buf) >= pos else None


def parse_streaminfo(header: bytes) -> FlacStreamInfo:
    """STREAMINFO is always the first metadata block, directly after the marker."""
    if len(header) < 4 + 4 + 34 or header[4] & 0x7F != 0:
        raise ValueError("missing STREAMINFO")
    d = header[8:42]
    rate = (d[10] << 12) | (d[11] << 4) | (d[12] >> 4)
    channels = ((d[12] >> 1) & 0x7) + 1
    bps = (((d[12] & 0x1) << 4) | (d[13] >> 4)) + 1
    return FlacStreamInfo(rate, channels, bps)


_FLAC_RATES = {
    1: 88200, 2: 176400, 3: 192000, 4: 8000, 5: 16000, 6: 22050,
    7: 24000, 8: 32000, 9: 44100, 10: 48000, 11: 96000,
}  # fmt: skip


@dataclass(frozen=True)
class FlacFrameHeader:
    block_size: int
    sample_rate: int  # 0 = take it from STREAMINFO
    header_len: int


def parse_flac_frame_header(b: bytes | memoryview, pos: int = 0) -> FlacFrameHeader | None:
    """Parse a FLAC frame header at `pos`. None unless the sync code, every field and the
    header CRC-8 are valid, or if the header is not complete in `b`."""
    n = len(b)
    if n - pos < 6:
        return None
    if b[pos] != 0xFF or (b[pos + 1] & 0xFE) != 0xF8:
        return None
    bs_code = b[pos + 2] >> 4
    sr_code = b[pos + 2] & 0x0F
    chan = b[pos + 3] >> 4
    size_code = (b[pos + 3] >> 1) & 0x7
    if bs_code == 0 or sr_code == 15 or chan > 10 or size_code == 3 or b[pos + 3] & 0x1:
        return None
    # UTF-8-style coded frame/sample number (1-7 bytes).
    i = pos + 4
    first = b[i]
    if first < 0x80:
        extra = 0
    elif first >= 0xC0 and first != 0xFF:
        extra = 0
        mask = 0x40
        while first & mask:
            extra += 1
            mask >>= 1
        if extra > 6:
            return None
    else:
        return None
    if n < i + 1 + extra:
        return None
    for k in range(1, extra + 1):
        if b[i + k] & 0xC0 != 0x80:
            return None
    i += 1 + extra
    if bs_code == 6:
        if n < i + 1:
            return None
        block_size = b[i] + 1
        i += 1
    elif bs_code == 7:
        if n < i + 2:
            return None
        block_size = (b[i] << 8 | b[i + 1]) + 1
        i += 2
    elif bs_code == 1:
        block_size = 192
    elif bs_code <= 5:
        block_size = 576 << (bs_code - 2)
    else:
        block_size = 256 << (bs_code - 8)
    if sr_code == 0:
        rate = 0
    elif sr_code <= 11:
        rate = _FLAC_RATES[sr_code]
    elif sr_code == 12:
        if n < i + 1:
            return None
        rate = b[i] * 1000
        i += 1
    else:
        if n < i + 2:
            return None
        v = b[i] << 8 | b[i + 1]
        rate = v if sr_code == 13 else v * 10
        i += 2
    if n < i + 1 or crc8(b[pos:i]) != b[i]:
        return None
    return FlacFrameHeader(block_size, rate, i + 1 - pos)


def split_flac(buf: bytes, synced: bool, stream_rate: int) -> tuple[list[Frame], int, bool, int]:
    """Split FLAC frame data (after the stream header) into whole frames.

    A frame has no length field: it ends where the next frame starts. A boundary is a valid
    frame header (sync + fields + CRC-8) at which the preceding bytes also pass the frame's
    CRC-16, so audio data that happens to look like a header is not mistaken for one.
    The last frame in `buf` is held back until the next header arrives.
    Returns (frames, consumed, synced, skipped).
    """
    frames: list[Frame] = []
    pos = skipped = 0
    n = len(buf)
    while pos < n:
        hdr = parse_flac_frame_header(buf, pos)
        if hdr is None:
            if n - pos < 24:  # could be an incomplete header; wait
                break
            synced = False
            pos += 1
            skipped += 1
            continue
        # Look for the next boundary.
        nxt = pos + hdr.header_len + 2
        found = None
        while True:
            nxt = buf.find(b"\xff", nxt)
            if nxt < 0 or n - nxt < 24:
                break
            if (buf[nxt + 1] & 0xFE) == 0xF8 and parse_flac_frame_header(buf, nxt) is not None:
                body = memoryview(buf)[pos:nxt]
                if crc16(body[:-2]) == int.from_bytes(body[-2:], "big"):
                    found = nxt
                    break
            nxt += 1
        if found is None:
            break
        rate = hdr.sample_rate or stream_rate
        frames.append(Frame(buf[pos:found], hdr.block_size / rate))
        synced = True
        pos = found
    return frames, pos, synced, skipped


# ------------------------------------------------------------------------ framers


class Mp3Framer:
    """Stateful wrapper around `split_mp3`. Call `reset()` when the encoder restarts."""

    stream_header: bytes | None = None  # MP3 has none

    def __init__(self) -> None:
        self._buf = b""
        self._synced = False
        self.skipped_bytes = 0

    def reset(self) -> None:
        self._buf = b""
        self._synced = False

    def feed(self, data: bytes) -> list[Frame]:
        buf = self._buf + data
        frames, consumed, self._synced, skipped = split_mp3(buf, self._synced)
        self.skipped_bytes += skipped
        self._buf = buf[consumed:]
        return frames


HeaderCallback = Callable[[bytes, bytes | None], None]


class FlacFramer:
    """Stateful wrapper: captures the stream header, then splits frames.

    After `reset()` (encoder restart) the header is captured again; `on_header(new, old)`
    is called each time, with `old` = the previously cached header (None the first time),
    so the caller can disconnect clients if it changed.
    """

    def __init__(self, on_header: HeaderCallback | None = None) -> None:
        self._on_header = on_header
        self.stream_header: bytes | None = None
        self.info: FlacStreamInfo | None = None
        self._buf = b""
        self._in_header = True
        self._synced = False
        self.skipped_bytes = 0

    def reset(self) -> None:
        self._buf = b""
        self._in_header = True
        self._synced = False

    def feed(self, data: bytes) -> list[Frame]:
        buf = self._buf + data
        if self._in_header:
            hlen = flac_header_len(buf)
            if hlen is None:
                self._buf = buf
                return []
            header = buf[:hlen]
            old = self.stream_header
            self.stream_header = header
            self.info = parse_streaminfo(header)
            self._in_header = False
            buf = buf[hlen:]
            if self._on_header is not None:
                self._on_header(header, old)
        assert self.info is not None
        frames, consumed, self._synced, skipped = split_flac(
            buf, self._synced, self.info.sample_rate
        )
        self.skipped_bytes += skipped
        self._buf = buf[consumed:]
        return frames


def make_framer(codec: str, on_header: HeaderCallback | None = None) -> Mp3Framer | FlacFramer:
    if codec == "mp3":
        return Mp3Framer()
    if codec == "flac":
        return FlacFramer(on_header)
    raise ValueError(f"unknown codec {codec!r}")
