#!/bin/bash
# Double-click to install (or reinstall) TimeFlip Tracker.
set -u
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/TimeFlip Tracker"
TITLE="TimeFlip Tracker"

say_box() {  # say_box "message" [stop|note]
  osascript -e "display dialog \"$1\" with title \"$TITLE\" buttons {\"OK\"} default button \"OK\" with icon ${2:-note}" >/dev/null
}
ask() {  # ask "prompt" [hidden]
  local extra=""
  [ "${2:-}" = hidden ] && extra="with hidden answer"
  osascript -e "text returned of (display dialog \"$1\" default answer \"\" $extra with title \"$TITLE\")" 2>/dev/null
}

# Python comes with Apple's free developer tools.
if ! xcode-select -p >/dev/null 2>&1; then
  say_box "First, macOS needs Apple's free developer tools (they include Python). Click Install in the next window, wait for it to finish, then double-click this installer again."
  xcode-select --install
  exit 0
fi
PY="$(/usr/bin/python3 -c 'import sys; print(sys.executable)' 2>/dev/null)"
if [ -z "$PY" ]; then
  say_box "Python isn't ready yet. If Apple's developer tools are still installing, wait for them to finish, then run this installer again." stop
  exit 1
fi

# Copy the app somewhere permanent, so the download can be deleted.
mkdir -p "$DEST"
rm -rf "$DEST/tftrack"
cp -R "$SRC/tftrack" "$DEST/"
cp "$SRC/Uninstall TimeFlip Tracker.command" "$DEST/" 2>/dev/null

while true; do
  EMAIL="$(ask "Your TimeFlip email address:")" || exit 0
  PASSWORD="$(ask "Your TimeFlip password:" hidden)" || exit 0
  if OUT="$(cd "$DEST" && printf '%s\n' "$PASSWORD" | "$PY" -m tftrack install --email "$EMAIL" 2>&1)"; then
    break
  fi
  MSG="$(printf '%s' "$OUT" | tail -n 1 | tr -d '"\\')"
  osascript -e "display dialog \"That didn't work: $MSG\" with title \"$TITLE\" buttons {\"Cancel\", \"Try again\"} default button \"Try again\" with icon caution" >/dev/null 2>&1 || exit 1
done
unset PASSWORD

sleep 2
open "http://127.0.0.1:8765/"
say_box "All set. Your tracker is open in your browser, and there's a 'TimeFlip Tracker' shortcut on your Desktop. Notifications will appear when you get close to a limit. You can close this window."
