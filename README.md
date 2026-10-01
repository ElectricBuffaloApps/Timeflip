# TimeFlip tracker

Pulls your TIMEFLIP2 data from the TimeFlip cloud API, checks it against daily and weekly limits, and sends a Mac notification when you near or reach a limit.

It runs on the Mac's built-in Python 3 and uses only the standard library, so there's nothing to install.

## How it works

- The TimeFlip phone app syncs the cube to the TimeFlip cloud.
- Every 5 minutes this tool downloads your tasks and time intervals (`GET /api/sync/all`) into a local SQLite database at `~/.tftrack/`.
- It totals today and this week per task, and per client.
- A **client** is the TimeFlip task **tag**: give every task for a client the same tag in the TimeFlip app.
- Alerts fire once per limit per day or week, at each level in `alert_levels_percent` (80% and 100% by default).

Alerts are only as up to date as the phone app's last sync to the cloud.

## Setup

1. Your TimeFlip account needs an email and password. Apple or Google sign-in won't work with the API.
2. Create the config file:
   ```sh
   cd /path/to/Timeflip
   python3 -m tftrack setup
   ```
3. Edit `~/.tftrack/config.json`: set your email and your limits (see below).
4. Store your password in the Keychain (it prompts for it) and test the sign-in:
   ```sh
   python3 -m tftrack setup
   ```
5. Check the data looks right. In the probe output, `duration` should be in seconds. If the numbers look about 1000 times too big, set `"duration_unit": "milliseconds"` in the config.
   ```sh
   python3 -m tftrack probe
   python3 -m tftrack status
   ```
6. Start the background check:
   ```sh
   python3 -m tftrack install-agent      # add --minutes 10 to change how often it runs
   ```
   macOS may ask once whether to allow Keychain access. Choose **Always Allow**.

## Limits

```json
{
  "email": "you@example.com",
  "week_starts": "monday",
  "alert_levels_percent": [80, 100],
  "duration_unit": "seconds",
  "limits": [
    {"task": "Admin", "daily_hours": 2},
    {"task": "Email", "daily_hours": 1, "weekly_hours": 4},
    {"client": "Acme Ltd", "weekly_hours": 10, "label": "Acme"}
  ]
}
```

- Each limit sets either `task` (the TimeFlip task name) or `client` (the task tag).
- Each limit needs `daily_hours`, `weekly_hours`, or both.
- Matching ignores upper and lower case.
- `status` warns if a name or tag doesn't match anything in TimeFlip.
- Changing a limit's hours resets its alerts for the current period.

## Commands

| Command | What it does |
|---|---|
| `setup` | Creates the config and stores your password |
| `probe` | Shows a sample of the raw API data |
| `sync` | Downloads your tasks and intervals |
| `status [--offline]` | Shows your progress against each limit |
| `check` | Syncs, then sends any notifications that are due. This is what the background agent runs |
| `install-agent` / `uninstall-agent` | Starts or stops the background check |

The log is at `~/.tftrack/check.log`.

## Tests

```sh
python3 -m unittest discover -s tests
```

## Not built yet

- Reports, charts and per-client timesheets (CSV/PDF) with billable totals.
