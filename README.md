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
2. **Alerts you only for units that qualify**: rent at or under your limit
   for its size (`AUTO_APPLY_RULES`, e.g. 1 bed $2,200, 2 bed $2,700), and your
   income at or above the unit's minimum. That's one **Emergency** Pushover
   alert, which breaks through Do Not Disturb. If it's a dream unit (below),
   the alert says so. Units that don't qualify are logged, never alerted.
3. **Applies, if auto-apply is on.** The alert then says *auto-applying now*,
   and a second, normal-priority message gives the result: *Applied*, or
   *failed / unconfirmed, apply yourself*. With auto-apply off, the alert says
   *apply now*.
4. **At 10:00**, with the applier stopped, it writes the morning's stats,
   saves any new layout's floorplan, refreshes the map, and rebuilds the
   summary page, then commits everything in `data/`.

| You get | When |
| --- | --- |
| Emergency: "Qualifying StuyTown unit - auto-applying now" (or "apply now" when auto-apply is off) | A new unit meets your conditions |
| Normal: "Applied: Apt …" / "Auto-apply failed …" | Auto-apply is on and has a result for that unit |
| Nothing | Any other unit, update or removal; problems (those go on the summary page) |

## The data

| File | What's in it |
| --- | --- |
| [`data/summary.md`](data/summary.md) | **Start here.** Rebuilt every morning: what needs a look, one line per morning, dream units, every unit seen (where, floor, size, rent, finish, layout, features, which way it faces, how long it lasted), buildings, layouts with floorplans, patterns |
| [`data/units.geojson`](data/units.geojson) | **The map**: every building and every unit seen (below) |
| `data/events.json` | Every unit ever seen, with the full listing as the site gave it, and every change and removal |
| `data/run_stats/<date>.json` | One per morning: when each unit was posted (to within 0.25 s, against the site's own clock), how long it stayed up, how the listings API behaved |
| `data/applications.json` | Each application: result and timeline (when sent, when the site received it) |
| `data/apply_runs/` | Recordings of trips through the form (only while auto-apply is on) |
| `data/floorplans/` | Each layout's floorplan (the site's SVG and a PNG copy), and `index.json` |
| `data/apartment_atlas.json` | Each line of apartments seen (same building + letter = stacked): layout, floors, rents, Oval views, which way it faces |
| `data/reference/` | `buildings.json` (from OpenStreetMap: outlines, which way each wall faces and what it looks at) and `line_overrides.json` (your own notes) |

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
| Variable `AUTO_APPLY_RULES` | Rent limits by bedrooms, $/month, inclusive: `1:2200, 2:2700` (`studio:` works too). Decide which units alert, even with auto-apply off |
| Variable `AUTO_APPLY_MAX_RENT` | One limit, e.g. `3000`: for every unit, or for sizes `AUTO_APPLY_RULES` doesn't list |
| Variable `AUTO_APPLY_MODE` | `on` or `off` |
| Variables `DREAM_RULES`, `DREAM_FLOORS` | Optional. What counts as a dream unit; unset means `1:2200, 2:2700` and `5-10` |
| Secret `APPLICANT_PROFILE` | Your details as one JSON document. `applicant_profile.example.json` shows every key. Keep your own copy: GitHub never shows a secret again |
| Secrets `PUSHOVER_TOKEN`, `PUSHOVER_USER` | Your Pushover app token and user key |

With auto-apply on, it applies **at most once per unit per day**. It goes
straight to StuyTown's application API (about 0.05 s after the unit appears)
once it has checked its request against the site's own form, and otherwise
fills the form in a browser (about 1 s). After applying, finish the
*detailed* application that StuyTown emails you **within 24 hours**.

## Dream units and the map

A **dream unit** is at or under `DREAM_RULES`, on a `DREAM_FLOORS` floor,
listed with **Oval views**, with its Oval windows facing **E round to SW**,
which means a building on the north side of the Oval. Dream units are only
highlighted (alert, summary, map). Auto-apply still follows `AUTO_APPLY_RULES`.
Ratings: **dream**, **possible dream** (unsure which way it faces), **almost**
(one thing missing; it says what), **great light** (sunny and away from the
streets, e.g. Peter Cooper Village), **price match**.

Which way windows face comes from OpenStreetMap's building outlines. Every
building is lined up with the street grid, so a window faces SSW (most sun),
ESE (morning sun), WNW (late sun) or NNE (almost none). A unit listed with
Oval views faces its building's Oval wall, and so does every apartment on
its line. If you learn a line's direction (at a viewing, say), add it to
`data/reference/line_overrides.json`.

**The map:** open [`data/units.geojson`](data/units.geojson) on github.com and
GitHub draws it. You can also use the geojson.io link on the summary page,
which is easier on a phone. Tap anything for details.

| On the map | Means |
| --- | --- |
| Gold building / orange wall | Looks at the Oval, sunny side: the dream buildings |
| Blue building / blue wall | Looks at the Oval, shady side |
| Green building / green wall | Sunny walls away from the streets |
| Grey building | No sunny, quiet walls |
| Gold star, orange star, purple, green, blue, grey dot | A unit: dream, possible dream, almost, great light, price match, the rest |

The buildings are fetched once, on the first morning (or press **Run
workflow → Refresh the map instead**). Until then, the map shows units as
dots on their buildings.

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
alerts instead* and *Test auto-apply instead*. It also has *Refresh the map
instead*.

## Files

| File | Job |
| --- | --- |
| `watch_loop.py` | The 7–10am run: the checking schedule, starting everything, commits |
| `check_units.py` | Fetches the listings, spots changes, picks alerts, logs events |
| `auto_apply.py` | The applier: browsers, the form, results |
| `direct_submit.py` | Learns the site's application request and sends it straight to its API |
| `apply_recorder.py` | Records each trip through the form, and redacts it |
| `run_stats.py`, `summary.py` | The morning's stats, and the summary page |
| `dream_units.py` | Dream-unit ratings, which way a unit faces, the apartment atlas |
| `reference_data.py` | Fetches the buildings (OpenStreetMap) and floorplans, and works out each wall's direction and view |
| `stuytown_geo.py` | The street grid, zones, the walk to the L, and the sun (NOAA's model, as suncalc.org uses) |
| `units_map.py` | Builds `data/units.geojson` |
| `background.py` | The background workers that send alerts and write logs |
| `.github/workflows/watch.yml` | The GitHub Actions workflow |

Map data © OpenStreetMap contributors (ODbL).
