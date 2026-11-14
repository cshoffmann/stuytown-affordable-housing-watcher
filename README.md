# StuyTown / PCV Affordable Housing Watcher

Watches StuyTown's affordable-housing listings every morning, records every
unit, alerts you to the ones that fit, and can apply for you within a
second. It runs free on GitHub Actions; nothing runs on your computer. The
design in more depth: [docs/architecture.md](docs/architecture.md).

## How it works

```mermaid
flowchart LR
    S["Google Cloud Scheduler<br/>6:13 am ET"] --> R["GitHub Actions run"]
    R --> C["Checker<br/>7:00–10:00"]
    C <-->|"off: every 10 s<br/>on: every 2 s, 0.25 s at :00 :15 :30 :45"| API[("StuyTown<br/>listings API")]
    C -->|"listings"| A["Applier<br/>on: applies<br/>off: rehearses, sends nothing"]
    A -->|"application"| ST[("StuyTown")]
    C -->|"alert"| P["Pushover"]
    C -->|"every unit"| L["Logger → data/"]
    L -->|"10:00"| W["Daily log, stats, floorplans,<br/>summary, one commit"]
```

| Time (ET) | What happens |
| --- | --- |
| 6:13 | Google Cloud Scheduler starts the workflow. GitHub's own schedule (6:13–9:43) is a backup. |
| 6:59 | Browsers open with the site already loaded: two to apply if auto-apply is on, one to rehearse if it's off. |
| 7:00–10:00 | **Auto-apply off (collecting data):** it checks every 10 s. The shortest listing so far lasted 25 s, so none are missed. **On:** every 2 s, and every 0.25 s from 3 s before to 15 s after each quarter hour, when StuyTown posts units. The site says it only lists between 7 and 10 AM. |
| New unit | The applier gets it first, then the alert goes out, then it's logged. Every unit is logged, whether it qualifies or not. |
| 10:00 | A line in [`data/daily_log.md`](data/daily_log.md) (units, beds, rent), stats, new floorplans and the summary page, then **one commit for the day**. Its message says what was listed, e.g. "Fri Nov 6: 4 new listings (6,140 checks)" with one line per unit, or "no new listings". |

## Alerts

| You get | When |
| --- | --- |
| **Emergency** "Qualifying StuyTown unit – apply now" (or "auto-applying now") | A new unit within your rent limit and income. It breaks through Do Not Disturb |
| "Applied: Apt …" / "Auto-apply failed …" | Auto-apply is on and has a result |
| Nothing | Any other unit (logged only) |
| GitHub email "Run failed" | Something needs fixing, e.g. Pushover keys or the site. The run still watches all morning; it's marked failed at the end |

## Rent limits and auto-apply

A unit **qualifies** when its rent is at or under `AUTO_APPLY_RULES` for its
size (e.g. `1:2300, 2:2800`) and your income is at or above the unit's
minimum. `AUTO_APPLY_MAX_RENT`, if set, covers sizes the rules don't list.

- `AUTO_APPLY_MODE=off`: you get the alert and apply yourself. It still **rehearses**: each morning it fills
  the first listed unit's form and presses SUBMIT with every request held back, so nothing is sent but the
  applying path stays tested and ready.
- `AUTO_APPLY_MODE=on`: it applies **once per unit per day**.
  - **Direct:** the application goes straight to StuyTown's API about
    0.05 s after the unit appears. It only does this once the request
    format has been proven against the site's current code.
  - **Browser:** otherwise it fills and submits the form in a warm browser,
    in about 1 s. That's also how the format gets proven.
- Then finish the detailed application StuyTown emails you **within 24
  hours**.

Units around $2,100–2,800 have been gone (3 applications taken) 25–81 s
after appearing, which is why applying comes before everything else.

## The data

| File | What's in it |
| --- | --- |
| [`data/daily_log.md`](data/daily_log.md) | One line per morning: how many units, and each one's apartment, beds and rent |
| [`data/summary.md`](data/summary.md) | **Start here.** What needs a look, each morning, every unit (floor, size, rent, finish, layout, features, how long it lasted), layouts with floorplans, patterns |
| `data/events.json` | Every unit ever seen, with the full listing, and every change and removal |
| `data/run_stats/<date>.json` | Each morning: checks, API health, when each unit was posted and how long it lasted |
| `data/applications.json`, `data/apply_runs/` | Each application's result and timeline; redacted recordings of the form |
| `data/floorplans/` | Each layout's floorplan (SVG + PNG) and room sizes |

Nothing personal is committed (the repo is public). Your details are
replaced with their key names, and tokens and API keys are scrubbed.

## Settings

Settings → Secrets and variables → Actions.

| Setting | What it is |
| --- | --- |
| Variable `AUTO_APPLY_MODE` | `on` or `off` |
| Variable `AUTO_APPLY_RULES` | Rent limits by bedrooms, $/month, inclusive: `1:2300, 2:2800` (`studio:` works too) |
| Variable `AUTO_APPLY_MAX_RENT` | Optional: one limit for sizes the rules don't list |
| Secret `APPLICANT_PROFILE` | Your details as one JSON document (`applicant_profile.example.json` shows every key). Keep a copy: GitHub never shows it again |
| Secrets `PUSHOVER_TOKEN`, `PUSHOVER_USER` | Pushover app token and user key |

**All off:** Actions → StuyTown watcher → ⋯ → Disable workflow, and pause the
Cloud Scheduler job.

## Setup (once)

1. **Pushover:** create an application for an API token; note your user
   key; add both as secrets. Buy the app after its 30-day trial, and allow
   Emergency alerts through Focus / Do Not Disturb.
2. **GitHub token** for the 6:13 start: Settings → Developer settings →
   Fine-grained tokens, this repo only, **Actions: Read and write**, longest
   expiry. Set a reminder to renew it.
3. **Google Cloud Scheduler** (free tier; full account, $1 budget alert).
   Create a job:

| Field | Value |
| --- | --- |
| Frequency / Timezone | `13 6 * * *` / `America/New_York` |
| Target | `POST https://api.github.com/repos/cshoffmann/stuytown-affordable-housing-watcher/actions/workflows/watch.yml/dispatches` |
| Body | `{"ref":"main"}` |
| Headers | `Authorization: Bearer <github_pat_…>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json` |
| Retries | 3, min backoff 30s |

Test it with ⋮ → Force run. Errors: 401 means a wrong token, 403 means no
Actions write access, and 404 means the token isn't scoped to this repo.
Runs that wait behind the main one and then cancel themselves are normal.

## Testing

**Run workflow** (Actions tab) has a button for each check:

| Button | What it does |
| --- | --- |
| *minutes* | A short live run |
| *Send a [TEST] alert* | Proves the Pushover keys work |
| *Test the alerts* | A fake morning |
| *Test auto-apply* | The real applier against a fake form, with your profile |
| *Check the site* | Compares StuyTown's own listings page with what the watcher sees |

On your computer:

```
pip install -r requirements.txt && playwright install chromium
python -m unittest discover -s tests    # everything, offline
python tests/simulate_morning.py        # a fake morning through the real code
python auto_apply.py --selftest         # the real applier against a fake form
python site_check.py                    # the live site vs the watcher
```

## Files

| File | Job |
| --- | --- |
| `watch_loop.py` | The morning run: schedule, workers, wrap-up, commits |
| `check_units.py` | Listings, changes, alerts, events |
| `auto_apply.py`, `direct_submit.py` | The applier: rules, browsers, the direct request |
| `apply_recorder.py` | Redacted recordings of the form |
| `run_stats.py`, `summary.py` | Each morning's stats; the summary page |
| `floorplans.py` | Each layout's floorplan (SVG, PNG, room sizes) |
| `site_check.py` | The live site vs the watcher |
| `background.py` | The alert and logging workers |
| `.github/workflows/watch.yml` | The workflow |
