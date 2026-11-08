"""Measures framing CPU cost: python scripts/bench_framing.py [seconds_of_audio]

Encodes noisy stereo audio (harder to compress than music, so a pessimistic case) with
ffmpeg, then times the framers over it in 8 KiB chunks, like the encoder delivers them.
"""

import subprocess
import sys
import time

from vinylcast.framing import FlacFramer, Mp3Framer

secs = int(sys.argv[1]) if len(sys.argv) > 1 else 20
src = ["-f", "lavfi", "-i", f"anoisesrc=a=0.3:sample_rate=44100:d={secs}", "-ac", "2"]
for codec, args, framer in (
    (
        "mp3",
        ["-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "0", "-f", "mp3"],
        Mp3Framer(),
    ),
    ("flac", ["-sample_fmt", "s16", "-c:a", "flac", "-f", "flac"], FlacFramer()),
):
    data = subprocess.run(
        ["ffmpeg", "-v", "error", *src, *args, "pipe:1"], capture_output=True, check=True
    ).stdout
    t0 = time.process_time()
    n = 0
    for i in range(0, len(data), 8192):
        n += len(framer.feed(data[i : i + 8192]))
    cpu = time.process_time() - t0
    kbps = len(data) * 8 / secs / 1000
    print(
        f"{codec:4s} {kbps:6.0f} kbps  {n:5d} frames  {cpu:6.3f} s CPU for {secs} s audio"
        f"  = {100 * cpu / secs:5.2f} % of one core"
    )
