#!/usr/bin/env bash
#
# Builds the Park Tile Archiver desktop app.
#
#   ./build-app.sh            build it
#   ./build-app.sh --run      build it, then open it
#
# On macOS you get "dist/Park Tile Archiver.app". On Linux you get
# dist/ParkTileArchiver/. For Windows, run the same steps from PowerShell --
# see the "On Windows" section of GUI.md.

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
PYTHON="${PYTHON:-python3}"

# ---------------------------------------------------------------- python

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    echo "Error: Python 3.11 or newer is required."
    echo
    echo "  Found:  $("$PYTHON" --version 2>&1 || echo 'no python3 at all')"
    echo
    echo "  macOS ships 3.9, which is too old. Install a newer one with:"
    echo "      brew install python@3.12"
    echo "  then re-run this script as:"
    echo "      PYTHON=python3.12 ./build-app.sh"
    exit 1
fi

echo "==> Using $("$PYTHON" --version)"

if [ ! -d "$VENV" ]; then
    echo "==> Creating virtual environment in $VENV"
    "$PYTHON" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate" 2>/dev/null || source "$VENV/Scripts/activate"

echo "==> Installing dependencies (this takes a minute the first time)"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -e ".[gui]"
python -m pip install --quiet pyinstaller

# ---------------------------------------------------------------- build

echo "==> Building"
rm -rf build dist
pyinstaller --clean --noconfirm packaging/ParkTileArchiver.spec

# ---------------------------------------------------------------- report

if [ -d "dist/Park Tile Archiver.app" ]; then
    APP="dist/Park Tile Archiver.app"
    # Gatekeeper flags anything downloaded or built without a signing identity.
    # Clearing the quarantine flag here avoids the "damaged and can't be
    # opened" dialog on first launch.
    xattr -cr "$APP" 2>/dev/null || true
    SIZE=$(du -sh "$APP" | cut -f1)
    echo
    echo "==> Built $APP  ($SIZE)"
    echo
    echo "    Open it with:      open '$APP'"
    echo "    Or drag it to your Applications folder and double-click."
    echo
    echo "    It is not code-signed, so the very first launch may need"
    echo "    right-click -> Open rather than a double-click."
    if [ "${1:-}" = "--run" ]; then
        open "$APP"
    fi
else
    echo
    echo "==> Built dist/ParkTileArchiver/"
    echo "    Run it with: ./dist/ParkTileArchiver/ParkTileArchiver"
    if [ "${1:-}" = "--run" ]; then
        ./dist/ParkTileArchiver/ParkTileArchiver
    fi
fi
