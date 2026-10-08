# StuyTown / PCV Affordable Housing Watcher

Checks the real StuyTown/PCV affordable housing listings every 15 seconds
from 7:00 to 10:00am ET, every day, and sends an Emergency-priority Pushover
alert (bypasses silent mode / Do Not Disturb) the moment a new unit appears —
with a link straight to that unit's page so you can apply. Each unit closes
after 3 applications, so speed matters. It also logs the unit's details to
`data/events.json` and saves a screenshot of the listings page. Runs free on
GitHub Actions, started each morning by Google Cloud Scheduler (free tier);
nothing has to stay running on your computer.

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

## The schedule

Two independent triggers start the same workflow
(`.github/workflows/watch.yml`):

1. **Google Cloud Scheduler — the main trigger.** A job fires every day at
   **6:13am New York time** and calls GitHub's API to start the workflow
   (the same as tapping **Run workflow**), so the run starts within seconds
   and shows as **"via manual request"** in the Actions tab. The job sets up,
   waits for 7:00, checks every 15 seconds until 10:00, then exits. You'll
   see one ~4-hour run per day. Setup is under
   [Google Cloud Scheduler](#google-cloud-scheduler-the-613am-start) below.
2. **GitHub's own schedule — the backup.** The workflow's `schedule` fires at
   6:13, 6:43, 7:13 … 9:43 (shows as **"via schedule"**). GitHub's scheduler
   is best-effort: in this repo's first week its runs arrived 4 to 9 hours
   late or not at all, which is why it isn't the main trigger. It's kept
   because it's free and harmless: while a watcher is running, backups wait
   in line and are replaced by newer ones — **"cancelled" runs in the Actions
   tab are normal** — and one that arrives after 10:00 exits within a minute.
   On a morning the Google trigger fails, an on-time backup covers whatever
   is left of the window.

Only one watcher ever runs at a time, so the two triggers can't double-alert.

Other safeguards:

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
4. Set up the 6:13am trigger in Google Cloud Scheduler (next section).

### Google Cloud Scheduler (the 6:13am start)

Free: Cloud Scheduler includes 3 jobs per billing account, and this uses 1.
Daily runs don't count against that.

**Google Cloud account.** Sign up at cloud.google.com (a card is required to
verify you, but isn't charged). Then:

- Click **Activate full account** (or upgrade under **Billing**). A free-trial
  account shuts down after 90 days and stops the job; an upgraded one keeps
  the free allowance, so this still costs $0.
- Under **Billing → Budgets & alerts**, create a **$1** budget so Google
  emails you if anything ever starts to cost money.

**GitHub token.** github.com → Settings → Developer settings → Personal
access tokens → **Fine-grained tokens** → Generate new token:

- Repository access: **Only select repositories** → this repo
- Repository permissions: **Actions → Read and write** (nothing else)
- Expiration: the longest offered. **Put a reminder in your calendar a week
  before it expires** — an expired token silently stops the 6:13 start (the
  GitHub backup schedule would still run, but unreliably).

**The job.** In the Google Cloud console, search **Cloud Scheduler** → enable
the API if asked → **Create job**:

| Field | Value |
| --- | --- |
| Name | `start-stuytown-watcher` |
| Region | any (this repo's uses `us-central1`) |
| Frequency | `13 6 * * *` |
| Timezone | `America/New_York` (handles daylight saving) |
| Target type | HTTP |
| URL | `https://api.github.com/repos/cshoffmann/stuytown-affordable-housing-watcher/actions/workflows/watch.yml/dispatches` |
| HTTP method | POST |
| Body | `{"ref":"main"}` |
| Header `Authorization` | `Bearer <your github_pat_… token>` |
| Header `Accept` | `application/vnd.github+json` |
| Header `X-GitHub-Api-Version` | `2022-11-28` |
| Header `Content-Type` | `application/json` |
| Auth header | None |
| Max retry attempts / Min backoff | `3` / `30s` |

**Test it:** ⋮ → **Force run**. After a minute or two (click the console's
own **Refresh**), *Status of last execution* should say **Success** and a
"via manual request" run should appear in the Actions tab. Outside 6–10am
that run exits in about a minute saying the window is over — that's expected.
If it says **Failed**, the job's logs show GitHub's answer: 401 = token
pasted wrong or `Bearer ` missing, 403 = token lacks Actions write, 404 =
token not scoped to this repo.

**Renewing the token:** generate a new one the same way, then edit the job
and replace the `Authorization` header value. Force run once to confirm.

## Auto-apply (`auto_apply.py`) — version 1

When a listed unit's monthly rent is **at or under $3,000** (and your income
meets the unit's minimum), the watcher opens the unit's page, presses
**Apply Now**, fills in the application from your saved applicant profile,
submits it, and sends you the result with a screenshot of the form. It runs
right after the new-unit alert, so you still hear about every unit first.

It's controlled by the `AUTO_APPLY_MODE` repository variable:

| Mode | What it does |
| --- | --- |
| `off` (or not set) | Never opens an application — the watcher works exactly as before |
| `dry_run` | Fills in the whole form and sends you a screenshot, but **never presses the final Submit**. Start here |
| `submit` | Fills in the form and submits it |

Safety rails: a unit is applied to **once, ever** (an attempt that crashed
gets one retry; a submit with no confirmation is never repeated), at most 3
applications per morning, cheapest unit first. It never fills a form with
required fields left empty, and it stops at a CAPTCHA it would have to solve —
both cases send you a "couldn't finish — apply yourself now" alert with the
link and a screenshot.

### How it finds the form

It doesn't depend on the site's exact HTML. It finds buttons by their text
(**Apply…**, **Next/Continue**, **Submit…**) and fields by their visible
label, e.g. a field labelled "First Name" gets `first_name`, "ZIP Code"
gets `zip`. The label patterns are in `FORM_FIELDS` in `auto_apply.py`.
Fields it doesn't know about go in your profile's `extra_fields` (by label
text), and yes/no questions go in `radio_choices`. Every result alert lists
any **required field it couldn't fill**, by the site's own label — that's
how you tune it: add those to your profile and try again.

### Your details: one JSON secret, never a file in the repo

This repo and its Actions logs are **public**, so a JSON file in the repo
would publish your details. Separate environment variables for name, address,
income and so on would mean a dozen secrets to keep in sync. So your profile
is **one JSON document stored as one GitHub secret**, `APPLICANT_PROFILE`:

- At the start of every run each value is masked (`::add-mask::`), so it
  shows as `***` anywhere in the log.
- Form screenshots show your details, so they go only to your phone (as a
  Pushover image) and to the gitignored `private/` folder. They're never put
  in `screenshots/`.
- `data/applications.json` (committed) records only which units were tried
  and how it went: the unit, the time, the result, the profile *key names*
  used and the site's labels for any empty required fields. None of your
  details.
- On your own computer, the same JSON goes in `applicant_profile.json`, which
  is gitignored.

### Turning it on

1. Copy `applicant_profile.example.json`, fill in your real details (dates
   as `YYYY-MM-DD`; leave `move_in_date` empty to use each unit's own
   available date), and keep the copy **off** the repo.
2. GitHub → this repo → **Settings → Secrets and variables → Actions**:
   - **Secrets** tab → *New repository secret* → name `APPLICANT_PROFILE`,
     value: paste the whole JSON.
   - **Variables** tab → *New repository variable* → `AUTO_APPLY_MODE` =
     `dry_run`. Optional: `AUTO_APPLY_MAX_RENT` (default `3000`).
3. **Test it with your real profile on a fake form:** Actions → **StuyTown
   watcher** → **Run workflow** → tick *Test auto-apply instead*. It fills
   and submits the form in `tests/fixtures/apply_site/` (on GitHub's server
   only; nothing reaches StuyTown) and sends a `[TEST]` screenshot to your
   phone that shows what it typed.
4. Leave it on `dry_run` for a morning or two. Each time a qualifying unit
   is listed you'll get a `[DRY RUN]` screenshot of the real form filled in.
   Fix anything it got wrong or left empty (see *How it finds the form*).
5. When the dry runs look right, change `AUTO_APPLY_MODE` to `submit`.

To dry-run against a real unit page from your own computer (it never
submits): `python auto_apply.py --unit-url "<the unit's link>" --headed`
shows the browser while it fills the form.

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
- `auto_apply.py` — applies to units at or under your rent limit (see
  *Auto-apply*); `applicant_profile.example.json` is the profile's shape
- `data/applications.json` — which units auto-apply tried, and how it went
  (no personal details)
- `data/last_seen.json` — the state (what's listed right now)
- `data/events.json` — history of every new / updated / removed unit
- `screenshots/` — one screenshot per new-unit event
- `tests/` — automated tests, the phone simulation, the test alert, and the
  fake data they use (`tests/fixtures/`)
- `.github/workflows/watch.yml` — the workflow; Google Cloud Scheduler starts
  it each morning, and its own `schedule` is the backup
