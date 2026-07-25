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

# Where the venv keeps its interpreter (POSIX vs Windows layouts).
if [ -x "$VENV/Scripts/python.exe" ]; then
    VPYTHON="$VENV/Scripts/python.exe"
else
    VPYTHON="$VENV/bin/python"
fi

# An existing .venv is not necessarily a working one. Unzipping a fresh copy of
# the project over an old folder leaves the previous .venv behind (it is not in
# the archive), and its interpreter is a symlink to whichever Python built it --
# dead as soon as that Python is upgraded or removed. Activating such a venv
# succeeds, because activation only edits PATH, and the failure surfaces later
# as a baffling "python: command not found". So test it rather than trust it.
if [ -d "$VENV" ] && ! "$VPYTHON" -c 'import sys' 2>/dev/null; then
    echo "==> Existing $VENV is broken (its interpreter no longer runs) — rebuilding"
    rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
    echo "==> Creating virtual environment in $VENV"
    "$PYTHON" -m venv "$VENV"
fi

if [ ! -x "$VPYTHON" ]; then
    echo "Error: $VENV exists but has no usable interpreter at $VPYTHON."
    echo "       Delete it and re-run:  rm -rf $VENV && $0"
    exit 1
fi

# Warn when reusing a venv built by a different Python than the one requested;
# the build would otherwise quietly use the old one.
WANT=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
HAVE=$("$VPYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if [ "$WANT" != "$HAVE" ]; then
    echo "==> Note: $VENV was built with Python $HAVE, but $PYTHON is $WANT."
    echo "    Using $HAVE. Run 'rm -rf $VENV' first if you meant to switch."
fi

echo "==> Installing dependencies (this takes a minute the first time)"
# Called by absolute path rather than relying on PATH after 'activate' -- one
# less thing that can silently resolve to the wrong interpreter.
"$VPYTHON" -m pip install --quiet --upgrade pip
"$VPYTHON" -m pip install --quiet -e ".[gui]"
"$VPYTHON" -m pip install --quiet pyinstaller

# ---------------------------------------------------------------- build

echo "==> Building"
rm -rf build dist
"$VPYTHON" -m PyInstaller --clean --noconfirm packaging/ParkTileArchiver.spec

# ---------------------------------------------------------------- verify
#
# Launch the thing we just built and make sure it can actually start. A bundle
# that dies on import still builds perfectly happily, and finding that out at
# double-click time — with no error message anywhere — is miserable.

if [ -d "dist/Park Tile Archiver.app" ]; then
    BINARY="dist/Park Tile Archiver.app/Contents/MacOS/ParkTileArchiver"
else
    BINARY="dist/ParkTileArchiver/ParkTileArchiver"
fi

echo "==> Checking the built app starts"
if OUTPUT=$("$BINARY" --self-test 2>&1); then
    echo "    $OUTPUT"
else
    echo
    echo "!!! The app was built but will not start. Its output was:"
    echo
    echo "$OUTPUT" | sed 's/^/    /'
    echo
    echo "    Please send that text along — it says exactly what is missing."
    exit 1
fi

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
