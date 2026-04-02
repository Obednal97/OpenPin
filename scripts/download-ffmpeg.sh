#!/bin/bash
# Download a static FFmpeg binary for macOS (bundled into the Electron app).
# Run this before `npm run build` or `npm run publish`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BIN_DIR="$PROJECT_ROOT/bin"
FFMPEG="$BIN_DIR/ffmpeg"

if [ -f "$FFMPEG" ]; then
  echo "FFmpeg already exists at $FFMPEG"
  "$FFMPEG" -version 2>/dev/null | head -1
  exit 0
fi

echo "Downloading static FFmpeg for macOS..."
mkdir -p "$BIN_DIR"

TMP_ZIP="$(mktemp /tmp/ffmpeg-XXXXXX.zip)"
curl -L "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip" -o "$TMP_ZIP"
unzip -o "$TMP_ZIP" -d "$BIN_DIR"
rm "$TMP_ZIP"

chmod +x "$FFMPEG"
echo "Downloaded FFmpeg:"
"$FFMPEG" -version | head -1
echo "Installed to $FFMPEG"
