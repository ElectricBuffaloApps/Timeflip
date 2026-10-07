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
5. Your browser opens the tracker. To reopen it later, open the **TimeFlip Tracker** app (orange clock icon) from Applications, Launchpad or Spotlight. You can drag it into your Dock.

After that, you can delete the downloaded folder.

If notifications don't appear, go to **System Settings → Notifications** and allow **Script Editor**, which is what sends them.

## Using it

- **Today:** the top of the page shows your total for today, time per task and today's entries.
- **Tasks not counted:** in Settings, tick sides like breaks, timers or "off" to leave them out of every figure. Today still shows how much time wasn't counted.
- **Limits:** open **Change limits**, pick a task or a client, and enter the hours per day and/or per week.
- **Clients** are the **tags** you give tasks in the TimeFlip app. Give every task for a client the same tag.
- **Charts:** pick a date range and choose **By task** or **By client**.
- **Timesheets:** pick a client and a month, then click **Open timesheet** (press **Print**, then **Save as PDF** for a PDF) or **Download spreadsheet**.
- **How each client is invoiced** (Settings):
  - **By the day:** each day's entries, with a total under each day.
  - **By the week:** every week whose Monday is in the month, with day and week totals. September's last week can run into October.
  - **Rounding:** day totals can be rounded to a block of minutes, such as 15.
- **Invoice lines:** each monthly timesheet starts with one line per day (by-the-day clients) or per week (by-the-week clients), with **Copy** buttons for pasting into a Revolut Business invoice or any other invoicing app. Set an hourly rate per client in Settings to show amounts. The panel doesn't print.
- **Fixing entries:** click **Edit** beside an entry on a timesheet to change its date, times or task (which can move it to another client). It's marked "adjusted" with the original time, also on the printed sheet. **Back to original** undoes it. Edits stay on your Mac and survive syncs; TimeFlip's own data isn't changed.
- **Adding time you didn't track:** **+ Add entry** at the top of a timesheet. Added entries are marked "added" and can be edited or deleted.
- **Not charging for something:** tick **Exclude** beside the entry on the timesheet. It stays on the sheet crossed out, isn't counted in the totals, and stays excluded after future syncs.
- **Money:** Today and "Where your time went" price time with the hourly rates in Settings (same rounding as invoices; excluded entries left out). Tick **My own business** for clients like FYPT1/FYPTD to give them an imagined rate: shown separately as own-business value, never on timesheets.
- **This month:** hours, money and per-hour rate for each client. Set a client to **Monthly income** (Settings) for work paid by the month, such as coaching; enter the month's income and it shows your effective hourly rate (income ÷ hours). The amount carries forward until you change it.
- **Targets:** income targets per day/week/month (real money only) and time targets per client, set in Settings, shown in the Targets section. Monthly-income clients count as an estimate from last month until the month's actual income is entered.
- **Billable amounts** on the plain (hourly) timesheet use the hourly rate and the billable switch you set on each task in the TimeFlip app.

The tracker checks TimeFlip every 5 minutes. It can only see what your phone app has already synced to TimeFlip, so alerts can lag a little.

## Updating

Updates install automatically once a day: overnight if your Mac is on, otherwise shortly after it wakes. The next time you open the tracker, a **What's new** message lists the changes.

To update straight away, click **Update now** on the banner at the top of the page, or **Check for updates** in Settings. To stop automatic updates, untick **Install updates automatically overnight** in Settings.

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
- **Releasing:** with every change pushed to the branch, increase the number in `tftrack/VERSION` and add a matching entry to `tftrack/CHANGELOG.json`. Installed copies compare the version with GitHub, update overnight from the background check, and show the changelog once.
