#!/bin/sh
# Regenerates the framing test fixtures. Noise keeps FLAC frames large and varied.
set -e
cd "$(dirname "$0")"
SRC="-f lavfi -i sine=frequency=440:sample_rate=44100 -f lavfi -i anoisesrc=a=0.2:seed=7:sample_rate=44100"
MIX="-filter_complex [0][1]amix=inputs=2 -t 1 -ac 2"
# MPEG-1 Layer III, 320k, with the ID3v2 tag ffmpeg writes by default
ffmpeg -v error -y $SRC $MIX -c:a libmp3lame -b:a 320k -f mp3 mp3_44k_320_id3.mp3
# MPEG-2 Layer III (576-sample frames), 22.05 kHz, 64k, no tag, written through a pipe
ffmpeg -v error -y $SRC $MIX -ar 22050 -c:a libmp3lame -b:a 64k -write_xing 0 -id3v2_version 0 -f mp3 pipe:1 > mp3_22k_64.mp3
# FLAC as the encoder emits it on a pipe (header not rewritten at the end)
ffmpeg -v error -y $SRC $MIX -sample_fmt s16 -c:a flac -f flac pipe:1 > flac_44k.flac
ffmpeg -v error -y $SRC $MIX -ar 48000 -sample_fmt s16 -c:a flac -f flac pipe:1 > flac_48k.flac
