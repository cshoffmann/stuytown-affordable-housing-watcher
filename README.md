# StuyTown / PCV Affordable Housing Watcher

Checks the real StuyTown/PCV affordable housing listings **every 2 seconds**
from 7:00 to 10:00am ET, every day. When a unit at or under your rent limit
appears, it **applies for you** within seconds: browsers kept open since 6:59
fill in and submit StuyTown's application form. It also sends an
Emergency-priority Pushover alert (bypasses silent mode / Do Not Disturb),
then a message with the result. Each unit closes after 3 applications, and
the cheap ones have been gone in 15–45 seconds, so speed is the whole game.
Every unit's details are logged to `data/events.json`. Runs free on GitHub
Actions, started each morning by Google Cloud Scheduler (free tier); nothing
has to stay running on your computer.

**How it's built:** a checker that never stops, an applier that always goes
first, and background workers for everything else. See
[docs/architecture.md](docs/architecture.md), which also compares it with the
old one-thing-at-a-time design.

## How alerts work (no duplicates)

`data/last_seen.json` remembers every unit currently listed, with the exact
data the API last returned for it. On every check, **the applier gets the
listings first**; alerts and logging come after, in the background:

| What the API shows | What happens |
| --- | --- |
| A new unit that **qualifies** (rent ≤ `AUTO_APPLY_MAX_RENT`, your income ≥ its minimum) | Auto-apply starts on it right away. **Emergency alert** "Qualifying StuyTown unit – auto-applying now", then one message with the result. Logged in `events.json` with the unit's full metadata |
| A new unit that **doesn't qualify** | One **normal** alert "New StuyTown unit (doesn't qualify)" with a link to it. Logged |
| The same unit, same data | **Nothing.** This is what stops a listing from alerting every 2 seconds |
| The same unit, changed data (rent, available date, income requirement…) | Logged in `events.json`, no alert |
| A unit gone for a minute (at least 2 checks in a row) | Logged, no alert. The unit leaves the state, so if it's **re-listed later it counts as new again** |
| A unit missing from just one response, then back | Nothing: treated as an API blip, so it can't re-alert you |

Alerts are sent by a background worker with its own retries, so a slow or
failing Pushover can delay an alert but never a check or an application.
With no `AUTO_APPLY_MAX_RENT` set, every new unit gets the Emergency alert.
There's no silent "baseline" run: if units are already listed the first time
it runs, you're alerted.

The state is committed back to the repo every 5 minutes when something
changed, and at the end of each morning, so the next day picks up exactly
where the last one left off.

## The schedule

Two independent triggers start the same workflow
(`.github/workflows/watch.yml`):

1. **Google Cloud Scheduler — the main trigger.** A job fires every day at
   **6:13am New York time** and calls GitHub's API to start the workflow
   (the same as tapping **Run workflow**), so the run starts within seconds
   and shows as **"via manual request"** in the Actions tab. The job sets up,
   opens the auto-apply browsers at 6:59, checks every 2 seconds from 7:00
   until 10:00, then exits. You'll see one ~4-hour run per day. Setup is under
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

- If the Pushover secrets are missing or wrong, the watcher still watches and
  auto-applies (that doesn't need your phone), and the run ends with a red X,
  so GitHub emails you.
- If the site ever asks it to slow down (HTTP 429/403), it backs off,
  honoring the site's Retry-After, and returns to 2 seconds gradually. Plain
  errors such as timeouts are retried quickly.
- GitHub turns off schedules in public repos after 60 days without a commit;
  if nothing has been committed for 45 days, the watcher commits a tiny
  `data/heartbeat.json` to prevent that.

To watch it live: Actions tab → today's **StuyTown watcher** run → expand
"Watch for new units". Changes log a line like
`[07:55:00 ET] 1 listed - NEW: Apt 5A, 287 Avenue C`, and there's one
"still checking" line a minute.

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

## Auto-apply (`auto_apply.py`)

When a listed unit's monthly rent is **at or under your limit** (and your
income meets the unit's minimum), the watcher applies for you. **Applying
always comes first**: nothing else can delay it.

1. **At 6:59** two applier workers each open Chromium and load the StuyTown
   site, so no application ever waits for a browser to start.
2. **The check that first sees the unit** hands it to the applier before
   any alert or log is queued; the checker carries on checking every 2 s. If
   two cheap units appear together, each worker takes one and they're
   applied to at the same time, cheapest first.
3. A worker opens the unit's form. Once the form's address has been learned
   and checked (see below), it goes **straight to the form**. Until then, or
   if that ever fails, it opens the unit's page and presses **APPLY NOW**
   (drawn by the page's JavaScript at the very bottom; look-alike links in
   the site's menu and footer are ignored).
4. It fills every field from your applicant profile (see the table below),
   then **reads each box back** to check it took the value.
5. It **takes a screenshot of the filled-in form**.
6. If everything StuyTown requires is filled in correctly, it presses
   **SUBMIT**, checks the click really reached the button, and waits for the
   site's answer.
7. In the background: **one** normal-priority message with the result
   ("Applied", with the filled-in form attached, or a separate "Auto-apply
   failed / unconfirmed" message with the page it ended on). The recording
   (see *What gets recorded*) and `data/applications.json` are written by the
   background logger, which waits until no application is running.

**Learning the form's address.** The first time APPLY NOW leads to the form,
its address is remembered (`data/apply_form_url.json`) if it contains the
unit's ID. When an over-limit unit's form is recorded, the watcher also
checks that opening that address directly shows the form for that
apartment. Only then do applications skip the unit page, about a second
faster. The check is repeated whenever the address changes.

**Speed matters.** Units under $3,000 have been gone **15–45 seconds** after
they appeared (all 3 applications taken); the $4,000+ ones stayed 35–50
minutes. Phase 1 takes posting → SUBMIT from about 8.7 s to about 2–3 s.
On the fake copy of the site, the applier submits about 1 s after being
handed a unit.

**After it submits:** StuyTown emails you a link to the *detailed*
application, which has to be completed **within 24 hours** to stay eligible.
That part is still yours, and the "Applied" message reminds you.

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

  Everything except *submitted* gets its own "Auto-apply failed" (or
  "unconfirmed") message telling you to **apply yourself now**, with the unit
  link and a screenshot of where it ended.
- **Every qualifying unit, once:** there's no daily limit, so every unit at
  or under your rent limit gets applied to, but each unit only once, ever: a
  unit being applied to is never queued again by the next checks, and one
  that was submitted (or may have been) is never sent again, even on a later
  morning. Only an attempt that sent nothing (a crash, a missed SUBMIT
  click) gets one more try.
- **Isolation:** the applier never waits on Pushover, disk or git, and a
  failure in any of those can't reach it. If an applier browser crashes, it's
  restarted.

### What gets recorded, so failures can be fixed

Every trip through the form is saved to
`data/apply_runs/<time>_<apartment>_apply/` and committed with the morning's
results. **Your details are replaced with their key names** before anything
is written: "Jane" becomes `<first_name>`, and "+1 212 555 0123" becomes
`<cell_phone>`. That covers any letter case, any phone format, the income as
`95000`, `95,000.00` or in cents, and URL-encoded text.

| File | What's in it |
| --- | --- |
| `report.json` | Each step with its timing and page address. Every form field with its label and how the site built it (`name`, `id`, `autocomplete`, `inputmode`, `maxlength`, `class` …, never its value). How each box took its value (a plain fill, typed key by key, which spelling) and, for phone/income/zip-type boxes, the *shape* it ended up in (`+# ### ### ####`). The buttons. What was filled and what wasn't. Whether the form named the right apartment. Console errors. **The text the page gained after SUBMIT** (the site's real confirmation or complaint). Page navigations, live (WebSocket) connections, and cookies by name and settings (never values). How the page is built and loaded (framework, load times, slowest requests). On recordings: how the form is sent (`action`, `method`, hidden fields by name and length, CAPTCHA widgets). The outcome, with a timeline |
| `report.json` → `analysis` | Worked out when it's written: **the request SUBMIT sent**, summarized (method, address, header names, the body's field names, which ones look like tokens, the answer and how fast it came), every request in the seconds after SUBMIT, where the scripts come from, the CDN, and any sign of an anti-bot, CAPTCHA or waiting-room service |
| `network.json` | Every request the browser made, including **the request SUBMIT sends and the site's answer**, with bodies for pages and API calls. Cookie headers are dropped |
| `1_unit_page.html` … `4_after_submit.html` | The page's HTML at each stage |

Each application in `data/applications.json` also gets a **timeline**: when
the checker queued it, when a browser took it, when the form was found and
how (`direct address` or `unit page`), when SUBMIT was pressed, when the
page first changed, when the answer came, all in UTC to the millisecond.

**One stats file per morning**, `data/run_stats/<date>.json`, covers what a
single recording can't: how the listings API answers being checked every 2
seconds (response times, statuses, whether the kept-open connection holds,
caching headers such as `Age` or `ETag`, any rate-limit headers, how far its
clock is from ours), every slow-down and why, every unit with how long it
really stayed listed and how many seconds after it first appeared SUBMIT was
pressed, and what the idle listings page did on its own (does the site poll
for new units itself?). It's about the site and the listings: nothing of
yours is in it. `docs/architecture.md` has a table of which file answers
which question.

Screenshots of the filled-in form can't be redacted, so they only go to your
phone (and the runner's gitignored `private/` folder, which disappears after
the run).

**There's real data even on mornings nothing qualifies.** While auto-apply is
on, it also opens the form of up to **2 units over your limit** each morning
and records it, without filling or sending anything, and only while no
application is running. These go to
`data/apply_runs/<time>_<apartment>_record/`, including a screenshot of the
empty form, which contains nothing of yours. This is also when the form's
address gets checked.

### Settings: repository variables, not code

Nothing is hard-coded. Two **repository variables** (Settings → Secrets and
variables → Actions → **Variables** tab) control auto-apply. Changes apply
from the next run.

| Variable | Value | Meaning |
| --- | --- | --- |
| `AUTO_APPLY_MODE` | `on` / `off` | `on`: fill, screenshot and submit for qualifying units. `off` or not set: auto-apply does nothing at all |
| `AUTO_APPLY_MAX_RENT` | e.g. `3000` | Your rent limit in $/month, inclusive (a $3,000.00 unit qualifies; $3,000.01 doesn't). **Required** when on: there's no built-in default, and if it's missing, auto-apply stays off and tells you why. It also decides which new units get the Emergency alert, even with auto-apply off |

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
`tests/` touches the real `data/` folder or git; simulation output goes to
`tests/output/` (gitignored, wiped each run).

One-time setup on your computer (PowerShell, from the repo folder):

```powershell
pip install -r requirements.txt
playwright install chromium
```

**1. Automated checks.** These cover:

- the duplicate/state logic and the alert rules;
- the 2-second checker: back-off, recovery, quiet logging, batched commits;
- the background workers;
- the applier's queue;
- form filling in a real headless browser;
- two browsers applying in parallel, and the learned form address, against a
  fake copy of StuyTown's site.

No network, no phone:

```powershell
python -m unittest discover -s tests -v
```

**2. Simulated morning on your phone.** Replays
`tests/fixtures/morning_scenario.json` (15 checks: a unit is posted, stays
listed, changes rent, has an API blip, is taken down, is re-listed alongside a
second unit) through the real watcher code, against a fake copy of the
listings API running on your computer. Auto-apply stays off; the rent limit
is set to $1,500 so both kinds of new-unit alert show up:

```powershell
$env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python tests/simulate_morning.py
```

Your phone should get exactly three alerts, all titled `[TEST] …`:

1. **Normal**: "New StuyTown unit (doesn't qualify)" (Apt 5A, $1,873).
2. **Emergency**: "Qualifying StuyTown unit - apply now" (Apt 12C, $1,450).
   Repeats every minute until you tap Acknowledge (test alerts stop by
   themselves after 3 minutes).
3. **Normal**: "New StuyTown unit (doesn't qualify)" (5A, re-listed at the
   same time).

The rent change and the removal are only logged. The script prints `OK` /
`MISMATCH` for each check; afterwards, look at `tests/output/events.json`.
Without the `$env:` part it's a dry run that prints the alerts instead of
sending them. `--interval 2` runs it at the real pace.

**3. One test alert.** Sends a single Emergency alert, built like the real
one for a qualifying unit, and nothing else. Run it as often as you like
while tuning Do Not Disturb:

```powershell
$env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python tests/send_test_notification.py
```

**4. The auto-apply self-test** (`python auto_apply.py --selftest`, or *Test
auto-apply instead* under **Run workflow**). This is the real applier: warm
browser, queue, background message and logging. It runs against the fake
copy of the form with your profile, and the result arrives as a `[TEST]
Applied` message with the filled-in form.

**5. The real thing in GitHub Actions.** Actions tab → **StuyTown watcher** →
**Run workflow**, set *minutes* to 2 and tick *Send a [TEST] alert*. That
checks the live listings every 2 seconds for 2 minutes from GitHub's servers
and sends one test alert, proving the secrets, the API and the setup all work
there. (Run it outside 7–10am, or it waits for the morning run to finish.)

## Files

- `watch_loop.py`: the 7–10am run. The checker (`Watcher`, `Pace`), plus
  starting the applier and the background workers and committing results.
  `python watch_loop.py --minutes 2` does a local test run against the live
  site (alerts only if you've set the Pushover variables; never commits).
- `check_units.py`: the core. Fetch the listings (one kept-open
  connection), compare with the saved state, pick the alerts, log events.
  `python check_units.py` just prints what's listed right now.
- `auto_apply.py`: the `Applier`, which applies to units at or under your
  rent limit (see *Auto-apply*). `applicant_profile.example.json` shows every
  profile key.
- `background.py`: the notifier and logger workers, and alert retries.
- `apply_recorder.py`: records each trip through the form, with your details
  redacted, and works out the `analysis` (the SUBMIT request, anti-bot
  signs). Also `SiteWatch`, which notes what the idle listings page does.
- `run_stats.py`: the morning's stats file (listings API, pace, units,
  applier).
- `docs/architecture.md`: how it's built, and how that compares with the
  old design.
- `data/last_seen.json`: the state (what's listed right now).
- `data/events.json`: history of every new / updated / removed unit.
- `data/applications.json`: which units auto-apply tried, and how it went
  (no personal details).
- `data/apply_form_url.json`: the learned address of the application form.
- `data/apply_runs/`: the form recordings.
- `data/run_stats/`: one stats file per morning (no personal details).
- `tests/`: automated tests, the phone simulation, the test alert, and the
  fake data they use (`tests/fixtures/`, including `apply_site/`, a fake copy
  of a unit page and the application form).
- `.github/workflows/watch.yml`: the workflow. Google Cloud Scheduler starts
  it each morning, and its own `schedule` is the backup.
