#!/bin/bash
# Double-click to look for the cube's password in your TimeFlip cloud data,
# then read the cube's history with it.
# It changes nothing on the cube and nothing in your TimeFlip account.
set -u
cd "$(dirname "$0")" || exit 1
VENV="$HOME/.tftrack/bluetooth-test-venv"

if ! /usr/bin/python3 -c 'import sys' >/dev/null 2>&1; then
  echo "Python isn't installed. Run the FlipTracker installer first, then try again."
  exit 1
fi

echo "Setting up (first run takes a minute)..."
if [ ! -x "$VENV/bin/python" ]; then
  mkdir -p "$HOME/.tftrack"
  /usr/bin/python3 -m venv "$VENV" || { echo "Could not set up Python."; exit 1; }
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip >/dev/null 2>&1
if ! "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check bleak; then
  echo "Could not download the Bluetooth library. Check your internet connection and try again."
  exit 1
fi

echo
echo "If macOS asks whether Terminal may use Bluetooth, click Allow."
echo
"$VENV/bin/python" find_password.py
echo
echo "Done. You can close this window."
