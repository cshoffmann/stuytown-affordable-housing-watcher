# StuyTown / PCV Affordable Housing Watcher

Checks the real StuyTown/PCV affordable housing listings roughly every 15
seconds between 7-10am ET (units reportedly go to only the first three
applicants in order, so detection speed matters) and sends an
Emergency-priority Pushover notification (bypasses silent mode / Do Not
Disturb) the moment a new unit appears. Also saves a screenshot and logs
the details (unit count, bedrooms, timestamp) to `data/events.json`. Runs
free on GitHub Actions -- nothing needs to stay running on your own
computer, and it's already live now that the repo is pushed; there's no
separate cron job to set up.

**Two workflows, not one:**
- **`watch.yml`** -- the live system. Triggers once around 6:58am ET and
  then loops continuously (checking every ~15 seconds, with a little
  random jitter) until just after 10am, when it exits on its own.
- **`check.yml`** -- a single on-demand check, kept around for manual
  testing. No automatic schedule; only runs when you click "Run workflow."

## Setup

1. In your Pushover dashboard, note your **User Key** (shown on the main
   page after login), then go to **Your Applications → Create an
   Application/API Token**, name it anything (e.g. "StuyTown Watcher"),
   and copy the **API Token** it gives you.
2. In this repo on GitHub: **Settings → Secrets and variables → Actions**,
   add two secrets: `PUSHOVER_TOKEN` (the API token) and `PUSHOVER_USER`
   (your user key).
3. That's it -- the schedule in `.github/workflows/watch.yml` is already
   active. To confirm it works right away rather than waiting for
   tomorrow's window: Actions tab → **StuyTown continuous watcher (7-10am
   ET)** → **Run workflow**.

## Confirming it's actually running

This looks different from a typical "one run per check" setup, so it's
worth knowing what to expect: because `watch.yml` is one continuous loop
rather than a fresh process every few minutes, you'll see **one run per
day**, lasting roughly 3 hours -- not dozens of short ones. Click into
that run and expand "Run continuous watcher" to see the log: mostly "No
change." repeated every ~15 seconds, with "NEW UNIT(S) DETECTED" if
something posts. Open it while it's still in progress and GitHub streams
that log live, so you can genuinely watch it check in real time.

`check.yml` (manual only, no schedule) is the quick way to sanity-check
the secrets and basic logic anytime without waiting for the window: run
it from the Actions tab and expand "Check for new units" -- the first log
line, `Pushover credentials loaded: True/False`, confirms the two secrets
are wired up correctly.

## File layout

- `data/last_seen.json` -- the current set of unit IDs, used to detect
  what's new next run
- `data/events.json` -- running history of every detection: timestamp,
  unit count, bedrooms, screenshot filename
- `screenshots/` -- one PNG per detection event

All three start out empty (`data/` and `screenshots/` each ship with a
placeholder `.gitkeep` file so the folders exist in the repo from the
start) and fill in over time as the workflow commits to them.

## Testing

There are 0 real units listed right now, so test with fake data rather
than waiting for a real one. Two different scripts, for two different
purposes -- and neither can ever touch the real `data/` or `screenshots/`
folders, so there's nothing to clean up afterward and no risk of polluting
real state (see "On test vs. production" below):

**`test_notification.py`** -- fires one real Pushover alert and does
nothing else, no files touched at all. Safe to run over and over while
you tune your phone: silence it, run this, see what happens, adjust
Pushover's Emergency-priority sound or your phone's Focus/DND exceptions,
repeat.

```bash
PUSHOVER_TOKEN=your-token PUSHOVER_USER=your-user-key python3 test_notification.py
```

**`test_check_units.py`** -- a genuine simulation of the *entire*
workflow, not just `check_units.py`: fakes a unit and the time window,
runs the real detection logic, and -- exactly like the real workflow --
if that counts as "new," it also takes a real screenshot via
`screenshot.py` and writes a real `events.json`-shaped entry. Everything
it touches lives under `test_output/` (already gitignored), completely
separate from the real `data/` and `screenshots/` folders. Run it once
per test, not repeatedly, since each run appends to its own local test
event log.

```bash
pip install playwright   # one-time, needed for the screenshot part
playwright install chromium
PUSHOVER_TOKEN=your-token PUSHOVER_USER=your-user-key python3 test_check_units.py
```

Check `test_output/screenshots/` and `test_output/events.json` afterward
to confirm everything came out looking right.

## On test vs. production

Short answer: one repo, not two -- splitting this into separate
test/production repos would mean duplicating the workflow, the secrets,
and the setup, for a script whose entire job is checking one webpage a
few times a day. Not worth it at this scale.

The actual risk a separate repo would guard against -- test runs
corrupting real state -- is solved more simply by making collision
impossible: `test_check_units.py` writes everything under `test_output/`,
a path the real `check_units.py` and `screenshot.py` never read from or
write to. Same codebase, same repo, genuinely can't interfere with each
other.

## How duplicates are handled

- **No duplicate alerts for the same unit.** `data/last_seen.json` is the
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

- `check_units.py` -- the core check: fetches listings, diffs against
  last-seen state, sends the alert, logs the event. Used directly by
  `check.yml`, and reused (not duplicated) by `watch_loop.py`.
- `watch_loop.py` -- continuous polling loop built on top of
  `check_units.py`'s functions; this is what actually runs live, 7-10am ET
- `screenshot.py` -- takes the screenshot (only when something's found)
- `test_notification.py` -- repeatable notification-only test, for tuning your phone
- `test_check_units.py` -- one-time full-pipeline test, fully isolated to `test_output/`
- `.github/workflows/watch.yml` -- the live schedule: one continuous loop, 7-10am ET
- `.github/workflows/check.yml` -- manual-only single check, for testing
