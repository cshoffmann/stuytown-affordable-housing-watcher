"""
Direct submit: send the application form's request straight to StuyTown's
application API -- the same request the site's own SUBMIT sends -- without
opening the unit page, pressing APPLY NOW or filling boxes. On 2026-10-12 the
browser needed ~1.1 s from a unit appearing to SUBMIT, ~0.75 s of it loading
the unit page; this sends the request ~0.05 s after the unit appears.

It's only used while it's PROVEN to send exactly what the site's form would:

  1. The site's own settings for the form are read from a unit page (it's in
     the page: the API address, its key, the success and error messages, the
     field limits) along with the version of the site's code
     (".../aHUnit-js.js.v639262698149509707").
  2. The request the site's form builds is captured -- from a real browser
     application, or from a recording of an over-limit unit's form, filled
     in and SUBMIT pressed with every outgoing request blocked, so nothing
     is sent. From it, the format is learned: how phones and the income are
     written, where the building address comes from, what "version" is.
     Only rule NAMES are saved (data/apply_api.json), never your details.
  3. A direct submit happens only if that format was proven against the same
     version of the site's code, and rebuilding the captured request from
     your profile with it gives exactly the same request. Any change to the
     site's code switches it off until the next capture proves it again.

If the API refuses a direct request or its answer is lost, the browser
applies instead. That's safe: the site itself refuses a second application
to the same apartment ("you have an open application"), so at worst the
browser's attempt is refused.
"""

import http.client
import json
import os
import re
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import check_units

API_FORMAT_FILE = "data/apply_api.json"
CONFIG_MAX_AGE_SECONDS = 30 * 60  # the site's settings are re-read this often
POST_TIMEOUT_SECONDS = 20  # the API took 5.9 s to answer at 7:15 on 2026-10-12
CONNECTION_MAX_AGE_SECONDS = 45  # older kept-open connections are replaced before sending

# The site's code, by its versioned address -- a new version means a new deploy.
SITE_VERSION = re.compile(r"/sb/aHUnit-js\.js\.v(\d+)")
# Every key the site's request has had (2026-10-12), with where its value comes from.
PROFILE_TEXT_KEYS = {  # request key -> profile key, sent as typed
    "firstName": "first_name", "lastName": "last_name", "email": "email",
    "address.apartment": "apartment_no", "address.building": "building", "address.streetName": "street_name",
    "address.city": "city", "address.state": "state", "address.zip": "zip",
}


def _utc_ms() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ------------------------------------------------------------ site settings


def parse_site_config(html: str) -> dict | None:
    """The form's settings from a unit page's HTML ("applyForm": {...}), plus
    the version of the site's code. None if the page doesn't have them."""
    form, start = None, html.find('"applyForm":')
    while start >= 0 and form is None:  # the first mention may be in a comment or text
        try:
            found, _ = json.JSONDecoder().raw_decode(html[start + len('"applyForm":'):].lstrip())
            form = found if isinstance(found, dict) and found.get("apiUrl") else None
        except ValueError:
            pass
        start = html.find('"applyForm":', start + 1)
    if form is None:
        return None
    version = SITE_VERSION.search(html)
    limits = {}
    for name, field in form.items():
        if name.endswith("Field") and isinstance(field, dict):
            limits[name] = {k: field.get(k) for k in ("minLength", "maxLength", "validation") if k in field}
    return {
        "api_url": form["apiUrl"],
        "header_name": form.get("apiUrlHeaderName"),
        "header_value": form.get("apiUrlHeaderValue"),
        "success_message": _plain(form.get("successMessage")),
        "error_message": _plain(form.get("errorMessage")),  # an open application for this apartment already
        "unavailable_message": _plain(form.get("unavailableUnitErrorMessage")),  # 3 applications already
        "field_limits": limits,
        "site_version": version.group(1) if version else None,
        "read_utc": _utc_ms(),
        "_read_at": time.monotonic(),
    }


def _plain(text) -> str | None:
    if not text:
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text))).strip()


def fetch_site_config(unit_spk: str = "") -> dict | None:
    """Read the settings from a unit page (any unit: they're the same on
    every page). Never raises."""
    url = f"{check_units.UNIT_PAGE_URL}?{urllib.parse.urlencode({'unitSpk': unit_spk or 'none'})}"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": check_units.REQUEST_HEADERS["User-Agent"]})
        with urllib.request.urlopen(request, timeout=15) as response:
            return parse_site_config(response.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"   Direct submit: couldn't read the site's form settings ({type(e).__name__}); using the browser")
        return None


# ------------------------------------------------------- the request format

# How a phone number can be written; 1XXXXXXXXXX is the number with its US code.
PHONE_STYLES = {
    "+digits": lambda d: f"+{d}",
    "digits": lambda d: d,
    "national": lambda d: d[1:],
    "spaced": lambda d: f"{d[0]} {d[1:4]} {d[4:7]} {d[7:]}",
    "+spaced": lambda d: f"+{d[0]} {d[1:4]} {d[4:7]} {d[7:]}",
    "dashed": lambda d: f"{d[1:4]}-{d[4:7]}-{d[7:]}",
    "+dashed": lambda d: f"+{d[0]}-{d[1:4]}-{d[4:7]}-{d[7:]}",
    "parens": lambda d: f"({d[1:4]}) {d[4:7]}-{d[7:]}",
}
INCOME_STYLES = {
    "whole": lambda n: f"{n:.0f}",
    "cents": lambda n: f"{n:.2f}",
    "commas+cents": lambda n: f"{n:,.2f}",
    "commas": lambda n: f"{n:,.0f}",
    "number": lambda n: int(n) if n == int(n) else n,
}
HOUSEHOLD_STYLES = {"text": lambda n: str(n), "number": lambda n: n}
# Where the request's "buildingAddress" (the address being applied to) comes from.
ADDRESS_STYLES = {
    "capitalized": check_units.address,  # "330 FIRST AVENUE" -> "330 First Avenue"
    "as listed": lambda unit: str((unit.get("building") or {}).get("address") or ""),
}


def _phone(value) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        digits = "1" + digits
    return digits if len(digits) == 11 and digits.startswith("1") else None


def _flatten(body: dict, prefix: str = "") -> dict:
    flat = {}
    for key, value in body.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _shape(body: dict):
    """The keys in order, nested: ["unitSpk", ..., ["address", [...]], ...]."""
    return [[k, _shape(v)] if isinstance(v, dict) else k for k, v in body.items()]


def _style_for(styles: dict, wanted, make) -> str | None:
    for name, style in styles.items():
        try:
            if make(style) == wanted:
                return name
        except Exception:
            continue
    return None


def learn_format(captured: dict, profile: dict, unit: dict, config: dict | None) -> dict | None:
    """The request format, from the request the site's form built for this
    unit with this profile -- or None if any part of it can't be explained
    (then the browser keeps doing the applying). Never contains a value of
    the profile: only how each one is written."""
    if not isinstance(captured, dict) or not config:
        return None
    flat = _flatten(captured)
    rules = {}
    # The cell phone first: an empty work phone can be sent as a copy of it.
    for key, value in sorted(flat.items(), key=lambda item: item[0] != "cellPhone"):
        if key == "unitSpk":
            if value != check_units.unit_id(unit):
                return None
        elif key in PROFILE_TEXT_KEYS:
            mine = profile.get(PROFILE_TEXT_KEYS[key])
            if str(value) != ("" if mine is None else str(mine).strip()):
                return None
        elif key in ("cellPhone", "workPhone"):
            mine = _phone(profile.get("cell_phone" if key == "cellPhone" else "work_phone"))
            if mine:
                style = _style_for(PHONE_STYLES, value, lambda s: s(mine))
                if style is None:
                    return None
                rules[key] = style
            elif key == "workPhone":  # no work phone of your own: what the form sends instead
                cell = _phone(profile.get("cell_phone"))
                if cell and rules.get("cellPhone") and value == PHONE_STYLES[rules["cellPhone"]](cell):
                    rules[key] = "same as cell"
                elif isinstance(value, str) and len(value) <= 2 and not re.search(r"\d", value):
                    rules[key] = "constant:" + value  # "" or "+"
                else:
                    return None
            else:
                return None
        elif key == "totalAnnualEarnings":
            amount = check_units.number(profile.get("annual_income"))
            style = amount and _style_for(INCOME_STYLES, value, lambda s: s(amount))
            if not style:
                return None
            rules[key] = style
        elif key == "householdSize":
            size = int(check_units.number(profile.get("household_size")) or 0)
            style = size and _style_for(HOUSEHOLD_STYLES, value, lambda s: s(size))
            if not style:
                return None
            rules[key] = style
        elif key == "buildingAddress":
            style = _style_for(ADDRESS_STYLES, value, lambda s: s(unit))
            if style is None:
                return None
            rules[key] = style
        elif key == "version":
            unit_version = unit.get("version")
            if value == unit_version and value != 1:
                rules[key] = "the unit's"
            elif value == 1 and unit_version != 1:
                rules[key] = "always 1"
            elif value == unit_version:
                rules[key] = "the unit's, or always 1"  # can't tell yet: only used while they agree
            else:
                return None
        else:
            return None  # a key the site didn't send before: let the browser handle it
    return {
        "site_version": config.get("site_version"),
        "api_url": config.get("api_url"),
        "shape": _shape(captured),
        "rules": rules,
    }


def merge_formats(old: dict | None, new: dict) -> dict:
    """A new capture can settle what an old one couldn't tell (the "version")."""
    if old and old.get("site_version") == new.get("site_version") and old.get("shape") == new.get("shape"):
        old_rule, new_rule = (old.get("rules") or {}).get("version"), new["rules"].get("version")
        if new_rule == "the unit's, or always 1" and old_rule in ("the unit's", "always 1"):
            new = {**new, "rules": {**new["rules"], "version": old_rule}}
    return new


def build_body(unit: dict, profile: dict, fmt: dict) -> dict | None:
    """The request the site's form would send for this unit, or None when the
    format can't say for sure (then the browser applies)."""
    rules = fmt.get("rules") or {}

    def value_for(key):
        if key == "unitSpk":
            return check_units.unit_id(unit)
        if key in PROFILE_TEXT_KEYS:
            mine = profile.get(PROFILE_TEXT_KEYS[key])
            return "" if mine is None else str(mine).strip()
        if key in ("cellPhone", "workPhone"):
            rule = rules.get(key, "")
            if rule == "same as cell":
                return PHONE_STYLES[rules["cellPhone"]](_phone(profile.get("cell_phone")))
            if rule.startswith("constant:"):
                return rule[len("constant:"):]
            digits = _phone(profile.get("cell_phone" if key == "cellPhone" else "work_phone"))
            if not digits or rule not in PHONE_STYLES:
                raise ValueError(key)
            return PHONE_STYLES[rule](digits)
        if key == "totalAnnualEarnings":
            return INCOME_STYLES[rules[key]](check_units.number(profile.get("annual_income")))
        if key == "householdSize":
            return HOUSEHOLD_STYLES[rules[key]](int(check_units.number(profile.get("household_size")) or 0))
        if key == "buildingAddress":
            return ADDRESS_STYLES[rules[key]](unit)
        if key == "version":
            rule = rules.get(key)
            if rule == "the unit's":
                return unit.get("version")
            if rule == "always 1":
                return 1
            if rule == "the unit's, or always 1" and unit.get("version") == 1:
                return 1
            raise ValueError("version")
        raise ValueError(key)

    def build(shape, prefix=""):
        body = {}
        for item in shape:
            if isinstance(item, list):
                body[item[0]] = build(item[1], f"{prefix}{item[0]}.")
            else:
                body[item] = value_for(f"{prefix}{item}")
        return body

    try:
        return build(fmt.get("shape") or [])
    except (ValueError, KeyError, TypeError):
        return None


def load_format() -> dict | None:
    try:
        with open(API_FORMAT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_format(fmt: dict) -> None:
    os.makedirs(os.path.dirname(API_FORMAT_FILE) or ".", exist_ok=True)
    tmp = API_FORMAT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(fmt, f, indent=2)
        f.write("\n")
    os.replace(tmp, API_FORMAT_FILE)


# --------------------------------------------------------------- the answer


def interpret(status: int, text: str, config: dict | None = None) -> tuple:
    """(our status, detail, the site's own submission time or None) for the
    API's answer to an application. Status: submitted | unconfirmed |
    rejected (the site said no: already applied, or the unit is full) |
    refused (4xx: certainly not created) | unknown (5xx: can't tell)."""
    try:
        answer = json.loads(text) if text else None
    except ValueError:
        answer = None
    submitted_on = answer.get("submittedOn") if isinstance(answer, dict) else None
    if 200 <= status < 300:
        if isinstance(answer, dict) and (str(answer.get("status", "")).lower() == "created" or answer.get("token")):
            return "submitted", "StuyTown's application API accepted it", submitted_on
        return "unconfirmed", f"the API answered {status} without saying it was created", submitted_on
    lowered = (text or "").lower()
    if re.search(r"open application|already (applied|submitted)|duplicate", lowered):
        return "rejected", "the site says you already have an open application for this apartment", None
    if re.search(r"3 other applicants|no longer available|unavailable|not available", lowered):
        return "rejected", "the apartment already has its 3 applications", None
    if status >= 500:
        return "unknown", f"the API failed (HTTP {status}); it may or may not have been created", None
    return "refused", f"the API refused it (HTTP {status})", None


# --------------------------------------------------------------- the client


class ApiClient:
    """One kept-open HTTPS connection to the application API, opened ahead
    of time (warm()) so sending doesn't wait for a TLS handshake."""

    def __init__(self):
        self._lock = threading.Lock()
        self._conn = None
        self._host = None
        self._opened = 0.0

    def warm(self, api_url: str) -> None:
        """Open (or renew) the connection. Never raises."""
        parts = urllib.parse.urlsplit(api_url)
        with self._lock:
            if self._conn is not None and self._host == parts.netloc \
                    and time.monotonic() - self._opened < CONNECTION_MAX_AGE_SECONDS:
                return
            self._close()
            try:
                self._conn = _connection(parts)
                self._conn.connect()
                self._host, self._opened = parts.netloc, time.monotonic()
            except (OSError, http.client.HTTPException):
                self._conn = None

    def post(self, api_url: str, body: dict, headers: dict) -> dict:
        """Send once. Returns {"status", "text", "sent_utc", "answered_utc",
        "seconds", "reused"} -- or {"error", "sent": bool} if it failed,
        where sent=False means the request certainly never left."""
        parts = urllib.parse.urlsplit(api_url)
        payload = json.dumps(body, separators=(",", ":")).encode()
        with self._lock:
            conn, reused = self._conn, self._conn is not None and self._host == parts.netloc \
                and time.monotonic() - self._opened < CONNECTION_MAX_AGE_SECONDS
            self._conn = None  # one request per warmed connection; the next one is warmed again
        if not reused:
            if conn is not None:
                conn.close()
            conn = _connection(parts)
            try:
                conn.connect()
            except (OSError, http.client.HTTPException) as e:
                return {"error": f"couldn't connect: {type(e).__name__}", "sent": False}
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        sent_utc, started = _utc_ms(), time.monotonic()
        try:
            conn.request("POST", path, body=payload, headers={**headers, "Content-Length": str(len(payload))})
            response = conn.getresponse()
            text = response.read().decode("utf-8", "replace")
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            return {"error": f"{type(e).__name__}: {e}"[:200], "sent": True, "sent_utc": sent_utc}
        conn.close()
        return {"status": response.status, "text": text, "sent_utc": sent_utc, "answered_utc": _utc_ms(),
                "seconds": round(time.monotonic() - started, 3), "reused": reused}

    def _close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def close(self):
        with self._lock:
            self._close()


def _connection(parts):
    if parts.scheme == "http":  # the fake site in tests
        return http.client.HTTPConnection(parts.netloc, timeout=POST_TIMEOUT_SECONDS)
    return http.client.HTTPSConnection(parts.netloc, timeout=POST_TIMEOUT_SECONDS,
                                       context=ssl.create_default_context())


def request_headers(fmt: dict, config: dict, unit: dict | None = None) -> dict:
    """The headers the site's form sends, with the API key from the site's
    current settings (and a referring page naming this unit, not the one the
    format was learned from)."""
    headers = {k: v for k, v in (fmt.get("headers") or {}).items()
               if k.lower() not in ("content-length", "host", "cookie") and k.lower() != (
                   config.get("header_name") or "").lower()}
    learned_from = fmt.get("verified_from")
    for name in [k for k in headers if k.lower() == "referer"]:
        if unit is not None and learned_from and learned_from in headers[name]:
            headers[name] = headers[name].replace(learned_from, check_units.unit_id(unit))
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json, text/plain, */*")
    if config.get("header_name") and config.get("header_value"):
        headers[config["header_name"]] = config["header_value"]
    return headers


def captured_headers(headers: dict, config: dict | None) -> dict:
    """The headers of a captured request worth repeating (not the key: that
    always comes from the site's current settings)."""
    skip = {"content-length", "host", "cookie", ((config or {}).get("header_name") or "").lower()}
    return {k: v for k, v in (headers or {}).items() if k.lower() not in skip and not k.startswith(":")}

