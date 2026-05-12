#!/bin/bash
# Prepare frontend assets for Tauri build — copies only what's needed,
# excluding node_modules, Python backend, and dev files.
set -euo pipefail

DIST="$(dirname "$0")/../dist-tauri"
APP="$(dirname "$0")/../app"

rm -rf "$DIST"
mkdir -p "$DIST"

# Copy frontend files
cp "$APP"/index.html "$DIST/"
cp "$APP"/renderer.js "$DIST/"
cp "$APP"/tauri-bridge.js "$DIST/"
cp "$APP"/styles.css "$DIST/"

# UI modules
mkdir -p "$DIST/ui"
cp "$APP"/ui/*.js "$DIST/ui/"

# CSS
mkdir -p "$DIST/css"
cp "$APP"/css/*.css "$DIST/css/"

# Assets (icons, wordmarks)
mkdir -p "$DIST/assets"
cp "$APP"/assets/*.png "$DIST/assets/"
cp "$APP"/assets/*.svg "$DIST/assets/" 2>/dev/null || true

# Root-level SVG icons (e.g. notion-icon.svg)
cp "$APP"/*.svg "$DIST/" 2>/dev/null || true

# App icons
cp "$APP"/icon*.png "$DIST/" 2>/dev/null || true
cp "$APP"/icon.icns "$DIST/" 2>/dev/null || true
cp "$APP"/trayTemplate*.png "$DIST/" 2>/dev/null || true

# Python backend files (needed by the sidecar)
cp "$APP"/api_server.py "$DIST/"
cp -r "$APP"/routes "$DIST/routes"
cp -r "$APP"/state "$DIST/state"
cp -r "$APP"/providers "$DIST/providers"
cp -r "$APP"/utils "$DIST/utils"

echo "Frontend prepared in $DIST"
