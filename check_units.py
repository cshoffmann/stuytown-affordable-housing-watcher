"""
StuyTown / Peter Cooper Village Affordable Housing Watcher
------------------------------------------------------------
Polls the real affordable-housing.stuytown.com listings API and sends an
Emergency-priority Pushover notification (bypasses silent/Do Not Disturb)
the moment a new unit appears. When that happens, it also appends an entry
to events.json (timestamp, unit count, bedroom breakdown) recording what
was found.

Meant to run under GitHub Actions on a schedule -- see
.github/workflows/check.yml. Runs every 5 minutes but only actually checks
between 7:00am-10:00am Eastern time; outside that window it exits
immediately without hitting the network.
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Confirmed live and working -- no authentication required (see README).
BASE_URL = "https://units.stuytown.com/api/ah-units"
ITEMS_PER_PAGE = 21  # matches what the site's own frontend requests

STATE_FILE = "data/last_seen.json"
EVENTS_FILE = "data/events.json"
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")  # application API token
PUSHOVER_USER = os.environ.get("PUSHOVER_USER")  # your personal user key

WINDOW_START_HOUR = 7   # 7:00am ET, inclusive
WINDOW_END_HOUR = 10    # 10:00am ET, exclusive


def within_window() -> bool:
    now_et = datetime.now(ZoneInfo("America/New_York"))
    return WINDOW_START_HOUR <= now_et.hour < WINDOW_END_HOUR


def fetch_all_units() -> list:
    """Fetch every page of unit listings. There's normally just one page,
    but this loops in case more units ever get posted than fit on one."""
    units = []
    page = 0
    while True:
        url = f"{BASE_URL}?page={page}&itemsOnPage={ITEMS_PER_PAGE}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; personal-housing-watcher/1.0)",
                "Accept": "application/json",
                # Matches what a real browser sends from the affordable
                # housing page -- harmless to include, cheap insurance
                # against any origin-based filtering.
                "Referer": "https://affordable-housing.stuytown.com/",
                "Origin": "https://affordable-housing.stuytown.com",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)

        page_units = data.get("unitModels", [])
        units.extend(page_units)

        total = data.get("totalCount", len(units))
        if len(units) >= total or not page_units:
            break
        page += 1

    return units


def unit_id(unit: dict) -> str:
    # Defensive: try the field names the market-rate StuyTown API uses,
    # fall back to the whole object so nothing crashes if the affordable
    # site's schema differs slightly once real units show up.
    return str(
        unit.get("unitSpk")
        or unit.get("id")
        or unit.get("unitNumber")
        or json.dumps(unit, sort_keys=True)
    )


def load_last_seen():
    if not os.path.exists(STATE_FILE):
        return None  # signals "first run ever"
    with open(STATE_FILE) as f:
        return set(json.load(f).get("unit_ids", []))


def save_last_seen(ids) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump({"unit_ids": sorted(ids)}, f)


def notify(message: str) -> None:
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        print("PUSHOVER_TOKEN/PUSHOVER_USER not set -- skipping notification. Message was:", message)
        return
    data = urllib.parse.urlencode(
        {
            "token": PUSHOVER_TOKEN,
            "user": PUSHOVER_USER,
            "title": "StuyTown affordable unit alert",
            "message": message,
            # Priority 2 = Emergency: bypasses silent mode/Do Not Disturb and
            # keeps re-alerting until you acknowledge it or it expires.
            # Required alongside priority=2: retry (seconds between repeats,
            # 30 min) and expire (total seconds before giving up, 10800 max).
            "priority": 2,
            "retry": 60,
            "expire": 3600,
            # No "sound" set on purpose -- set your preferred Emergency-
            # priority sound in the Pushover app itself (Settings -> sounds),
            # so you control it from your phone rather than from this code.
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=data,
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


def build_screenshot_filename(timestamp: str, new_units: list) -> str:
    # Use the human-readable apartment number for the filename, not the
    # long internal unitSpk key -- unit_id() is still what's used for the
    # actual diffing logic, this is purely cosmetic.
    numbers = [str(u.get("unitNumber") or unit_id(u)) for u in new_units]
    ids_str = ",".join(sorted(numbers))
    cleaned = "".join(c if c.isalnum() or c in "-_," else "_" for c in ids_str)
    suffix = f"_{cleaned}" if cleaned else ""
    return f"{timestamp}{suffix}.png"


def record_event(timestamp: str, new_units: list, screenshot_filename: str) -> None:
    """Append one entry to events.json -- never overwrites past entries,
    so this builds up a running history rather than just the latest count."""
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE) as f:
            events = json.load(f)
    else:
        events = []

    events.append(
        {
            "detected_at_utc": timestamp,
            "unit_count": len(new_units),
            "units": [
                {
                    "unit_id": unit_id(u),
                    "unit_number": u.get("unitNumber"),
                    # Confirmed field name via the sibling market-rate API
                    # (units.stuytown.com/api/units); worth double-checking
                    # against the first real affordable listing that appears.
                    "bedrooms": u.get("bedrooms"),
                }
                for u in new_units
            ],
            "screenshot": f"screenshots/{screenshot_filename}",
        }
    )

    os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
    with open(EVENTS_FILE, "w") as f:
        json.dump(events, f, indent=2)


def set_github_output(name: str, value: str) -> None:
    # Lets a later, conditional workflow step (the screenshot step) know
    # whether it should run. No-ops when not running under GitHub Actions
    # (e.g. during local testing), so this is always safe to call.
    output_file = os.environ.get("GITHUB_OUTPUT")
    if not output_file:
        return
    with open(output_file, "a") as f:
        f.write(f"{name}={value}\n")


def main() -> None:
    # Prints on every run, in-window or not, so you can confirm GitHub's
    # secrets are actually wired up by reading any run's log -- without
    # needing to wait for a real unit to appear and actually trigger notify().
    print(f"Pushover credentials loaded: {bool(PUSHOVER_TOKEN and PUSHOVER_USER)}")

    if not within_window():
        print("Outside the 7-10am ET window -- skipping this run.")
        set_github_output("new_unit_found", "false")
        return

    units = fetch_all_units()
    current_ids = {unit_id(u) for u in units}
    previous_ids = load_last_seen()

    save_last_seen(current_ids)

    if previous_ids is None:
        print(f"Baseline run -- {len(current_ids)} unit(s) currently listed. Not notifying.")
        set_github_output("new_unit_found", "false")
        return

    new_ids = current_ids - previous_ids
    if new_ids:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        new_units = [u for u in units if unit_id(u) in new_ids]
        screenshot_filename = build_screenshot_filename(timestamp, new_units)

        print(f"New unit(s) detected: {new_ids}")
        notify(
            f"{len(new_ids)} new unit(s) just posted at StuyTown/PCV affordable "
            f"housing -- affordable-housing.stuytown.com/apartments/"
        )
        record_event(timestamp, new_units, screenshot_filename)

        set_github_output("new_unit_found", "true")
        set_github_output("new_unit_ids", ",".join(sorted(new_ids)))
        set_github_output("screenshot_filename", screenshot_filename)
    else:
        print("No change.")
        set_github_output("new_unit_found", "false")


if __name__ == "__main__":
    main()
