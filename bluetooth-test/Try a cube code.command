#!/bin/bash
# Double-click to try codes you type as the cube's password.
# Nothing is saved. It changes nothing on the cube.
set -u
cd "$(dirname "$0")" || exit 1
VENV="$HOME/.tftrack/bluetooth-test-venv"

ask() {  # ask "prompt" -> prints hidden answer, empty if cancelled
  osascript -e "text returned of (display dialog \"$1\" default answer \"\" with hidden answer with title \"FlipTracker\")" 2>/dev/null
}

if ! /usr/bin/python3 -c 'import sys' >/dev/null 2>&1; then
  echo "Python isn't installed. Run the FlipTracker installer first, then try again."
  exit 1
fi

echo "Setting up (first run takes a minute)..."
if [ ! -x "$VENV/bin/python" ]; then
  mkdir -p "$HOME/.tftrack"
  /usr/bin/python3 -m venv "$VENV" || { echo "Could not set up Python."; exit 1; }
fi
"$VENV/bin/python" -m pip install --quiet --disable-pip-version-check bleak >/dev/null 2>&1 || {
  echo "Could not download the Bluetooth library. Check your internet connection and try again."; exit 1; }

CODE1="$(ask 'First code to try (the cube code, not your TimeFlip login):')"
CODE2="$(ask 'Second code to try (leave blank if you only have one):')"

if [ -z "$CODE1" ] && [ -z "$CODE2" ]; then
  echo "No codes entered. Nothing to do."
  exit 0
fi

echo
echo "Keep your phone's Bluetooth OFF and flip the cube when it starts looking."
echo "If macOS asks whether Terminal may use Bluetooth, click Allow."
echo
ARGS=()
[ -n "$CODE1" ] && ARGS+=("$CODE1")
[ -n "$CODE2" ] && ARGS+=("$CODE2")
"$VENV/bin/python" try_codes.py "${ARGS[@]}"
unset CODE1 CODE2 ARGS
echo
echo "Done. You can close this window."
