"""
StuyTown / Peter Cooper Village Affordable Housing Watcher -- core logic
------------------------------------------------------------------------
Fetches the real affordable-housing.stuytown.com listings API, compares it
with what was there on the previous check, and sends Pushover alerts when
something changed. watch_loop.py runs this every 15 seconds from 7-10am ET
(see .github/workflows/watch.yml); tests/ runs the exact same code on fake
data.

The state -- data/last_seen.json -- holds every unit currently listed, with
the exact data the API last returned for it. On each check:

  NEW unit (its ID isn't in the state)
      -> Emergency alert (bypasses Do Not Disturb) with a link straight to
         the unit's page so you can apply, plus an events.json entry with
         the unit's full metadata and a screenshot of the listings page.
  SAME unit, SAME data
      -> nothing. This is what stops a listing from alerting every 15s.
  SAME unit, DIFFERENT data (rent, available date, income requirement...)
      -> one normal-priority "updated" alert + an events.json entry.
  Unit GONE
      -> only counts as removed once it's been missing for
         REMOVAL_CONFIRM_POLLS checks in a row (~1 minute), so a one-off
         API hiccup can't make a listing you already know about alert again.
         Then: a quiet alert + events.json entry, and it leaves the state --
         so if it's ever re-listed, it alerts as NEW again.

There's no silent "baseline" run: if units are already listed the first time
this runs (no state file yet), you get alerted about them.

Run this file directly to print what's listed right now (read-only -- no
alerts, no state changes):
    python check_units.py
"""

import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Confirmed live and working -- no authentication required (see README).
BASE_URL = "https://units.stuytown.com/api/ah-units"
ITEMS_PER_PAGE = 21  # matches what the site's own frontend requests
MAX_PAGES = 20  # safety stop; there's normally just one page

SITE_URL = "https://affordable-housing.stuytown.com"
LISTINGS_URL = f"{SITE_URL}/apartments/"
# Each unit's own page (with the Apply button) -- the same link the site's
# DETAILS button on a listing card goes to.
UNIT_PAGE_URL = f"{SITE_URL}/apartments/units"

STATE_FILE = "data/last_seen.json"
EVENTS_FILE = "data/events.json"
SCREENSHOT_DIR = "screenshots"

PUSHOVER_API = "https://api.pushover.net/1"
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")  # application API token
PUSHOVER_USER = os.environ.get("PUSHOVER_USER")  # your personal user key
PUSHOVER_MESSAGE_LIMIT = 1024  # characters; Pushover rejects longer messages
TITLE_PREFIX = ""  # tests set "[TEST] " so simulated alerts are obvious on your phone
# Emergency priority re-alerts every RETRY seconds until you tap Acknowledge,
# for at most EXPIRE seconds.
EMERGENCY_RETRY_SECONDS = 60
EMERGENCY_EXPIRE_SECONDS = 3600

# A unit has to be missing from this many checks in a row (~1 minute at one
# check every 15s) before it counts as removed.
REMOVAL_CONFIRM_POLLS = 4
# Fields that can change without anything you'd care about changing.
IGNORED_FIELDS = {"version"}
# At most this many "updated" alerts per unit per run (one run = one
# morning). Safety net in case some field turns out to change on every
# response, which would otherwise mean an alert -- and a commit -- every 15s.
MAX_UPDATE_ALERTS_PER_UNIT = 3
_update_alerts_sent = Counter()


class PushoverRejected(RuntimeError):
    """Pushover refused the request (bad token/user key or invalid field) --
    retrying won't help, unlike a network error."""


@dataclass
class Changes:
    """What one check found, compared with the previous check."""

    listed: int = 0  # units in this API response
    new: list = field(default_factory=list)  # unit dicts
    updated: list = field(default_factory=list)  # {"before", "after", "fields"}
    removed: list = field(default_factory=list)  # state records {"first_seen_utc", "data", ...}

    def __bool__(self) -> bool:
        return bool(self.new or self.updated or self.removed)

    def kinds(self) -> list:
        return [kind for kind in ("new", "updated", "removed") if getattr(self, kind)]

    def summary(self) -> str:
        if not self:
            return "no change"
        parts = []
        if self.new:
            parts.append("NEW: " + "; ".join(unit_label(u) for u in self.new))
        if self.updated:
            parts.append("UPDATED: " + "; ".join(
                f"{unit_label(c['after'])} ({', '.join(c['fields'])})" for c in self.updated
            ))
        if self.removed:
            parts.append("REMOVED: " + "; ".join(unit_label(r["data"]) for r in self.removed))
        return " | ".join(parts)


# ---------------------------------------------------------------- fetching


def fetch_all_units() -> list:
    """Fetch every page of unit listings. There's normally just one page,
    but this loops in case more units ever get posted than fit on one."""
    units = []
    for page in range(MAX_PAGES):
        url = f"{BASE_URL}?page={page}&itemsOnPage={ITEMS_PER_PAGE}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; personal-housing-watcher/1.0)",
                "Accept": "application/json",
                # Matches what a real browser sends from the affordable
                # housing page -- harmless to include, cheap insurance
                # against any origin-based filtering.
                "Referer": f"{SITE_URL}/",
                "Origin": SITE_URL,
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)

        page_units = data.get("unitModels") if isinstance(data, dict) else None
        if not isinstance(page_units, list):
            # Never read an error/maintenance response as "zero units" --
            # that would make every listed unit look like it was taken down.
            raise ValueError(f"Unexpected API response: {json.dumps(data)[:200]}")
        units.extend(page_units)

        total = data.get("totalCount") or len(units)
        if len(units) >= total or not page_units:
            break
    return units


# ------------------------------------------------------ describing a unit


def unit_id(unit: dict) -> str:
    # unitSpk is the API's own unique key for a unit (e.g.
    # "P~NYST31~B~287~U~0T-A"). The fallbacks are defensive, so nothing
    # crashes if the schema ever changes.
    return str(
        unit.get("unitSpk")
        or unit.get("id")
        or unit.get("name")
        or unit.get("unitNumber")
        or json.dumps(unit, sort_keys=True)
    )


def apartment(unit: dict) -> str:
    # The affordable site shows "Apt {name}"; the market-rate API calls the
    # same thing unitNumber.
    return str(unit.get("name") or unit.get("unitNumber") or "?")


def address(unit: dict) -> str:
    # "440 EAST 23RD STREET" -> "440 East 23rd Street" (str.title() would give "23Rd")
    building = unit.get("building") or {}
    return " ".join(word.capitalize() for word in str(building.get("address") or "").split())


def unit_label(unit: dict) -> str:
    """'Apt 5A, 287 Avenue C'"""
    addr = address(unit)
    return f"Apt {apartment(unit)}" + (f", {addr}" if addr else "")


def unit_url(unit: dict) -> str:
    """Link to the unit's own page, where the Apply button is."""
    spk = unit.get("unitSpk")
    if not spk:
        return LISTINGS_URL
    return f"{UNIT_PAGE_URL}?unitSpk={urllib.parse.quote(str(spk), safe='~')}"


def unit_details(unit: dict) -> str:
    """'2 bed / 1 bath, $1,873/mo, min. income $67,428, available Nov 1'"""
    parts = []
    beds, baths = unit.get("bedrooms"), unit.get("bathrooms")
    if beds is not None:
        rooms = "Studio" if beds == 0 else f"{beds} bed"
        if baths:
            rooms += f" / {baths} bath"
        parts.append(rooms)
    price = _money(unit.get("price"))
    if price:
        parts.append(f"{price}/mo")
    income = _money(unit.get("incomeRequirement"))
    if income:
        parts.append(f"min. income {income}")
    available = _available(unit.get("availableDate"))
    if available:
        parts.append(available)
    return ", ".join(parts)


def unit_summary(unit: dict, include_data: bool = False) -> dict:
    """The events.json view of a unit. include_data adds the full API object."""
    summary = {
        "unit_id": unit_id(unit),
        "apartment": apartment(unit),
        "address": address(unit),
        "bedrooms": unit.get("bedrooms"),
        "bathrooms": unit.get("bathrooms"),
        "price": unit.get("price"),
        "income_requirement": unit.get("incomeRequirement"),
        "available_date": unit.get("availableDate"),
        "url": unit_url(unit),
    }
    if include_data:
        summary["data"] = unit
    return summary


def _money(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _available(value) -> str:
    if not value:
        return ""
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return f"available {value}"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when <= datetime.now(timezone.utc):
        return "available now"
    return f"available {when:%b} {when.day}"


# --------------------------------------------------------- state & diffing


def load_state() -> dict:
    """{unit_id: {"first_seen_utc", "missing_polls", "data"}} -- empty if
    there's no state file yet."""
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f).get("units", {})


def save_state(units_state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"units": units_state}, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_FILE)  # all-or-nothing, so a crash can't leave half a file


def changed_fields(before: dict, after: dict) -> list:
    # Compares parsed values, so a reordered JSON key isn't a "change".
    keys = (set(before) | set(after)) - IGNORED_FIELDS
    return sorted(k for k in keys if before.get(k) != after.get(k))


def diff_units(previous: dict, units: list, now_utc: str) -> tuple:
    """Compare one API response with the saved state. Pure function: returns
    (next_state, Changes) and touches nothing else."""
    current = {}
    for unit in units:
        current.setdefault(unit_id(unit), unit)  # a unit repeated across pages counts once

    changes = Changes(listed=len(current))
    next_state = {}
    for uid, unit in current.items():
        record = previous.get(uid)
        if record is None:
            changes.new.append(unit)
            next_state[uid] = {"first_seen_utc": now_utc, "missing_polls": 0, "data": unit}
            continue
        fields = changed_fields(record["data"], unit)
        if fields:
            changes.updated.append({"before": record["data"], "after": unit, "fields": fields})
        # Seen again, so any "missing" streak from an API hiccup is forgiven.
        next_state[uid] = {**record, "missing_polls": 0, "data": unit}

    for uid, record in previous.items():
        if uid in current:
            continue
        missing = record.get("missing_polls", 0) + 1
        if missing >= REMOVAL_CONFIRM_POLLS:
            changes.removed.append(record)  # dropped from the state
        else:
            next_state[uid] = {**record, "missing_polls": missing}

    return next_state, changes


def process_snapshot(units: list, take_screenshot=None, now: datetime | None = None,
                     label: str = "") -> Changes:
    """Compare one API response with the saved state and act on what changed:
    alert, save the state, take a screenshot, log events. take_screenshot is
    a function(path) -> bool, called only when new units appear."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    previous = load_state()
    next_state, changes = diff_units(previous, units, stamp)
    muted = [c for c in changes.updated
             if _update_alerts_sent[unit_id(c["after"])] >= MAX_UPDATE_ALERTS_PER_UNIT]
    changes.updated = [c for c in changes.updated if c not in muted]
    for change in changes.updated:
        _update_alerts_sent[unit_id(change["after"])] += 1
    print(f"[{label or stamp}] {changes.listed} listed - {changes.summary()}")
    if muted:
        print(f"   (not alerting: {', '.join(unit_label(c['after']) for c in muted)} already "
              f"changed {MAX_UPDATE_ALERTS_PER_UNIT}+ times this run)")

    alert_sent = False
    if changes.new:
        # The alert that matters, sent before anything else. If it fails this
        # raises BEFORE the state is saved, so the next check (15s later) sees
        # the same units as new and tries again -- a Pushover hiccup can delay
        # this alert, but never swallow it.
        alert_sent = notify(**new_units_alert(changes.new))
        if alert_sent:
            print(f"   Emergency alert sent for {len(changes.new)} new unit(s)")

    if next_state != previous:
        save_state(next_state)

    if changes.new:
        screenshot = None
        if take_screenshot:
            path = f"{SCREENSHOT_DIR}/{build_screenshot_filename(now, changes.new)}"
            try:
                if take_screenshot(path):
                    screenshot = path
                    print(f"   Screenshot saved: {path}")
            except Exception as e:
                print(f"   Screenshot failed (alert already sent, not critical): {e}")
        record_event({
            "event": "new",
            "detected_at_utc": stamp,
            "unit_count": len(changes.new),
            "units": [unit_summary(u, include_data=True) for u in changes.new],
            "screenshot": screenshot,
            "alert_sent": alert_sent,
        })

    # Lower-priority alerts are best-effort: the state is already saved, so
    # a failure here is logged rather than retried.
    if changes.updated:
        record_event({
            "event": "updated",
            "detected_at_utc": stamp,
            "units": [
                {
                    **unit_summary(c["after"]),
                    "changed": {
                        name: {"before": c["before"].get(name), "after": c["after"].get(name)}
                        for name in c["fields"]
                    },
                }
                for c in changes.updated
            ],
        })
        _notify_best_effort(updated_alert(changes.updated))

    if changes.removed:
        record_event({
            "event": "removed",
            "detected_at_utc": stamp,
            "units": [
                {**unit_summary(r["data"]), "first_seen_utc": r.get("first_seen_utc")}
                for r in changes.removed
            ],
        })
        _notify_best_effort(removed_alert(changes.removed, now))

    return changes


def build_screenshot_filename(now: datetime, new_units: list) -> str:
    # Apartment numbers rather than the long internal unitSpk keys, and no
    # ":" in the timestamp (not allowed in Windows filenames).
    apartments = ",".join(sorted(apartment(u) for u in new_units))
    cleaned = "".join(c if c.isalnum() or c in "-_," else "_" for c in apartments)[:80]
    stamp = now.strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}_{cleaned}.png" if cleaned else f"{stamp}.png"


def record_event(event: dict) -> None:
    """Append one entry to events.json -- never overwrites past entries, so
    this builds up a running history."""
    events = []
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, encoding="utf-8") as f:
            events = json.load(f)
    events.append(event)
    os.makedirs(os.path.dirname(EVENTS_FILE) or ".", exist_ok=True)
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)
        f.write("\n")


# ------------------------------------------------------------------ alerts


def new_units_alert(units: list) -> dict:
    if len(units) == 1:
        title = "New StuyTown affordable unit - apply now"
        url, url_title = unit_url(units[0]), "Open this unit to apply"
    else:
        title = f"{len(units)} new StuyTown affordable units - apply now"
        url, url_title = LISTINGS_URL, "Open all listings to apply"
    message = _fit([_linked_line(u) for u in units],
                   footer="Each unit closes after 3 applications.")
    return {"title": title, "message": message, "url": url, "url_title": url_title, "priority": 2}


def updated_alert(updates: list) -> dict:
    lines = []
    for change in updates:
        what = ", ".join(_describe_change(name, change["before"].get(name), change["after"].get(name))
                         for name in change["fields"])
        lines.append(f"{_linked_line(change['after'])}\nChanged: {html.escape(what)}")
    one = len(updates) == 1
    return {
        "title": "StuyTown listing updated" if one else f"{len(updates)} StuyTown listings updated",
        "message": _fit(lines),
        "url": unit_url(updates[0]["after"]) if one else LISTINGS_URL,
        "url_title": "Open this unit" if one else "Open all listings",
        "priority": 0,
    }


def removed_alert(records: list, now: datetime) -> dict:
    lines = []
    for record in records:
        listed_for = _listed_for(record.get("first_seen_utc"), now)
        lines.append(html.escape(unit_label(record["data"]) + (f" - {listed_for}" if listed_for else "")))
    one = len(records) == 1
    return {
        "title": "StuyTown unit no longer listed" if one else f"{len(records)} StuyTown units no longer listed",
        "message": _fit(lines),
        "url": LISTINGS_URL,
        "url_title": "Open all listings",
        "priority": -1,  # quiet: shows up, no sound
    }


def _linked_line(unit: dict) -> str:
    line = f'<a href="{html.escape(unit_url(unit))}">{html.escape(unit_label(unit))}</a>'
    details = unit_details(unit)
    return f"{line} - {html.escape(details)}" if details else line


def _describe_change(name: str, before, after) -> str:
    if isinstance(before, (dict, list)) or isinstance(after, (dict, list)):
        return name
    if name in ("price", "incomeRequirement"):
        before, after = _money(before) or before, _money(after) or after
    return f"{name} {before} to {after}"


def _listed_for(first_seen_utc, now: datetime) -> str:
    try:
        start = datetime.strptime(first_seen_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    minutes = max(0, int((now - start).total_seconds() // 60))
    if minutes < 60:
        return f"was listed for {minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 48:
        return f"was listed for {hours}h {mins:02d}m"
    return f"was listed for {hours // 24} days"


def _fit(lines: list, footer: str = "") -> str:
    """Join lines into one message under Pushover's length limit, replacing
    whatever doesn't fit with '+N more on the listings page'."""
    shown = []
    for i, line in enumerate(lines):
        hidden_after = len(lines) - i - 1
        candidate = shown + [line]
        if hidden_after:
            candidate.append(f"+{hidden_after} more on the listings page")
        if footer:
            candidate.append(footer)
        if len("\n".join(candidate)) > PUSHOVER_MESSAGE_LIMIT:
            break
        shown.append(line)
    hidden = len(lines) - len(shown)
    if hidden:
        shown.append(f"+{hidden} more on the listings page")
    if footer:
        shown.append(footer)
    return "\n".join(shown)


def notify(title: str, message: str, *, priority: int = 0, url: str | None = None,
           url_title: str | None = None) -> bool:
    """Send one Pushover notification (HTML-formatted, so unit names are
    tappable links). Returns True once Pushover has accepted it, or False if
    the credentials aren't set -- a dry run that prints the alert instead.
    Raises if it couldn't be delivered."""
    title = (TITLE_PREFIX + title)[:250]
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        print(f"   [dry run: PUSHOVER_TOKEN/PUSHOVER_USER not set] would send priority {priority} alert:")
        print(f"      {title}")
        for line in message.splitlines():
            print(f"      | {line}")
        if url:
            print(f"      link: {url_title or url} -> {url}")
        return False

    fields = {
        "token": PUSHOVER_TOKEN,
        "user": PUSHOVER_USER,
        "title": title,
        "message": message[:PUSHOVER_MESSAGE_LIMIT],
        "html": 1,
        "priority": priority,
        # No "sound" set on purpose -- pick your Emergency-priority sound in
        # the Pushover app itself (Settings -> sounds).
    }
    if url:
        fields["url"] = url[:512]
    if url_title:
        fields["url_title"] = url_title[:100]
    if priority == 2:
        # Emergency: bypasses silent mode / Do Not Disturb and repeats until
        # you acknowledge it. Pushover requires retry + expire with it.
        fields["retry"] = EMERGENCY_RETRY_SECONDS
        fields["expire"] = EMERGENCY_EXPIRE_SECONDS
    _pushover_post("messages.json", fields)
    return True


def _notify_best_effort(alert: dict) -> None:
    try:
        notify(**alert)
    except Exception as e:
        print(f"   WARNING: couldn't send '{alert['title']}' alert: {e}")


def validate_pushover_credentials() -> str | None:
    """Ask Pushover whether the token and user key are valid -- sends nothing
    to your phone. Returns what's wrong, or None if they're fine (or Pushover
    couldn't be reached, which isn't a configuration problem)."""
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        return "PUSHOVER_TOKEN / PUSHOVER_USER are not set"
    try:
        _pushover_post("users/validate.json", {"token": PUSHOVER_TOKEN, "user": PUSHOVER_USER})
    except PushoverRejected as e:
        return str(e)
    except RuntimeError as e:
        print(f"Couldn't reach Pushover to validate the credentials (continuing anyway): {e}")
    return None


def _pushover_post(endpoint: str, fields: dict, attempts: int = 3) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    error = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(f"{PUSHOVER_API}/{endpoint}", data=body, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                detail = e.read().decode(errors="replace")[:300]
                raise PushoverRejected(f"Pushover rejected the request (HTTP {e.code}): {detail}") from None
            error = e
        except OSError as e:  # network trouble (URLError, timeouts, resets)
            error = e
        if attempt < attempts:
            time.sleep(2 * attempt)
    raise RuntimeError(f"Couldn't reach Pushover after {attempts} attempts: {error}")


def main() -> None:
    units = fetch_all_units()
    print(f"{len(units)} unit(s) listed right now at {LISTINGS_URL}")
    for unit in units:
        print(f" - {unit_label(unit)}: {unit_details(unit)}\n   {unit_url(unit)}")


if __name__ == "__main__":
    main()
