#!/usr/bin/env bash
# Re-create docs/media/demo.cast, demo.gif and demo.mp4 from the scripted session.
#
# Needs: asciinema 2.x, python3 with `pyte` and `pillow`, ffmpeg, jq, and a checkout where
# `make install` and `make init` have been run. Override the interpreter with PYTHON=...
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$(dirname "$HERE")"
PYTHON="${PYTHON:-python3}"

# 100x30 terminal, same size the GIF is drawn at.
TERM=xterm-256color asciinema rec --overwrite -q --cols 100 --rows 30 \
	--title "tv-broker-bridge: quick start on SimBroker (paper trading)" \
	-c "bash '$HERE/demo-session.sh'" "$OUT/demo.cast"

"$PYTHON" "$HERE/cast_to_gif.py" "$OUT/demo.cast" "$OUT/demo.gif" \
	--title "tv-broker-bridge  |  quick start on SimBroker (paper trading)" --tail-hold 2.5

# H.264 for sites and social posts that do not autoplay GIFs.
ffmpeg -y -loglevel error -i "$OUT/demo.gif" \
	-vf "fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p" \
	-c:v libx264 -preset slow -crf 18 -tune animation -movflags +faststart "$OUT/demo.mp4"

ls -l "$OUT"/demo.*
