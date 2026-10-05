# TimeFlip Tracker

TimeFlip Tracker works with your TIMEFLIP2 cube. It:

- sends you a Mac notification when you near or reach a time limit (daily or weekly, per task or per client)
- shows charts of where your time went
- makes timesheets for each client (print/PDF or spreadsheet), with billable totals

## Install (about 5 minutes, no typing commands)

1. On GitHub, switch the branch drop-down to `claude/modest-wozniak-j8obe2`, then click **Code → Download ZIP**. Open the downloaded ZIP to unzip it.
2. In the unzipped folder, **right-click `Install TimeFlip Tracker.command` and choose Open**. If macOS warns that it's from an unidentified developer, click **Open**.
3. If asked, let macOS install Apple's free developer tools, then double-click the installer again.
4. Enter your TimeFlip email and password when asked. Your account needs an email and password; Apple or Google sign-in won't work here.
5. Your browser opens the tracker. There's also a **TimeFlip Tracker** shortcut on your Desktop.

After that, you can delete the downloaded folder.

If notifications don't appear, go to **System Settings → Notifications** and allow **Script Editor**, which is what sends them.

## Using it

- **Limits:** open **Change limits**, pick a task or a client, and enter the hours per day and/or per week.
- **Clients** are the **tags** you give tasks in the TimeFlip app. Give every task for a client the same tag.
- **Charts:** pick a date range and choose **By task** or **By client**.
- **Timesheets:** pick a client and click **Open timesheet** (then press **Print**, and choose **Save as PDF** if you want a PDF) or **Download spreadsheet**.
- **Billable amounts** use the hourly rate and the billable switch you set on each task in the TimeFlip app.

The tracker checks TimeFlip every 5 minutes. It can only see what your phone app has already synced to TimeFlip, so alerts can lag a little.

## Uninstall

Double-click **Uninstall TimeFlip Tracker.command**, which is in the download folder or in `~/Library/Application Support/TimeFlip Tracker`. Your settings and history stay in `~/.tftrack` until you delete that folder.

## For developers

- Python 3.9+ standard library only.
- Data is in `~/.tftrack/`: `config.json`, `tftrack.sqlite`, and the logs.
- The web page runs on `http://127.0.0.1:8765` and only accepts requests from this Mac.
- The background check runs every 5 minutes.
- Both run as launchd agents: `com.tftrack.web` and `com.tftrack.check`.
- Commands (run from `~/Library/Application Support/TimeFlip Tracker`): `python3 -m tftrack status | sync | check | probe | serve`.
- `probe` prints a sample of the raw API data. If durations turn out to be in milliseconds, set `"duration_unit": "milliseconds"` in `config.json`.
- Tests: `python3 -m unittest discover -s tests`
