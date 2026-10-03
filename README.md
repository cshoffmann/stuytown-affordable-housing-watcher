# StuyTown / PCV Affordable Housing Watcher

Checks the real StuyTown/PCV affordable housing listings every 15 seconds
from 7:00 to 10:00am ET, every day, and sends an Emergency-priority Pushover
alert (bypasses silent mode / Do Not Disturb) the moment a new unit appears —
with a link straight to that unit's page so you can apply. Each unit closes
after 3 applications, so speed matters. It also logs the unit's details to
`data/events.json` and saves a screenshot of the listings page. Runs free on
GitHub Actions; nothing has to stay running on your computer.

## How alerts work (no duplicates)

`data/last_seen.json` remembers every unit currently listed, with the exact
data the API last returned for it. On every check:

| What the API shows | What happens |
| --- | --- |
| A unit that wasn't there before | **Emergency alert** with an "Open this unit to apply" link, an `events.json` entry with the unit's full metadata, and a screenshot |
| The same unit, same data | **Nothing.** This is what stops a listing from alerting every 15 seconds |
| The same unit, changed data (rent, available date, income requirement…) | One normal-priority "listing updated" alert, plus an `events.json` entry |
| A unit gone for ~1 minute (4 checks in a row) | One quiet alert (no sound), plus an `events.json` entry; the unit leaves the state, so if it's **re-listed later it alerts as new again** |
| A unit missing from just one response, then back | Nothing — treated as an API blip, so it can't re-alert you |

If sending the new-unit alert fails (e.g. Pushover is briefly unreachable),
the state isn't updated, so the next check 15 seconds later tries again — a
hiccup can delay an alert but never lose it. There's no silent "baseline"
run: if units are already listed the first time it runs, you're alerted.

The state is committed back to the repo whenever it changes and at the end of
each morning, so the next day picks up exactly where the last one left off.

## The schedule (`.github/workflows/watch.yml`)

- Triggers every day at **6:13am New York time** (daylight saving handled by
  GitHub's `timezone` setting). The job sets up, waits for 7:00, checks every
  15 seconds until 10:00, then exits. You'll see one ~4-hour run per day.
- **Backup triggers** at 6:43, 7:13 … 9:43. GitHub's scheduler can start runs
  late or skip them (in this repo's first days, one run arrived ~4 hours
  late), so if the 6:13 run never shows up, the next one that fires takes over
  for whatever is left of the window. While a watcher is running, backups wait
  in line and are replaced by newer ones — **"cancelled" runs in the Actions
  tab are normal** — and the last one exits within seconds after 10:00.
- If the Pushover secrets are missing or wrong, the run fails immediately with
  a red X (and GitHub emails you) instead of silently watching with no way to
  reach your phone.
- GitHub turns off schedules in public repos after 60 days without a commit;
  if nothing has been committed for 45 days, the watcher commits a tiny
  `data/heartbeat.json` to prevent that.

To watch it live: Actions tab → today's **StuyTown watcher** run → expand
"Watch for new units". Each check logs a line like
`[07:55:00 ET] 1 listed - NEW: Apt 5A, 287 Avenue C`.

## Setup

1. In your Pushover dashboard, note your **User Key**, then **Your
   Applications → Create an Application/API Token** and copy the **API Token**.
2. In this repo on GitHub: **Settings → Secrets and variables → Actions**, add
   `PUSHOVER_TOKEN` (the API token) and `PUSHOVER_USER` (your user key).
3. In the Pushover app, pick a sound for Emergency-priority alerts, and allow
   Pushover through your phone's Focus / Do Not Disturb settings.

## Testing

All tests live in `tests/`; fake data in `tests/fixtures/`. Nothing in
`tests/` touches the real `data/` or `screenshots/` folders or git —
simulation output goes to `tests/output/` (gitignored, wiped each run).

One-time setup on your computer (PowerShell, from the repo folder):

```powershell
pip install -r requirements.txt
playwright install chromium
```

**1. Automated checks** — the duplicate/state logic, alert content and links,
the schedule. No network, no phone:

```powershell
python -m unittest discover -s tests -v
```

**2. Simulated morning on your phone** — replays
`tests/fixtures/morning_scenario.json` (15 checks: a unit is posted, stays
listed, changes rent, has an API blip, is taken down, is re-listed alongside a
second unit) through the real watcher code, against a fake copy of the
listings API running on your computer:

```powershell
$env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python tests/simulate_morning.py
```

Your phone should get exactly four alerts, all titled `[TEST] …`:

1. **Emergency** — "New StuyTown affordable unit - apply now" (Apt 5A), with
   an "Open this unit to apply" link. Repeats every minute until you tap
   Acknowledge (test alerts stop by themselves after 3 minutes).
2. **Normal** — "StuyTown listing updated" (5A's rent changed).
3. **Quiet** — "StuyTown unit no longer listed" (5A taken down).
4. **Emergency** — "2 new StuyTown affordable units" (5A re-listed + 12C).

Every other check stays silent, and the script prints `OK` / `MISMATCH` for
each one. Afterwards, look at `tests/output/events.json` and
`tests/output/screenshots/` — the screenshots are the real listings page
showing the fake units, exactly as the site would display them. Without the
`$env:` part it's a dry run that prints the alerts instead of sending them.
`--interval 15` runs it at the real pace (default is 5 seconds between checks);
`--no-screenshots` skips the browser.

**3. One test alert** — sends a single new-unit-style Emergency alert and
nothing else. Run it as often as you like while tuning Do Not Disturb:

```powershell
$env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python tests/send_test_notification.py
```

**4. The real thing in GitHub Actions** — Actions tab → **StuyTown watcher** →
**Run workflow**, set *minutes* to 2 and tick *Send a [TEST] alert*. That
checks the live listings every 15 seconds for 2 minutes from GitHub's servers
and sends one test alert, proving the secrets, the API and the setup all work
there. (Run it outside 7–10am, or it waits for the morning run to finish.)

## Files

- `check_units.py` — the core: fetch the listings, compare with the saved
  state, alert, log events. `python check_units.py` just prints what's listed
  right now.
- `watch_loop.py` — the 7–10am loop: timing, screenshots, committing results.
  `python watch_loop.py --minutes 2` does a local test run against the live
  site (alerts only if you've set the Pushover variables; never commits).
- `screenshot.py` — full-page screenshot of the listings page via a headless
  browser (Playwright)
- `data/last_seen.json` — the state (what's listed right now)
- `data/events.json` — history of every new / updated / removed unit
- `screenshots/` — one screenshot per new-unit event
- `tests/` — automated tests, the phone simulation, the test alert, and the
  fake data they use (`tests/fixtures/`)
- `.github/workflows/watch.yml` — the daily schedule
