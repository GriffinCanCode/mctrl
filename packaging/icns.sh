#!/usr/bin/env bash
# Render one icon concept all the way to a .icns.
#
#   PYTHON=/path/to/python3 ICON=gaze packaging/icns.sh build/MindControl.app/…/MindControl.icns
#
# PYTHON needs pyobjc, which every bundle already has; ICON defaults to the one
# the app ships with.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:?usage: icns.sh <output.icns>}"
ICON="${ICON:-gaze}"
PYTHON="${PYTHON:-python3}"

"$PYTHON" "$ROOT/packaging/icon.py" "$ICON" >/dev/null

ICONSET="$ROOT/build/icons/MindControl.iconset"
rm -rf "$ICONSET"; mkdir -p "$ICONSET"
# Every slot iconutil knows about. Each is named for the point size it serves, so
# three pixel sizes are asked for twice: 32pt @1x and 16pt @2x are one image.
while read -r name pixels; do
    sips -z "$pixels" "$pixels" "$ROOT/build/icons/$ICON.png" --out "$ICONSET/$name.png" >/dev/null
done <<'SIZES'
icon_16x16 16
icon_16x16@2x 32
icon_32x32 32
icon_32x32@2x 64
icon_128x128 128
icon_128x128@2x 256
icon_256x256 256
icon_256x256@2x 512
icon_512x512 512
icon_512x512@2x 1024
SIZES
iconutil -c icns "$ICONSET" -o "$OUT"
