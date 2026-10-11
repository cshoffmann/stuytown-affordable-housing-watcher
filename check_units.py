"""
StuyTown / Peter Cooper Village Affordable Housing Watcher -- core logic
------------------------------------------------------------------------
Fetches the real affordable-housing.stuytown.com listings API and compares
it with what was there on the previous check. watch_loop.py runs a check
every 2 seconds from 7-10am ET (see .github/workflows/watch.yml); tests/
runs the exact same code on fake data.

The state -- data/last_seen.json -- holds every unit currently listed, with
the exact data the API last returned for it. On each check
(process_snapshot):

  1. The applier (auto_apply.py) gets the listings FIRST, before any alert
     or log is even queued.
  2. NEW unit (its ID isn't in the state)
       qualifies for auto-apply  -> Emergency alert (bypasses Do Not Disturb)
       doesn't                   -> one normal alert
     plus an events.json entry with the unit's full metadata.
  3. SAME unit, DIFFERENT data (rent, available date...) -> events.json only.
  4. Unit GONE -> only counts as removed once it's been missing for
     REMOVAL_CONFIRM_SECONDS (and at least REMOVAL_MIN_MISSED_CHECKS checks),
     so a one-off API hiccup can't make a listing you know about alert
     again. Then an events.json entry, and it leaves the state -- so if it's
     ever re-listed, it alerts as NEW again.

Alerts and the events log are handed to background workers (background.py):
a slow or failing Pushover or disk never delays the next check or an
application.

There's no silent "baseline" run: if units are already listed the first time
this runs (no state file yet), you get alerted about them.

Run this file directly to print what's listed right now (read-only -- no
alerts, no state changes):
    python check_units.py
"""

import gzip
import html
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

import background

# Confirmed live and working -- no authentication required (see README).
BASE_URL = "https://units.stuytown.com/api/ah-units"
ITEMS_PER_PAGE = 21  # matches what the site's own frontend requests
MAX_PAGES = 20  # safety stop; there's normally just one page
# A check every 2 seconds can't wait 20 for a slow answer: give up after this
# and try again on the next check.
REQUEST_TIMEOUT_SECONDS = 4
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; personal-housing-watcher/1.0)",
    "Accept": "application/json",
    "Accept-Encoding": "gzip",
    # Matches what a real browser sends from the affordable housing page --
    # harmless to include, cheap insurance against origin-based filtering.
    "Referer": "https://affordable-housing.stuytown.com/",
    "Origin": "https://affordable-housing.stuytown.com",
}

SITE_URL = "https://affordable-housing.stuytown.com"
LISTINGS_URL = f"{SITE_URL}/apartments/"
# Each unit's own page (with the Apply button) -- the same link the site's
# DETAILS button on a listing card goes to.
UNIT_PAGE_URL = f"{SITE_URL}/apartments/units"

STATE_FILE = "data/last_seen.json"
EVENTS_FILE = "data/events.json"

PUSHOVER_API = "https://api.pushover.net/1"
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN")  # application API token
PUSHOVER_USER = os.environ.get("PUSHOVER_USER")  # your personal user key
PUSHOVER_MESSAGE_LIMIT = 1024  # characters; Pushover rejects longer messages
TITLE_PREFIX = ""  # tests set "[TEST] " so simulated alerts are obvious on your phone
# Emergency priority re-alerts every RETRY seconds until you tap Acknowledge,
# for at most EXPIRE seconds.
EMERGENCY_RETRY_SECONDS = 60
EMERGENCY_EXPIRE_SECONDS = 3600

# A unit counts as removed once it's been missing for this long -- and from
# at least this many checks in a row, whatever the checking pace.
REMOVAL_CONFIRM_SECONDS = 60
REMOVAL_MIN_MISSED_CHECKS = 2
# Fields that can change without anything you'd care about changing.
IGNORED_FIELDS = {"version"}


class PushoverRejected(RuntimeError):
    """Pushover refused the request (bad token/user key or invalid field) --
    retrying won't help, unlike a network error."""


class ListingsUnavailable(RuntimeError):
    """The listings API answered, but not with listings: rate-limited (429),
    refused (403) or a server error. The checker slows down when it sees
    this; retry_after is the API's own Retry-After, in seconds, if it sent one."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


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


class ListingsClient:
    """Fetches the listings over ONE kept-open connection: no new TCP + TLS
    handshake on every check (checks are 2 seconds apart), a short timeout,
    and one silent reconnect if the server closed the idle connection."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = base_url or BASE_URL
        self.timeout = timeout or REQUEST_TIMEOUT_SECONDS
        parts = urllib.parse.urlsplit(self.base_url)
        self._scheme, self._host, self._path = parts.scheme, parts.netloc, parts.path
        self._conn = None
        # What the last fetch_all() saw, one entry per request, for the run's
        # stats (run_stats.py): timing, status, size and the caching headers.
        self.responses = []

    def fetch_all(self) -> list:
        """Every page of unit listings. There's normally just one page, but
        this loops in case more units ever get posted than fit on one."""
        units = []
        self.responses = []
        for page in range(MAX_PAGES):
            data = self._get(f"{self._path}?page={page}&itemsOnPage={ITEMS_PER_PAGE}")
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

    def _get(self, path: str):
        for attempt in (1, 2):
            reused = self._conn is not None
            conn = self._connection()
            started = time.monotonic()
            try:
                conn.request("GET", path, headers=REQUEST_HEADERS)
                response = conn.getresponse()
                body = response.read()
            except (http.client.HTTPException, OSError):
                self.close()
                if attempt == 2:
                    raise
                continue  # a kept-open connection the server had closed: reconnect once
            self.responses.append({
                "ms": round((time.monotonic() - started) * 1000, 1),
                "status": response.status,
                "bytes": len(body),
                "reused_connection": reused,
                "headers": {k.lower(): v for k, v in response.getheaders()},
                "body": body,
            })
            if response.getheader("Connection", "").lower() == "close":
                self.close()
            if response.status != 200:
                raise ListingsUnavailable(f"listings API answered HTTP {response.status}", response.status,
                                          _retry_after(response.getheader("Retry-After")))
            if (response.getheader("Content-Encoding") or "").lower() == "gzip":
                body = gzip.decompress(body)
            return json.loads(body)

    def _connection(self):
        if self._conn is None:
            make = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
            self._conn = make(self._host, timeout=self.timeout)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


def _retry_after(value) -> float | None:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


_client = None


def fetch_all_units() -> list:
    """The listings, through a shared kept-open client (rebuilt if BASE_URL
    changes, as the tests do)."""
    global _client
    if _client is None or _client.base_url != BASE_URL:
        _client = ListingsClient(BASE_URL)
    return _client.fetch_all()


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


def unit_rent(unit: dict) -> float | None:
    """Monthly rent: the listed price, else the cheapest lease-term rate."""
    price = number(unit.get("price"))
    if price is not None:
        return price
    rates = [r for r in map(number, (unit.get("unitRates") or {}).values()) if r is not None]
    return min(rates) if rates else None


def number(value) -> float | None:
    """3040.84, "3,040.84" or "$3,040" -> 3040.84 / 3040.0; None if it isn't one."""
    if isinstance(value, str):
        value = value.replace("$", "").replace(",", "").strip()
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


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
            next_state[uid] = {"first_seen_utc": now_utc, "last_seen_utc": now_utc, "missing_polls": 0, "data": unit}
            continue
        fields = changed_fields(record["data"], unit)
        if fields:
            changes.updated.append({"before": record["data"], "after": unit, "fields": fields})
        # Seen again, so any "missing" streak from an API hiccup is forgiven.
        seen = {k: v for k, v in record.items() if k != "missing_since_utc"}
        # last_seen_utc: the last check it was listed in -- with first_seen_utc,
        # how long it really stayed up (to within one check, not the minute
        # it takes to confirm a removal).
        next_state[uid] = {**seen, "last_seen_utc": now_utc, "missing_polls": 0, "data": unit}

    for uid, record in previous.items():
        if uid in current:
            continue
        missing = record.get("missing_polls", 0) + 1
        since = record.get("missing_since_utc") or now_utc
        if missing >= REMOVAL_MIN_MISSED_CHECKS and _seconds_between(since, now_utc) >= REMOVAL_CONFIRM_SECONDS:
            changes.removed.append(record)  # dropped from the state
        else:
            next_state[uid] = {**record, "missing_polls": missing, "missing_since_utc": since}

    return next_state, changes


def _seconds_between(earlier: str, later: str) -> float:
    try:
        parse = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        return (parse(later) - parse(earlier)).total_seconds()
    except (TypeError, ValueError):
        return 0.0


_INLINE = background.Worker("inline", inline=True)


def process_snapshot(units: list, now: datetime | None = None, label: str = "", apply=None,
                     qualifies=None, notifier=None, logger=None) -> Changes:
    """One check: compare the listings with the saved state, save it, and act
    on what changed -- in order of what matters:

      1. apply(units): the applier gets the listings before anything else
         (auto_apply.Applier.dispatch -- it only queues work, so this is fast,
         and returns the units it took on).
      2. Alerts go to the notifier, and the events log to the logger -- both
         background workers, so neither can delay the next check or an
         application. qualifies(unit) picks Emergency vs normal alerts; with
         no qualifies, every new unit is an Emergency.

    Without notifier/logger (tests, the fake morning) the jobs run inline."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    notifier, logger = notifier or _INLINE, logger or _INLINE
    previous = load_state()
    next_state, changes = diff_units(previous, units, stamp)
    if next_state != previous:
        save_state(next_state)

    queued = []  # the units the applier took on
    if apply:
        try:
            queued = list(apply(units) or [])
        except Exception as e:
            print(f"   WARNING: handing the listings to the applier failed: {e}")

    if changes:
        print(f"[{label or stamp}] {changes.listed} listed - {changes.summary()}")
    if changes.new:
        qualifying = [u for u in changes.new if qualifies is None or _safe_bool(qualifies, u)]
        others = [u for u in changes.new if u not in qualifying]
        if qualifying:
            # "auto-applying now" only when the applier really took it on (it
            # won't, say, for a unit it already applied to before a re-listing).
            taken = {unit_id(u) for u in queued}
            notifier.submit(background.notify_with_retries, notify,
                            qualifying_alert(qualifying, auto_applying=all(unit_id(u) in taken for u in qualifying)))
        if others:
            notifier.submit(background.notify_with_retries, notify, new_units_alert(others))
        logger.submit(record_event, {
            "event": "new",
            "detected_at_utc": stamp,
            "unit_count": len(changes.new),
            "units": [{**unit_summary(u, include_data=True), "qualifies_for_auto_apply": u in qualifying}
                      for u in changes.new],
        })
    if changes.updated:
        logger.submit(record_event, {
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
    if changes.removed:
        logger.submit(record_event, {
            "event": "removed",
            "detected_at_utc": stamp,
            "units": [
                {**unit_summary(r["data"]), "first_seen_utc": r.get("first_seen_utc"),
                 "last_seen_utc": r.get("last_seen_utc"),
                 "listed_for_seconds": _seconds_between(r.get("first_seen_utc"), r.get("last_seen_utc"))
                 if r.get("last_seen_utc") else None}
                for r in changes.removed
            ],
        })
    return changes


def _safe_bool(fn, unit) -> bool:
    try:
        return bool(fn(unit))
    except Exception:
        return True  # when in doubt, the louder alert


def record_event(event: dict) -> None:
    """Append one entry to events.json -- never overwrites past entries, so
    this builds up a running history."""
    events = []
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, encoding="utf-8") as f:
            events = json.load(f)
    events.append(event)
    os.makedirs(os.path.dirname(EVENTS_FILE) or ".", exist_ok=True)
    tmp = EVENTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)
        f.write("\n")
    os.replace(tmp, EVENTS_FILE)  # all-or-nothing: a commit can't catch half a file


# ------------------------------------------------------------------ alerts


def qualifying_alert(units: list, auto_applying: bool) -> dict:
    """Emergency: a unit at or under your rent limit. The only alert that
    breaks through Do Not Disturb."""
    action = "auto-applying now" if auto_applying else "apply now"
    if len(units) == 1:
        title = f"Qualifying StuyTown unit - {action}"
        url, url_title = unit_url(units[0]), "Open this unit"
    else:
        title = f"{len(units)} qualifying StuyTown units - {action}"
        url, url_title = LISTINGS_URL, "Open all listings"
    footer = ("Auto-apply is on: you'll get the result in a separate message."
              if auto_applying else "Each unit closes after 3 applications.")
    message = _fit([_linked_line(u) for u in units], footer=footer)
    return {"title": title, "message": message, "url": url, "url_title": url_title, "priority": 2}


def new_units_alert(units: list) -> dict:
    """Normal priority: new units that don't qualify (over your rent limit or
    above your income)."""
    if len(units) == 1:
        title = "New StuyTown unit (doesn't qualify)"
        url, url_title = unit_url(units[0]), "Open this unit"
    else:
        title = f"{len(units)} new StuyTown units (don't qualify)"
        url, url_title = LISTINGS_URL, "Open all listings"
    message = _fit([_linked_line(u) for u in units], footer="Each unit closes after 3 applications.")
    return {"title": title, "message": message, "url": url, "url_title": url_title, "priority": 0}


def _linked_line(unit: dict) -> str:
    line = f'<a href="{html.escape(unit_url(unit))}">{html.escape(unit_label(unit))}</a>'
    details = unit_details(unit)
    return f"{line} - {html.escape(details)}" if details else line


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
           url_title: str | None = None, attachment: tuple | None = None) -> bool:
    """Send one Pushover notification (HTML-formatted, so unit names are
    tappable links). Returns True once Pushover has accepted it, or False if
    the credentials aren't set -- a dry run that prints the alert instead.
    Raises if it couldn't be delivered. attachment is an optional image,
    (filename, bytes, mime type), shown in the notification."""
    title = (TITLE_PREFIX + title)[:250]
    if not (PUSHOVER_TOKEN and PUSHOVER_USER):
        print(f"   [dry run: PUSHOVER_TOKEN/PUSHOVER_USER not set] would send priority {priority} alert:")
        print(f"      {title}")
        for line in message.splitlines():
            print(f"      | {line}")
        if url:
            print(f"      link: {url_title or url} -> {url}")
        if attachment:
            print(f"      image: {attachment[0]} ({len(attachment[1]) // 1024} KB)")
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
    _pushover_post("messages.json", fields, attachment=attachment)
    return True


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


def _pushover_post(endpoint: str, fields: dict, attempts: int = 3, attachment: tuple | None = None) -> dict:
    headers = {}
    if attachment:
        body, headers["Content-Type"] = _multipart(fields, attachment)
    else:
        body = urllib.parse.urlencode(fields).encode()
    error = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(f"{PUSHOVER_API}/{endpoint}", data=body, headers=headers, method="POST")
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


def _multipart(fields: dict, attachment: tuple) -> tuple:
    """multipart/form-data body for a Pushover message with an image."""
    filename, data, mime = attachment
    boundary = f"----stuytown-watcher-{os.urandom(8).hex()}"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; filename="{filename}"\r\n'
                 f'Content-Type: {mime}\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def main() -> None:
    units = fetch_all_units()
    print(f"{len(units)} unit(s) listed right now at {LISTINGS_URL}")
    for unit in units:
        print(f" - {unit_label(unit)}: {unit_details(unit)}\n   {unit_url(unit)}")


if __name__ == "__main__":
    main()
