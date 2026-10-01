# StuyTown / PCV Affordable Housing Watcher

Checks the real StuyTown/PCV affordable housing listings every 5 minutes
between 7-10am ET and sends an Emergency-priority Pushover notification
(bypasses silent mode / Do Not Disturb) the moment a new unit appears.
Also saves a screenshot and logs the details (unit count, bedrooms,
timestamp) to `events.json`. Runs free on GitHub Actions -- nothing needs
to stay running on your own computer, and it's already live now that the
repo is pushed; there's no separate cron job to set up.

## Setup

1. In your Pushover dashboard, note your **User Key** (shown on the main
   page after login), then go to **Your Applications → Create an
   Application/API Token**, name it anything (e.g. "StuyTown Watcher"),
   and copy the **API Token** it gives you.
2. In this repo on GitHub: **Settings → Secrets and variables → Actions**,
   add two secrets: `PUSHOVER_TOKEN` (the API token) and `PUSHOVER_USER`
   (your user key).
3. That's it -- the schedule in `.github/workflows/check.yml` is already
   active. Go to the **Actions** tab and run the workflow manually once
   (`Run workflow`) to confirm it works right away, rather than waiting
   for the next 7-10am ET window.

## Confirming it's actually running

The **Actions** tab shows every run, automatic or manual, with a
timestamp and pass/fail status. During 7-10am ET you should see a new run
roughly every 5 minutes; outside that window, runs still fire every 5
minutes but each one just logs "Outside the 7-10am ET window" and exits
immediately. Click into any run to see its logs.

## Testing

There are 0 real units listed right now, so test with fake data rather
than waiting for a real one. Two different scripts, for two different
purposes:

**`test_notification.py`** -- fires one real Pushover alert and does
nothing else (no state or log files touched). Safe to run over and over
while you tune your phone: silence it, run this, see what happens, adjust
Pushover's Emergency-priority sound or your phone's Focus/DND exceptions,
repeat.

```bash
PUSHOVER_TOKEN=your-token PUSHOVER_USER=your-user-key python3 test_notification.py
```

**`test_check_units.py`** -- run this once (not repeatedly) to confirm
the full pipeline end to end: fakes a unit and the time window, then runs
the real detection logic, writes a real `events.json` entry, and sends a
real Pushover push. It prints the `events.json` content so you can
eyeball that the bedroom count and timestamp look right.

```bash
PUSHOVER_TOKEN=your-token PUSHOVER_USER=your-user-key python3 test_check_units.py
```

Delete `last_seen.json` and `events.json` afterward so the real deployed
workflow starts from an honest, empty baseline instead of this fake data.

**Screenshot capture** -- no faking needed, it always screenshots
whatever's live right now:

```bash
pip install playwright
playwright install chromium
python screenshot.py
```

## How duplicates and screenshots are handled

- **No duplicate alerts for the same unit.** `last_seen.json` is the
  dedup check: a unit only triggers a notification/screenshot/event-log
  entry the moment it first appears relative to that file, never again on
  later runs while it stays listed. If a unit is later taken down and a
  *different* one (or the same one re-listed later) appears, that's
  treated as new again -- which is the correct behavior, not a bug.
- **One honest edge case:** if the workflow's final `git push` step were
  to fail after a run already updated `last_seen.json` locally, the next
  run would start from the old committed state and could re-detect (and
  re-screenshot) the same unit. Low-probability for a repo only this
  script writes to, and not worth extra complexity to guard against
  unless it actually happens.
- **Screenshots are taken via Playwright** (`screenshot.py`), not a
  keyboard "print screen" -- the screenshot happens inside a GitHub
  Actions run with no physical screen or keyboard to capture from, so it
  has to be done by having a real (headless) browser render the page and
  export its pixels, which is exactly what `page.screenshot()` does.

## Files

- `check_units.py` -- checks listings, sends the alert, logs the event
- `screenshot.py` -- takes the screenshot (only when something's found)
- `test_notification.py` -- repeatable notification-only test, for tuning your phone
- `test_check_units.py` -- one-time full-pipeline test
- `.github/workflows/check.yml` -- runs everything on a schedule
