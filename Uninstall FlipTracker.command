#!/bin/bash
# Double-click to remove FlipTracker. Your settings and history in ~/.tftrack are kept.
DEST="$HOME/Library/Application Support/TimeFlip Tracker"
osascript -e 'display dialog "Remove FlipTracker from this Mac?" with title "FlipTracker" buttons {"Cancel", "Remove"} default button "Cancel" with icon caution' >/dev/null 2>&1 || exit 0
if [ -d "$DEST/tftrack" ]; then
  (cd "$DEST" && /usr/bin/python3 -m tftrack uninstall)
fi
rm -rf "$DEST"
osascript -e 'display dialog "FlipTracker has been removed." with title "FlipTracker" buttons {"OK"} default button "OK"' >/dev/null
