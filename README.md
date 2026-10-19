# StuyTown / PCV Affordable Housing Watcher

Every morning from 7:00 to 10:00am ET it checks StuyTown's affordable-housing
listings: every **2 seconds**, and every **0.25 seconds** around each quarter
hour, when StuyTown posts units. It records **every** listing it sees and,
optionally, applies for you. It runs free on GitHub Actions, started at 6:13am
by Google Cloud Scheduler. Nothing runs on your computer.

How it's built, and what the live data has shown so far:
[docs/architecture.md](docs/architecture.md).

## What it does each morning

1. **Checks the listings** and compares each answer with the last one. A unit
   that's new, changed or gone is logged in `data/events.json`, qualifying or
   not.
2. **Alerts you only for units that qualify**: rent at or under
   `AUTO_APPLY_MAX_RENT`, and your income at or above the unit's minimum. That's
   one **Emergency** Pushover alert, which breaks through Do Not Disturb. Units
   that don't qualify are logged, never alerted.
3. **Applies, if auto-apply is on.** The alert then says *auto-applying now*,
   and a second, normal-priority message gives the result: *Applied*, or
   *failed / unconfirmed, apply yourself*. With auto-apply off, the alert says
   *apply now*.
4. **At 10:00** it writes the morning's stats and rebuilds the summary page,
   then commits everything in `data/`.

| You get | When |
| --- | --- |
| Emergency: "Qualifying StuyTown unit - auto-applying now" (or "apply now" when auto-apply is off) | A new unit meets your conditions |
| Normal: "Applied: Apt …" / "Auto-apply failed …" | Auto-apply is on and has a result for that unit |
| Nothing | Any other unit, update or removal; problems (those go on the summary page) |

## The data

| File | What's in it |
| --- | --- |
| [`data/summary.md`](data/summary.md) | **Start here.** Rebuilt every morning: what needs a look, one line per morning, every unit seen (posted, rent, beds, income minimum, how long it lasted), patterns (posting times, weekdays, how long units last) |
| `data/events.json` | Every unit ever seen, with the full listing as the site gave it, and every change and removal |
| `data/run_stats/<date>.json` | One per morning: when each unit was posted (to within 0.25 s, against the site's own clock), how long it stayed up, how the listings API behaved |
| `data/applications.json` | Each application: result and timeline (when sent, when the site received it) |
| `data/apply_runs/` | Recordings of trips through the form (only while auto-apply is on) |

Nothing personal is ever committed: your details are replaced with their key
names before anything is written, and so are tokens and API keys. The repo is
public, so that matters.

## Turning it on and off

- **Everything off:** Actions tab → **StuyTown watcher** → **⋯** → **Disable
  workflow**. Also pause the Cloud Scheduler job. GitHub's own backup schedule
  would otherwise still start it.
- **Collect data and alert, but don't apply:** set the `AUTO_APPLY_MODE`
  variable to `off`.
- **Apply too:** set `AUTO_APPLY_MODE` to `on`. Needs the variable and secret
  below.

| Setting (Settings → Secrets and variables → Actions) | What it is |
| --- | --- |
| Variable `AUTO_APPLY_MAX_RENT` | Your rent limit in $/month, e.g. `3000` (inclusive). Decides which units alert, even with auto-apply off |
| Variable `AUTO_APPLY_MODE` | `on` or `off` |
| Secret `APPLICANT_PROFILE` | Your details as one JSON document. `applicant_profile.example.json` shows every key. Keep your own copy: GitHub never shows a secret again |
| Secrets `PUSHOVER_TOKEN`, `PUSHOVER_USER` | Your Pushover app token and user key |

With auto-apply on, it applies **at most once per unit per day**. It goes
straight to StuyTown's application API (about 0.05 s after the unit appears)
once it has checked its request against the site's own form, and otherwise
fills the form in a browser (about 1 s). After applying, finish the
*detailed* application that StuyTown emails you **within 24 hours**.

## Setup (once)

1. **Pushover:** create an application to get an API token, and note your
   user key. Add both as secrets (above). In the Pushover app, allow Emergency
   alerts through Focus / Do Not Disturb.
2. **GitHub token for the 6:13 start:** github.com → Settings → Developer
   settings → **Fine-grained tokens**. Give it access to this repo only, with
   **Actions: Read and write** and the longest expiry. Set a calendar reminder
   to renew it.
3. **Google Cloud Scheduler** (free: 3 jobs included; this uses 1). Activate a
   full account, so it doesn't stop after the 90-day trial, and set a $1
   budget alert. Then **Cloud Scheduler → Create job**:

| Field | Value |
| --- | --- |
| Frequency / Timezone | `13 6 * * *` / `America/New_York` |
| Target | HTTP `POST` to `https://api.github.com/repos/cshoffmann/stuytown-affordable-housing-watcher/actions/workflows/watch.yml/dispatches` |
| Body | `{"ref":"main"}` |
| Headers | `Authorization: Bearer <github_pat_…>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json` |
| Retries | 3, min backoff 30s |

To test it, use **⋮ → Force run**. A "via manual request" run should appear
in the Actions tab. If it fails, the error code says why: 401 means the token
is wrong, 403 means it lacks Actions write access, and 404 means it isn't
scoped to this repo.

GitHub's own schedule (6:13, 6:43 … 9:43) is only a backup: it's often hours
late. While one run is going, any others wait and then cancel themselves.
"Cancelled" runs in the Actions tab are normal.

## Testing

```
pip install -r requirements.txt && playwright install chromium
python -m unittest discover -s tests       # everything, offline, against fake copies of the site
python tests/simulate_morning.py           # a fake morning through the real code (1 Emergency alert)
python tests/send_test_notification.py     # one test alert to your phone
python auto_apply.py --selftest            # the real applier against a fake copy of the form
```

The Pushover keys are read from `PUSHOVER_TOKEN` / `PUSHOVER_USER`; without
them, alerts are printed instead of sent. In GitHub, **Run workflow** has the
same tests: *minutes* for a short live run, *Send a [TEST] alert*, *Test the
alerts instead* and *Test auto-apply instead*.

## Files

| File | Job |
| --- | --- |
| `watch_loop.py` | The 7–10am run: the checking schedule, starting everything, commits |
| `check_units.py` | Fetches the listings, spots changes, picks alerts, logs events |
| `auto_apply.py` | The applier: browsers, the form, results |
| `direct_submit.py` | Learns the site's application request and sends it straight to its API |
| `apply_recorder.py` | Records each trip through the form, and redacts it |
| `run_stats.py`, `summary.py` | The morning's stats, and the summary page |
| `background.py` | The background workers that send alerts and write logs |
| `.github/workflows/watch.yml` | The GitHub Actions workflow |
