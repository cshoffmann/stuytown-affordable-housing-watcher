# StuyTown / PCV Affordable Housing Watcher

Checks the real StuyTown/PCV affordable housing listings every 5 minutes
between 7-10am ET and sends a free push notification to your phone the
moment a new unit appears. Also saves a screenshot and logs the details
(unit count, bedrooms, timestamp) to `events.json`. Runs free on GitHub
Actions -- nothing needs to stay running on your own computer.

## Setup

1. Install the [ntfy](https://ntfy.sh) app on your phone and subscribe to
   a topic name only you'd guess, e.g.
2. In this repo: **Settings → Secrets and variables → Actions**, add a
   secret named `NTFY_TOPIC` with that topic name.
3. Push this repo to GitHub, then run the workflow manually once from the
   **Actions** tab to confirm it works.

## Test it first

There are 0 real units right now, so test with fake data before trusting it:

```bash
# Mac/Linux
NTFY_TOPIC=your-topic-name python3 test_check_units.py

# Windows PowerShell
$env:NTFY_TOPIC="your-topic-name"; python test_check_units.py
```

This should trigger a real push to your phone. Delete `last_seen.json` and
`events.json` afterward so the real deployment starts clean.

## Files

- `check_units.py` -- checks listings, sends the alert, logs the event
- `screenshot.py` -- takes the screenshot (only when something's found)
- `test_check_units.py` -- the test above
- `.github/workflows/check.yml` -- runs everything on a schedule
