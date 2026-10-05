#!/bin/bash
# Double-click to remove TimeFlip Tracker. Your settings and history in ~/.tftrack are kept.
DEST="$HOME/Library/Application Support/TimeFlip Tracker"
osascript -e 'display dialog "Remove TimeFlip Tracker from this Mac?" with title "TimeFlip Tracker" buttons {"Cancel", "Remove"} default button "Cancel" with icon caution' >/dev/null 2>&1 || exit 0
if [ -d "$DEST/tftrack" ]; then
  (cd "$DEST" && /usr/bin/python3 -m tftrack uninstall)
fi
rm -rf "$DEST"
osascript -e 'display dialog "TimeFlip Tracker has been removed." with title "TimeFlip Tracker" buttons {"OK"} default button "OK"' >/dev/null
