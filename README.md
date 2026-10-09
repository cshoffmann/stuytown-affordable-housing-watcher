# StuyTown / PCV Affordable Housing Watcher

Checks the real StuyTown/PCV affordable housing listings every 10 seconds
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
| The same unit, same data | **Nothing.** This is what stops a listing from alerting every 10 seconds |
| The same unit, changed data (rent, available date, income requirement…) | One normal-priority "listing updated" alert, plus an `events.json` entry |
| A unit gone for ~1 minute (6 checks in a row) | One quiet alert (no sound), plus an `events.json` entry; the unit leaves the state, so if it's **re-listed later it alerts as new again** |
| A unit missing from just one response, then back | Nothing — treated as an API blip, so it can't re-alert you |

If sending the new-unit alert fails (e.g. Pushover is briefly unreachable),
the state isn't updated, so the next check 10 seconds later tries again — a
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
   waits for 7:00, checks every 10 seconds until 10:00, then exits. You'll
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

## Auto-apply (`auto_apply.py`) — version 3

When a listed unit's monthly rent is **at or under your limit** (and your
income meets the unit's minimum), the watcher applies for you:

1. **Right after the new-unit alert** (so you always hear about the unit
   first), it opens the unit's page in a headless browser and waits for its
   **APPLY NOW** button. That button is drawn by the page's JavaScript at the
   very bottom. Look-alike links in the site's menu and footer are ignored.
2. It presses APPLY NOW and waits for the application form ("Hi, you're
   applying to …").
3. It fills every field from your applicant profile (see the table below),
   then **reads each box back** to check it took the value.
4. It **takes a screenshot of the filled-in form**.
5. If everything StuyTown requires is filled in correctly, it presses
   **SUBMIT**, checks the click really reached the button, and waits for the
   site's answer.
6. It sends you the result with **the filled-in form attached**, plus a quiet
   second message with a screenshot of what the page showed after SUBMIT. It
   records the whole trip (see *What gets recorded*) and logs the outcome in
   `data/applications.json`.

**Speed matters.** In the first week, the units under $3,000 were gone **30–90
seconds** after they appeared; the $4,000+ ones stayed 35–50 minutes. So:

- The watcher checks every **10 seconds**.
- It skips downloading images, video and fonts while applying.
- On the copy of the form, it takes about **2 seconds** from opening the unit
  page to the site's answer.
- If several units qualify, the cheapest goes first.

**After it submits:** StuyTown emails you a link to the *detailed*
application, which has to be completed **within 24 hours** to stay eligible.
That part is still yours, and the "Applied" alert reminds you.

### The form it fills (as recorded 2026-10-08)

It's one page. Each field is found by its label text (`FORM_FIELDS` in
`auto_apply.py`), so the site's code can change without breaking anything as
long as the labels stay the same:

| Form label | Profile key | Notes |
| --- | --- | --- |
| First Name * | `first_name` | |
| Last Name * | `last_name` | |
| Email * | `email` | StuyTown's follow-up link goes here |
| Cell Phone * | `cell_phone` | Any US format. The box starts with "+", so the +1 is added for you |
| Work Phone | `work_phone` | Optional |
| Building * | `building` | The **building number** of your current address, e.g. `123` |
| Street name * | `street_name` | e.g. `Example Street` |
| Apartment No. | `apartment_no` | Optional |
| City * | `city` | |
| State | `state` | Optional, e.g. `NY` |
| Zip * | `zip` | 5 digits |
| Household Size * | `household_size` | Everyone who'll live there, including you |
| Household Gross Annual Income, $ * | `annual_income` | Before-tax yearly total, as a plain number, e.g. `95000` |

If the site ever adds a field, put it in the profile's `extra_fields`, keyed
by its label, e.g. `"extra_fields": {"Date of Birth": "01/31/1990"}`. No code
change is needed.

### What makes it reliable

- **Labels, not page code.** It reads each box's label the way a person
  would: from the `<label>`, or from the text sitting just above the box. That
  works whether or not the site's HTML links the label to the box. A "*" in
  the label counts as required.
- **Every value is read back.** Masked boxes reformat what's typed. If a value
  doesn't stick, it's typed again key by key, and other spellings are tried
  (`95000`, `95000.00`, and so on). If it still doesn't show correctly, the
  form isn't sent. One example this catches: a "+" phone box given a 10-digit
  number would read "+212 555…", which is a Moroccan number.
- **Never sends a half-filled form.** It won't press SUBMIT if:
  - a required box is empty or wrong, or
  - any field StuyTown requires wasn't found at all, which would mean a label
    changed.
- **SUBMIT is checked to have been pressed.** A full-page screenshot can leave
  the page so that the next click lands *beside* the button. It scrolls back
  after every screenshot, and confirms the click reached SUBMIT. If it
  didn't, it clicks again; a missed click sends nothing.
- **Knows how it ended:**

  | Result | What happened | Tried again? |
  | --- | --- | --- |
  | submitted | Confirmation text appeared | Never |
  | unconfirmed | SUBMIT pressed, no confirmation within 30s | Never: it may have gone through, and the site allows one application per apartment |
  | rejected | The site showed errors and kept the form | No |
  | incomplete | Something required couldn't be filled; nothing sent | No |
  | blocked | A CAPTCHA you'd have to click; nothing sent | No |
  | failed | It crashed, timed out, or couldn't press SUBMIT; nothing sent | Once more, on a later check |

  Everything except *submitted* comes with an alert that says **"apply
  yourself now"**, with the unit link and screenshots.
- **Limits:** one application per unit, ever, and at most 3 per morning.
  Nothing in auto-apply can stop or delay the watcher's alerts.

### What gets recorded, so failures can be fixed

Every trip through the form is saved to
`data/apply_runs/<time>_<apartment>_apply/` and committed with the morning's
results. **Your details are replaced with their key names** before anything
is written: "Jane" becomes `<first_name>`, and "+1 212 555 0123" becomes
`<cell_phone>`. That covers any letter case, any phone format, the income as
`95000`, `95,000.00` or in cents, and URL-encoded text.

| File | What's in it |
| --- | --- |
| `report.json` | Each step with its timing and page address. Every form field with its label and how the site built it (`name`, `id`, `autocomplete`, `inputmode`, `maxlength`, `class` …, never its value). The buttons. What was filled and what wasn't. Whether the form named the right apartment. Console errors. The outcome |
| `network.json` | Every request the browser made, including **the request SUBMIT sends and the site's answer**, with bodies for pages and API calls. Cookies are dropped |
| `1_unit_page.html` … `4_after_submit.html` | The page's HTML at each stage |

Screenshots of the filled-in form can't be redacted, so they only go to your
phone (and the runner's gitignored `private/` folder, which disappears after
the run).

**There's real data even on mornings nothing qualifies.** While auto-apply is
on, it also opens the form of up to **2 units over your limit** each morning
and records it, without filling or sending anything. These go to
`data/apply_runs/<time>_<apartment>_record/`, including screenshots of the
unit page and the empty form, since those contain nothing of yours. So the
first morning gives a recording of the real form, even if no cheap unit
appears.

### Settings: repository variables, not code

Nothing is hard-coded. Two **repository variables** (Settings → Secrets and
variables → Actions → **Variables** tab) control auto-apply. Changes apply
from the next run.

| Variable | Value | Meaning |
| --- | --- | --- |
| `AUTO_APPLY_MODE` | `on` / `off` | `on`: fill, screenshot and submit for qualifying units. `off` or not set: auto-apply does nothing at all |
| `AUTO_APPLY_MAX_RENT` | e.g. `3000` | Your rent limit in $/month, inclusive (a $3,000.00 unit qualifies; $3,000.01 doesn't). **Required** when on: there's no built-in default, and if it's missing, auto-apply stays off and tells you why |

Variables aren't shown to visitors of the repo, but they aren't encrypted
either. That's fine for a rent limit, but not for your details.

### Your details: one encrypted secret

Your details go in **one repository secret**, `APPLICANT_PROFILE`, holding
the whole profile as JSON (`applicant_profile.example.json` shows every key).
This is the reliable way to keep them hidden:

- GitHub encrypts secrets and never shows them again, not even to you. Keep
  your own copy, e.g. in a password manager, for when you need to change
  something: you re-paste the whole JSON.
- The repo and its Actions logs are public, so:
  - every profile value is masked (shown as `***`) at the start of each run;
  - screenshots of the filled form go only to your phone;
  - the recordings and `data/applications.json` never contain a value.
- At the start of each run the profile is checked: every required key
  present, email, phone, ZIP, household size and income well-formed. A
  problem turns auto-apply off for that morning, and you get an alert naming
  the key (never the value), instead of finding out when a unit appears.
- On your own computer, the same JSON goes in `applicant_profile.json`, which
  is gitignored.

### Turning it on

1. Copy `applicant_profile.example.json` and fill in your real details.
   Keep the copy **out of the repo** (password manager, notes app).
2. GitHub → this repo → **Settings → Secrets and variables → Actions**:
   - **Secrets** tab → *New repository secret*: name `APPLICANT_PROFILE`,
     value: paste the whole JSON.
   - **Variables** tab → *New repository variable*: `AUTO_APPLY_MAX_RENT` =
     `3000`, and `AUTO_APPLY_MODE` = `on`.
3. **Test your real profile on the fake form:** Actions → **StuyTown
   watcher** → **Run workflow** → tick *Test auto-apply instead*. It runs the
   real auto-apply code against the copy of the form in
   `tests/fixtures/apply_site/` on GitHub's server (nothing reaches
   StuyTown). You get a `[TEST] Applied` alert on your phone with a
   screenshot of the filled-in form showing exactly what it typed. The run's
   redacted recording is attached to the run as `test-output`.

To watch it fill a real unit's form on your own computer without submitting:
`python auto_apply.py --check-form "<the unit's link>" --headed`.

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
`tests/fixtures/morning_scenario.json` (17 checks: a unit is posted, stays
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
`--interval 10` runs it at the real pace (default is 5 seconds between checks);
`--no-screenshots` skips the browser.

**3. One test alert** — sends a single new-unit-style Emergency alert and
nothing else. Run it as often as you like while tuning Do Not Disturb:

```powershell
$env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python tests/send_test_notification.py
```

**4. The real thing in GitHub Actions** — Actions tab → **StuyTown watcher** →
**Run workflow**, set *minutes* to 2 and tick *Send a [TEST] alert*. That
checks the live listings every 10 seconds for 2 minutes from GitHub's servers
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
  *Auto-apply*); `applicant_profile.example.json` shows every profile key
- `apply_recorder.py` — records each trip through the form, with your
  details redacted
- `data/apply_runs/` — those recordings
- `tests/fixtures/apply_site/` — a fake copy of a unit page and the
  application form, for the tests and the self-test
- `data/applications.json` — which units auto-apply tried, and how it went
  (no personal details)
- `data/last_seen.json` — the state (what's listed right now)
- `data/events.json` — history of every new / updated / removed unit
- `screenshots/` — one screenshot per new-unit event
- `tests/` — automated tests, the phone simulation, the test alert, and the
  fake data they use (`tests/fixtures/`)
- `.github/workflows/watch.yml` — the workflow; Google Cloud Scheduler starts
  it each morning, and its own `schedule` is the backup
