"""
Auto-apply: when a listed unit's monthly rent is at or under your limit,
apply for it from your saved applicant profile -- straight to StuyTown's
application API once the request is proven (direct_submit.py), otherwise
by filling in and submitting the form in a browser -- then report the result.

The Applier runs next to the watcher's checker (watch_loop.py), which hands
it every check's listings FIRST, before any alert or log is even queued:

  - APPLY_WORKERS browser workers start at 6:59 with Chromium open and the
    StuyTown site already loaded, so an application never waits for a
    browser to start. Two qualifying units are applied to at the same time.
  - dispatch() only queues work and returns immediately; the checker keeps
    checking every 2 seconds while applications run.
  - The result alert, the applications log and the form recording are handed
    to background workers (background.py). They never delay an application,
    and their failures never reach it.
  - Direct submit: once a request the site's own form built has proven the
    format (a browser application, or a recording that presses SUBMIT with
    every request held back), a qualifying unit's application is sent
    straight to the API, ~0.05 s after the checker sees it. Anything
    unproven, refused or lost goes to a browser instead.

Two repository variables control it:

    AUTO_APPLY_MODE      on | off (default). Off = auto-apply does nothing at all.
    AUTO_APPLY_MAX_RENT  your rent limit in $/month, e.g. 3000 -- required when
                         on; there is no built-in default. Also decides which
                         new units get the Emergency alert.

Your details come from APPLICANT_PROFILE -- the whole JSON document, stored as
ONE GitHub secret -- or, on your own computer, the gitignored
applicant_profile.json. applicant_profile.example.json shows every key.

The form (affordable-housing.stuytown.com, as recorded 2026-10-12) is a dialog
that APPLY NOW opens on the unit page:

    First Name *   Last Name *   Email *   Cell Phone *   Work Phone
    Building *  Street name *  Apartment No.  City *  State  Zip *
    Household Size *   Household Gross Annual Income, $ *        [SUBMIT]

and submitting it gets you an email with a link to the detailed application,
which has to be completed within 24 hours.

Every trip through the form is recorded by apply_recorder.py into
data/apply_runs/ -- steps and timings, the form's fields, the page HTML, the
network traffic including what SUBMIT sends and what comes back -- with your
details redacted. While on, it also records the form of up to
MAX_FORM_RECORDINGS_PER_RUN units over your limit each morning, whenever no
application is running -- filled in, with SUBMIT pressed and every outgoing
request held back in the browser, so nothing is sent.

The repo (and its Actions logs) are public, so nothing personal is ever
printed, committed or logged: in GitHub Actions every profile value is masked
in the log, screenshots of the filled-in form go only to your phone (as a
Pushover image) and the gitignored private/ folder, and data/applications.json
and data/apply_runs/ never contain your details.

    python auto_apply.py --selftest              # the real applier, against a FAKE copy of the form
    python auto_apply.py --check-form URL        # fill a real unit's form on your computer -- never submits
    python auto_apply.py --check-form URL --headed   # same, with the browser window visible
"""

import argparse
import copy
import functools
import html
import http.server
import itertools
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import apply_recorder
import background
import check_units
import direct_submit

_MODE_SETTING = os.environ.get("AUTO_APPLY_MODE", "").strip().lower()
ENABLED = _MODE_SETTING in ("on", "true", "yes", "1")
_MAX_RENT_SETTING = os.environ.get("AUTO_APPLY_MAX_RENT", "").strip()
MAX_RENT = check_units.number(_MAX_RENT_SETTING)  # $/month, inclusive; None = not set

PROFILE_ENV = "APPLICANT_PROFILE"
PROFILE_FILE = "applicant_profile.json"
APPLICATIONS_FILE = "data/applications.json"
PRIVATE_DIR = "private/applications"  # gitignored: screenshots of the filled form show your details

# What the form needs, in its order. Label patterns match the start of the
# field's label, ignoring case. Fields the site adds later can be filled
# without a code change through the profile's "extra_fields".
FORM_FIELDS = [
    # profile key       label pattern                    required by the form
    ("first_name", r"^first\s*name", True),
    ("last_name", r"^last\s*name", True),
    ("email", r"^e-?mail", True),
    ("cell_phone", r"^(cell|mobile)\s*phone|^phone", True),
    ("work_phone", r"^work\s*phone", False),
    ("building", r"^building", True),  # the building NUMBER of your current address, e.g. 123
    ("street_name", r"^street", True),
    ("apartment_no", r"^apartment|^apt\b", False),
    ("city", r"^city", True),
    ("state", r"^state", False),
    ("zip", r"^zip|^postal", True),
    ("household_size", r"^household\s*size", True),
    ("annual_income", r"income", True),
]
REQUIRED_PROFILE_KEYS = tuple(key for key, _, required in FORM_FIELDS if required)
PHONE_KEYS = ("cell_phone", "work_phone")

# Every qualifying unit is applied to -- there's no daily limit -- but each
# at most once a day (New York time). If the same unit is listed again on a
# later day, it's applied to again: the site ranks applications in the order
# they arrive, so it's worth being early again. An attempt that crashed or
# couldn't press SUBMIT (so nothing was sent) gets one more try that day.
MAX_FAILED_ATTEMPTS_PER_UNIT = 2
MAX_FORM_RECORDINGS_PER_RUN = 2  # over-limit units whose (unfilled) form gets recorded
APPLY_WORKERS = 2  # browsers kept open, so two qualifying units are applied to at once
WARM_REFRESH_SECONDS = 240  # an idle worker reloads the site this often, to keep its connections open
DIRECT_SUBMIT = True  # send straight to the API once the request format is proven (direct_submit.py)
WARM_BEFORE_POSTING_SECONDS = 20  # the API connection is opened this long before each quarter hour
MAX_FORM_PAGES = 6  # Next/Continue presses, in case the form ever grows pages
FORM_TIMEOUT_MS = 20000  # waiting for APPLY NOW, and for the form after it
CONFIRMATION_TIMEOUT_MS = 30000
PUSHOVER_IMAGE_LIMIT = 5_000_000  # bytes

SELFTEST_SITE = Path(__file__).resolve().parent / "tests" / "fixtures" / "apply_site"
EXAMPLE_PROFILE = Path(__file__).resolve().parent / "applicant_profile.example.json"
SELFTEST_RUNS_DIR = "tests/output/apply_runs"  # gitignored
NEXT_STEP_REMINDER = ("Watch your email: StuyTown sends a link to the detailed application, "
                      "which has to be completed within 24 hours.")


# ------------------------------------------------------------ the profile


class ProfileError(ValueError):
    """The applicant profile is missing pieces or isn't valid JSON."""


# ------------------------------------------------------------ the profile


def load_profile() -> dict | None:
    """Your applicant details: APPLICANT_PROFILE (JSON text, e.g. a GitHub
    secret), else applicant_profile.json. None if neither exists. Raises
    ProfileError if what's there can't be used -- without echoing any of it."""
    raw, source = os.environ.get(PROFILE_ENV, ""), f"the {PROFILE_ENV} secret"
    if not raw.strip() and os.path.exists(PROFILE_FILE):
        raw, source = Path(PROFILE_FILE).read_text(encoding="utf-8"), PROFILE_FILE
    if not raw.strip():
        return None
    try:
        profile = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProfileError(f"{source} isn't valid JSON (line {e.lineno}, column {e.colno})") from None
    if not isinstance(profile, dict):
        raise ProfileError(f"{source} must be a JSON object ({{ ... }})")
    mask_in_github_logs(profile)
    problems = profile_problems(profile)
    if problems:
        raise ProfileError(f"{source}: {'; '.join(problems)}")
    return profile


def profile_problems(profile: dict) -> list:
    """What's missing or malformed -- by key name only, never the values."""
    problems = []
    missing = [key for key in REQUIRED_PROFILE_KEYS if not str(profile.get(key) or "").strip()]
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    email = str(profile.get("email") or "").strip()
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        problems.append("email doesn't look like an email address")
    for key in PHONE_KEYS:
        if str(profile.get(key) or "").strip() and phone_digits(profile[key]) is None:
            problems.append(f"{key} should be a 10-digit US number, e.g. 212-555-0123")
    zip_code = str(profile.get("zip") or "").strip()
    if zip_code and not re.fullmatch(r"\d{5}(-\d{4})?", zip_code):
        problems.append("zip should be 5 digits")
    size = check_units.number(profile.get("household_size"))
    if profile.get("household_size") not in (None, "") and (size is None or size < 1 or size != int(size)):
        problems.append("household_size should be a whole number, e.g. 1")
    income = check_units.number(profile.get("annual_income"))
    if profile.get("annual_income") not in (None, "") and (income is None or income <= 0):
        problems.append("annual_income should be a number, e.g. 95000")
    return problems


# The form's own limits (its settings on 2026-10-12): longer values, or
# non-ASCII ones where it says "Is ASCII", are refused by the site.
FIELD_LIMITS = {"first_name": (20, True), "last_name": (25, True), "building": (7, False),
                "street_name": (32, False), "apartment_no": (6, False), "city": (30, False), "state": (2, True),
                "zip": (10, True), "household_size": (2, False)}


def profile_warnings(profile: dict) -> list:
    """Values the site's form would refuse -- by key name only, never the values."""
    warnings = []
    for key, (longest, ascii_only) in FIELD_LIMITS.items():
        text = str(profile.get(key) if profile.get(key) is not None else "").strip()
        if len(text) > longest:
            warnings.append(f"{key} is longer than the form allows ({longest} characters)")
        elif ascii_only and not text.isascii():
            warnings.append(f"{key} has characters the form refuses (letters with accents etc.)")
    return warnings


def phone_digits(value) -> str | None:
    """'212-555-0123' / '+1 (212) 555-0123' -> '12125550123' (US numbers
    only: the form's phone boxes start with '+', so the country code matters)."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        digits = "1" + digits
    return digits if len(digits) == 11 and digits.startswith("1") else None


def mask_in_github_logs(profile: dict) -> None:
    """Tell GitHub Actions to blank out every profile value wherever it would
    appear in this run's (public) log. GitHub only masks a secret's full text
    on its own, not the individual values inside it."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    for value in _profile_strings(profile):
        if len(value) >= 3:  # masking "1" or "NY" would blank out half the log
            print(f"::add-mask::{value}")


def _profile_strings(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            if not str(key).startswith("_"):
                yield from _profile_strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _profile_strings(inner)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        if text:
            yield text
            digits = phone_digits(text)
            if digits:  # the phone as the form shows it, too
                yield digits
                yield digits[1:]


@functools.cache
def _profile_for_run() -> dict | None:
    """The profile, checked once per run along with the rent limit. A problem
    is reported (log + one alert) and switches auto-apply off for the run; it
    never stops the watcher -- the alerts matter more."""
    try:
        if MAX_RENT is None:
            raise ProfileError("the AUTO_APPLY_MAX_RENT repository variable isn't set to a number "
                               "(e.g. 3000)")
        profile = load_profile()
        if profile is None:
            raise ProfileError(f"no applicant profile found (set the {PROFILE_ENV} secret, "
                               f"or create {PROFILE_FILE} on your own computer)")
        return profile
    except ProfileError as e:
        print(f"Auto-apply is OFF for this run: {e}")
        background.notify_with_retries(check_units.notify, {
            "title": "StuyTown auto-apply is off",
            "message": f"{html.escape(str(e))}. New-unit alerts still work.",
            "priority": 0,
        }, attempts=2)
        return None


def startup_check() -> None:
    """Print what auto-apply will do this run (and mask the profile in the
    log before anything else prints). Called once when the watcher starts."""
    if _MODE_SETTING and not ENABLED and _MODE_SETTING != "off":
        print(f"WARNING: AUTO_APPLY_MODE={_MODE_SETTING!r} isn't on or off -- treating it as off.")
    if not ENABLED:
        print("Auto-apply: off (set the AUTO_APPLY_MODE repository variable to on to turn it on)")
        return
    profile = _profile_for_run()
    if profile:
        print(f"Auto-apply: ON for units at or under ${MAX_RENT:,.0f}/mo (applicant profile loaded)")
        for warning in profile_warnings(profile):
            print(f"   WARNING: {warning} -- the site would refuse the application")


# ------------------------------------------------- which units to apply to


def skip_reason(unit: dict, profile: dict) -> str | None:
    """Why this unit doesn't qualify, or None if it does."""
    rent = check_units.unit_rent(unit)
    if rent is None:
        return "no rent listed"
    if MAX_RENT is None:
        return "AUTO_APPLY_MAX_RENT isn't set"
    if rent > MAX_RENT:
        return f"rent ${rent:,.2f} is over ${MAX_RENT:,.2f}"
    income = check_units.number(profile.get("annual_income"))
    required = check_units.number(unit.get("incomeRequirement"))
    if income is not None and required is not None and income < required:
        # Deliberately doesn't say what your income is: this goes in the log.
        return f"your annual income is below its ${required:,.0f} minimum"
    return None


NEW_YORK = ZoneInfo("America/New_York")


def new_york_date(utc_stamp: str | None = None):
    """The New York calendar date of a "2026-10-11T11:02:03Z" stamp (now if None)."""
    when = datetime.fromisoformat(utc_stamp.replace("Z", "+00:00")) if utc_stamp else datetime.now(timezone.utc)
    return when.astimezone(NEW_YORK).date()


def already_handled(record: dict | None, today=None) -> str | None:
    """Why this unit shouldn't be tried (again) today, or None. Only today's
    attempts count (New York date): a unit listed again on a later day gets
    applied to again."""
    today = today or new_york_date()
    attempts = [a for a in (record or {}).get("attempts", [])
                if not a.get("at_utc") or new_york_date(a["at_utc"]) == today]
    if any(a["status"] in ("submitted", "unconfirmed") for a in attempts):
        return "already applied today"  # an unconfirmed submit may have gone through: never send a second
    done = [a for a in attempts if a["status"] != "failed"]
    if done:
        return f"already tried ({done[-1]['status']})"
    if len(attempts) >= MAX_FAILED_ATTEMPTS_PER_UNIT:
        return f"gave up after {len(attempts)} failed tries"
    return None


def load_applications() -> dict:
    if not os.path.exists(APPLICATIONS_FILE):
        return {}
    with open(APPLICATIONS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_applications(applications: dict) -> None:
    os.makedirs(os.path.dirname(APPLICATIONS_FILE) or ".", exist_ok=True)
    tmp = APPLICATIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(applications, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, APPLICATIONS_FILE)


@functools.cache
def _profile_if_any() -> dict | None:
    try:
        return load_profile()
    except ProfileError:
        return None


def qualifies(unit: dict) -> bool:
    """Would auto-apply's rules take this unit (rent at or under the limit,
    income at or above its minimum)? Picks the Emergency alert over the
    normal one -- also when auto-apply is off, so you can apply yourself. With
    no AUTO_APPLY_MAX_RENT set, every new unit counts."""
    if MAX_RENT is None:
        return True
    return skip_reason(unit, _profile_if_any() or {}) is None


class DirectRoute:
    """Everything about submitting straight to the API (direct_submit.py):
    the site's current form settings, the proven request format, and the
    kept-open connection. Used by the Applier from several threads."""

    def __init__(self, profile: dict, logger):
        self.profile = profile
        self.logger = logger
        self.format = direct_submit.load_format()
        self.config = None
        self.client = direct_submit.ApiClient()
        self._lock = threading.Lock()

    def refresh_config(self, unit_spk: str = "", html: str | None = None) -> None:
        """Read the site's form settings -- from a page the browser already
        has, or by fetching a unit page. A change of the site's code version
        switches direct submit off until a new capture proves the format."""
        config = direct_submit.parse_site_config(html) if html else direct_submit.fetch_site_config(unit_spk)
        if not config:
            return
        with self._lock:
            before = (self.config or {}).get("site_version")
            self.config = config
        if before and before != config.get("site_version"):
            print(f"   Direct submit: the site's code changed (v{before} -> v{config.get('site_version')}); "
                  "the browser applies until the new version's request is checked")

    def stale(self) -> bool:
        config = self.config
        return config is None or time.monotonic() - config["_read_at"] > direct_submit.CONFIG_MAX_AGE_SECONDS

    def why_not(self) -> str | None:
        """Why a unit can't be submitted directly right now, or None."""
        fmt, config = self.format, self.config
        if not DIRECT_SUBMIT:
            return "direct submit is turned off"
        if not fmt or not fmt.get("rules"):
            return "the site's request hasn't been captured yet"
        if not config:
            return "couldn't read the site's form settings"
        if self.stale():
            return "the site's form settings are out of date"
        if not config.get("site_version") or fmt.get("site_version") != config.get("site_version"):
            return f"the site's code changed since the request was checked (v{fmt.get('site_version')})"
        if fmt.get("api_url") != config.get("api_url"):
            return "the site sends applications somewhere else now"
        return None

    def prepare(self, unit: dict):
        """(api_url, body, headers) for a direct submit, or a reason why not."""
        reason = self.why_not()
        if reason:
            return reason
        body = direct_submit.build_body(unit, self.profile, self.format)
        if body is None:
            return "the request for this unit can't be built for sure (its 'version')"
        return self.config["api_url"], body, direct_submit.request_headers(self.format, self.config, unit)

    def learn(self, captured: dict | None, unit: dict, source: str) -> str:
        """Check a request the site's own form built (captured by the
        browser) and learn the format from it. Returns what happened, for the
        log -- never the request itself."""
        if not captured or not isinstance(captured.get("body"), dict):
            return "no request captured"
        config = self.config
        if config is None or (captured.get("url") and captured["url"].split("?")[0] != config.get("api_url")):
            return "the captured request went to an unexpected address; not learned"
        new = direct_submit.learn_format(captured["body"], self.profile, unit, config)
        if new is None:
            return "the site's request has something unexpected in it; the browser keeps applying"
        new = direct_submit.merge_formats(self.format, new)
        new["headers"] = direct_submit.captured_headers(captured.get("headers") or {}, config)
        if direct_submit.build_body(unit, self.profile, new) != captured["body"]:
            return "couldn't rebuild the site's request exactly; the browser keeps applying"
        old = self.format or {}
        same = all(old.get(k) == new.get(k) for k in ("site_version", "api_url", "shape", "rules", "headers"))
        new.update(verified_utc=_utc_ms(), verified_from=check_units.unit_id(unit), verified_by=source)
        self.format = new
        if not same:
            self.logger.submit(direct_submit.save_format, copy.deepcopy(new))
            return f"learned the site's request (code v{new['site_version']}); qualifying units now go direct"
        return "the site's request is unchanged"

    def submit(self, prepared) -> dict:
        api_url, body, headers = prepared
        return self.client.post(api_url, body, headers)

    def keep_warm(self) -> None:
        config = self.config
        if config and not self.why_not():
            self.client.warm(config["api_url"])

    def observations(self) -> dict:
        fmt, config = self.format or {}, self.config or {}
        return {"ready": self.why_not() is None, "why_not": self.why_not(),
                "site_version": config.get("site_version"), "format_site_version": fmt.get("site_version"),
                "format_rules": fmt.get("rules"), "verified_utc": fmt.get("verified_utc")}


def _near_posting_time(now: datetime | None = None) -> bool:
    """Within a few seconds before, or a minute after, a quarter hour -- when
    StuyTown posts units (7:15, 7:30, 8:00, 8:15 so far)."""
    now = now or datetime.now(timezone.utc)
    into = (now.minute % 15) * 60 + now.second + now.microsecond / 1e6
    return into >= 15 * 60 - WARM_BEFORE_POSTING_SECONDS or into <= 60


class Applier:
    """Applies to qualifying units the moment the checker hands them over.

    dispatch() runs on the checker's thread and never waits. A unit goes
    DIRECT -- its application sent straight to StuyTown's API on a thread of
    its own -- whenever the request format has been proven for the site's
    current code (see direct_submit.py). Otherwise, or if a direct request
    fails, a browser applies: APPLY_WORKERS threads each keep one Chromium
    open (Playwright objects belong to the thread that made them) and take
    jobs from one priority queue -- applications first, cheapest first, then
    form recordings, which only run while no application is. Each browser
    trip also checks the site's request against the proven format. Results
    go to the background notifier and logger; nothing they do can delay or
    break an application."""

    def __init__(self, profile: dict, notifier=None, logger=None, workers: int = APPLY_WORKERS,
                 runs_dir: str | None = None):
        self.profile = profile
        self.notifier = notifier or background.Worker("notifier-inline", inline=True)
        self.logger = logger or background.Worker("logger-inline", inline=True)
        self.workers = workers
        self.runs_dir = runs_dir
        self.applications = load_applications()
        self.direct = DirectRoute(profile, self.logger)
        self._jobs = queue.PriorityQueue()
        self._order = itertools.count()
        self._lock = threading.Lock()
        self._in_flight = set()  # unit IDs queued or being applied to
        self._queued_at = {}  # unit ID -> (monotonic, UTC) when dispatch() queued it
        self._direct_tried = {}  # unit ID -> what a direct request that the browser took over from did
        self._ready_seconds = []  # per browser launch: start -> site loaded
        self.site_watch = apply_recorder.SiteWatch()  # what the idle listings page does on its own
        self.recordings = []  # this run's recordings: apartment, outcome, what direct submit made of it
        self._recorded = set()
        self._skips_logged = set()
        self._recording = False
        self._stopping = threading.Event()
        self._started = threading.Semaphore(0)
        self._threads = []

    # ------------------------------------------------------------ lifecycle

    def start(self, wait_seconds: float = 90) -> None:
        """Start the workers and wait (up to wait_seconds) until each has its
        browser open and the site loaded; read the site's form settings."""
        for index in range(self.workers):
            thread = threading.Thread(target=self._worker, args=(index,), name=f"applier-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        self.direct.refresh_config(_a_known_unit())
        known = _last_listed_unit()
        if self.direct.why_not() and known is not None:
            # Before 7:00 nothing is listed, but the last unit seen may still
            # have its page and form: a recording of it (SUBMIT held back,
            # nothing sent) can prove the format before the first posting.
            self._jobs.put((1, 0.0, next(self._order), "record", known))
        maintain = threading.Thread(target=self._maintain, name="applier-direct", daemon=True)
        maintain.start()
        self._threads.append(maintain)
        reason = self.direct.why_not()
        print("   Direct submit: " + ("ready -- qualifying units go straight to StuyTown's API"
                                       if reason is None else f"not yet ({reason}); the browser applies"))
        deadline = time.monotonic() + wait_seconds
        for _ in range(self.workers):
            if not self._started.acquire(timeout=max(0.0, deadline - time.monotonic())):
                print("   WARNING: an applier browser is slow to start; carrying on")
                break

    def _maintain(self) -> None:
        """Keep the site's settings fresh, and the API connection open around
        posting times."""
        while not self._stopping.wait(1.0):
            try:
                if self.direct.stale():
                    self.direct.refresh_config(_a_known_unit())
                if _near_posting_time():
                    self.direct.keep_warm()
            except Exception as e:
                print(f"   WARNING: direct submit upkeep failed: {type(e).__name__}: {e}")

    def busy(self) -> bool:
        """True while an application is queued or running -- the background
        logger waits for this to clear."""
        return bool(self._in_flight)

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._in_flight and time.monotonic() < deadline:
            time.sleep(0.1)
        return not self._in_flight

    def stop(self, timeout: float = 60) -> None:
        """Let running applications finish (up to timeout), then close."""
        self.wait_idle(timeout)
        self._stopping.set()
        for thread in self._threads:
            thread.join(timeout=10)
        self.direct.client.close()

    def observations(self) -> dict:
        """For the morning's run stats (run_stats.py): no personal details."""
        return {
            "workers": self.workers,
            "browser_ready_seconds": list(self._ready_seconds),
            "direct_submit": self.direct.observations(),
            "recordings": list(self.recordings),
            "profile_warnings": profile_warnings(self.profile),
            "idle_listings_page": self.site_watch.summary(),
        }

    # -------------------------------------------------------------- dispatch

    def dispatch(self, units: list) -> list:
        """Called by the checker with every check's listings. For each unit
        that qualifies and isn't already handled or in progress (cheapest
        first): a direct submit on its own thread, or a browser job. Also a
        recording for an over-limit unit or two. Returns the units taken.
        Never waits."""
        queued = []
        with self._lock:
            for unit in units:
                uid = check_units.unit_id(unit)
                if uid in self._in_flight:
                    continue
                not_eligible = skip_reason(unit, self.profile)
                reason = not_eligible or already_handled(self.applications.get(uid))
                if not reason:
                    self._in_flight.add(uid)
                    self._queued_at[uid] = (time.monotonic(), _utc_ms())
                    queued.append(unit)
                    continue
                if uid not in self._skips_logged:
                    self._skips_logged.add(uid)
                    print(f"   Auto-apply: skipping {check_units.unit_label(unit)} -- {reason}")
                if not_eligible and uid not in self._recorded and len(self._recorded) < MAX_FORM_RECORDINGS_PER_RUN:
                    self._recorded.add(uid)
                    self._jobs.put((1, 0.0, next(self._order), "record", unit))
        for unit in sorted(queued, key=lambda u: check_units.unit_rent(u) or 0):
            prepared = self.direct.prepare(unit)
            if isinstance(prepared, tuple):
                threading.Thread(target=self._direct, args=(unit, prepared), daemon=True,
                                 name=f"direct-{check_units.apartment(unit)}").start()
                print(f"   Auto-apply: applying to {check_units.unit_label(unit)} now (direct)")
            else:
                self._browser_job(unit)
                print(f"   Auto-apply: applying to {check_units.unit_label(unit)} now (browser: {prepared})")
        return queued

    def _browser_job(self, unit: dict) -> None:
        self._jobs.put((0, check_units.unit_rent(unit) or 0.0, next(self._order), "apply", unit))

    # ---------------------------------------------------------------- direct

    def _direct(self, unit: dict, prepared) -> None:
        uid = check_units.unit_id(unit)
        try:
            queued = self._queued_at.get(uid)
            started, began = _utc_ms(), time.monotonic()
            result = self.direct.submit(prepared)
            if "error" in result:
                status, detail, server_time = "lost", result["error"], None
            else:
                status, detail, server_time = direct_submit.interpret(result["status"], result["text"])
            timeline = {"started_utc": started,
                        "queue_wait_seconds": round(began - queued[0], 3) if queued else None,
                        "form_via": "direct (API)", "submit_pressed_utc": result.get("sent_utc"),
                        "answer_utc": result.get("answered_utc"), "server_submitted_utc": server_time,
                        "api_seconds": result.get("seconds"), "connection_was_warm": result.get("reused")}
            recording = _direct_recording(unit, prepared, result, status, detail, timeline, self.profile,
                                          self.runs_dir)
            if status in ("submitted", "unconfirmed", "rejected"):
                attempt = Attempt(status, detail, filled=_keys_sent(prepared[1]), timeline=timeline,
                                  seconds=time.monotonic() - began, form_url=prepared[0])
                print(f"   Auto-apply result for {check_units.unit_label(unit)} after {attempt.seconds:.2f}s "
                      f"(direct): {status} -- {detail}")
                self._finished(unit, attempt, recording)
                return
            # Refused, or the answer was lost: the browser applies. A request
            # that did go through makes the site refuse the second one, so
            # nothing is ever sent twice by mistake.
            print(f"   Auto-apply: the direct request for {check_units.unit_label(unit)} {detail}; "
                  "the browser applies instead")
            self._direct_tried[uid] = {"status": status, "detail": detail, **timeline}
            if recording is not None:
                self.logger.submit(recording.write)
            if status == "refused" and result.get("status") in (400, 422):
                self.direct.format = None  # the API doesn't take this format (any more): browser only
            self._browser_job(unit)
        except Exception as e:
            print(f"   WARNING: direct submit for {check_units.unit_label(unit)} crashed ({type(e).__name__}: {e}); "
                  "the browser applies instead")
            self._direct_tried[uid] = {"status": "crashed", "detail": f"{type(e).__name__}: {e}"[:200]}
            self._browser_job(unit)

    # --------------------------------------------------------------- workers

    def _worker(self, index: int) -> None:
        from playwright.sync_api import sync_playwright

        announced = False
        for launch in range(3):  # a crashed browser is restarted, twice at most
            try:
                with sync_playwright() as p:
                    launched = time.monotonic()
                    browser = p.chromium.launch()
                    context = new_context(browser)
                    watch = self.site_watch if index == 0 else None
                    warm = _warm_up(context, watch)
                    self._ready_seconds.append(round(time.monotonic() - launched, 2))
                    if not announced:
                        announced = True
                        self._started.release()
                    if self._serve(browser, context, warm, watch):
                        if watch:
                            watch.harvest(warm)
                        browser.close()
                        return
                    print(f"   WARNING: applier browser {index} stopped responding; restarting it")
                    continue
            except Exception as e:
                print(f"   WARNING: applier browser {index} crashed ({type(e).__name__}: {e}); "
                      + ("restarting it" if launch < 2 else "giving up on it"))
        if not announced:
            self._started.release()

    def _serve(self, browser, context, warm, watch=None) -> bool:
        """Take jobs until stopped (True), or until the browser dies (False:
        the job goes back in the queue and the browser is restarted)."""
        last_warm = time.monotonic()
        while not self._stopping.is_set():
            try:
                job = self._jobs.get(timeout=0.5)
            except queue.Empty:
                if time.monotonic() - last_warm > WARM_REFRESH_SECONDS:
                    _reload_quietly(warm, watch)
                    last_warm = time.monotonic()
                continue
            _, _, order, kind, unit = job
            if not browser.is_connected():
                self._jobs.put(job)
                self._jobs.task_done()
                return False
            try:
                if kind == "record":
                    with self._lock:
                        allowed = not self._in_flight and not self._recording
                        self._recording = self._recording or allowed
                    if not allowed:  # an application comes first; try again shortly
                        time.sleep(0.5)
                        self._jobs.put((1, 0.0, order, kind, unit))
                        continue
                    try:
                        self._record_form(context, unit)
                    finally:
                        self._recording = False
                else:
                    self._apply(context, unit)
                last_warm = time.monotonic()
            except Exception as e:
                print(f"   WARNING: auto-apply job for {check_units.unit_label(unit)} crashed: {e}")
                if kind == "apply":
                    self._finished(unit, Attempt("failed", f"{type(e).__name__}: {e}"[:200]), None)
            finally:
                self._jobs.task_done()
        return True

    def _apply(self, context, unit: dict) -> None:
        uid = check_units.unit_id(unit)
        queued = self._queued_at.get(uid)
        started = _utc_ms()
        waited = round(time.monotonic() - queued[0], 3) if queued else None
        attempt, recording = apply_to_unit(context, unit, self.profile, runs_dir=self.runs_dir,
                                           site=self.direct.config)
        attempt.timeline = {"started_utc": started, "queue_wait_seconds": waited, **attempt.timeline}
        tried = self._direct_tried.pop(uid, None)
        if tried:
            attempt.timeline["direct_tried"] = tried
            if tried["status"] in ("lost", "unknown", "crashed") and attempt.status == "rejected" \
                    and "open application" in attempt.detail:
                attempt.status = "unconfirmed"
                attempt.detail = ("the direct request's answer was lost, and the site now says you already have "
                                  "an application for this apartment: it most likely went through")
        print(f"   Auto-apply result for {check_units.unit_label(unit)} after {attempt.seconds:.1f}s: "
              f"{attempt.status} -- {attempt.detail}")
        if attempt.status in ("submitted", "unconfirmed"):
            if self.direct.stale() and recording is not None and recording.pages.get("3_form_filled"):
                self.direct.refresh_config(html=recording.pages["3_form_filled"])  # the page has them too
            print(f"   Direct submit: {self.direct.learn(attempt.captured, unit, 'a browser application')}")
        attempt.captured = None  # it holds your details: never kept past this point
        self._finished(unit, attempt, recording)

    def _record_form(self, context, unit: dict) -> None:
        attempt, recording = apply_to_unit(context, unit, self.profile, record_only=True, runs_dir=self.runs_dir,
                                           site=self.direct.config)
        print(f"   Auto-apply: recorded the form of {check_units.unit_label(unit)} "
              f"in {attempt.seconds:.1f}s -- {attempt.status}: {attempt.detail}")
        if recording is not None:
            page = recording.pages.get("1_unit_page") or recording.pages.get("2_form_empty")
            if page:
                self.direct.refresh_config(html=page)
        learned = self.direct.learn(attempt.captured, unit, "a recording (nothing sent)")
        attempt.captured = None
        print(f"   Direct submit: {learned}")
        self.recordings.append({"apartment": check_units.apartment(unit), "unit_id": check_units.unit_id(unit),
                                "status": attempt.status, "detail": attempt.detail, "direct_submit_check": learned})
        if recording:
            recording.report["notes"]["direct_submit_check"] = learned
            self.logger.submit(recording.write)

    def _finished(self, unit: dict, attempt: "Attempt", recording) -> None:
        uid = check_units.unit_id(unit)
        if recording is not None:
            attempt.recording = recording.folder
        with self._lock:
            queued = self._queued_at.pop(uid, None)
            attempt.timeline = {**({"queued_utc": queued[1]} if queued else {}), **attempt.timeline,
                                "finished_utc": _utc_ms()}
            _record(self.applications, unit, attempt)
            snapshot = copy.deepcopy(self.applications)
            self._in_flight.discard(uid)
        # Everything from here on is background work.
        self.notifier.submit(background.notify_with_retries, check_units.notify, result_alert(unit, attempt))
        self.logger.submit(save_applications, snapshot)
        if recording is not None:
            self.logger.submit(recording.write)
        self.logger.submit(save_private_screenshots, unit, attempt)


def _a_known_unit() -> str:
    """Any unit ID the site has listed (its unit pages carry the form's
    settings), so reading them doesn't depend on something being listed now."""
    try:
        with open(check_units.STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if state:
            return next(iter(state))
        with open(check_units.EVENTS_FILE, encoding="utf-8") as f:
            events = json.load(f)
        return next(u["unit_id"] for e in reversed(events) for u in e.get("units", []) if u.get("unit_id"))
    except (OSError, ValueError, StopIteration, KeyError, TypeError):
        return ""


def _last_listed_unit() -> dict | None:
    """The most recently posted unit in the events log, as the API listed it."""
    try:
        with open(check_units.EVENTS_FILE, encoding="utf-8") as f:
            events = json.load(f)
        for event in reversed(events):
            for listed in event.get("units", []) if event.get("event") == "new" else []:
                if isinstance(listed.get("data"), dict) and listed["data"].get("unitSpk"):
                    return listed["data"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _keys_sent(body: dict) -> list:
    """The profile keys a direct request carried (names only)."""
    flat = direct_submit._flatten(body)
    keys = [direct_submit.PROFILE_TEXT_KEYS[k] for k in flat if k in direct_submit.PROFILE_TEXT_KEYS and flat[k] != ""]
    extra = {"cellPhone": "cell_phone", "workPhone": "work_phone", "householdSize": "household_size",
             "totalAnnualEarnings": "annual_income"}
    return keys + [name for k, name in extra.items() if flat.get(k) not in (None, "")]


def _direct_recording(unit, prepared, result, status, detail, timeline, profile, runs_dir):
    """A direct submit's request and answer, as a recording -- redacted like
    every other when the logger writes it."""
    api_url, body, headers = prepared
    entry = {"t": 0.0, "method": "POST", "type": "direct", "url": api_url, "request_headers": headers,
             "request_body": json.dumps(body)}
    if "error" in result:
        entry["failed"] = result["error"]
    else:
        entry.update(status=result["status"], answered_t=result["seconds"], response_body=result["text"][:20000],
                     response_headers={"content-type": "application/json"})
    folder = apply_recorder.run_folder(check_units.apartment(unit), "direct", runs_dir)
    report = {"started_utc": timeline["started_utc"], "seconds": result.get("seconds"),
              "outcome": {"status": status, "detail": detail, "timeline": timeline},
              "steps": [{"t": 0.0, "step": "pressing SUBMIT", "how": "sent straight to the API"}],
              "notes": {"unit": check_units.unit_summary(unit)}, "console": [], "navigations": [],
              "websockets": [], "cookies": []}
    return apply_recorder.Recording(folder, apply_recorder.Redactor(profile), report, [entry], {}, {})


def _record(applications: dict, unit: dict, attempt: "Attempt") -> None:
    """What goes in the (public) applications log: the unit -- already public
    -- and how it went. Never any of your details."""
    record = applications.setdefault(check_units.unit_id(unit), {
        "apartment": check_units.apartment(unit),
        "address": check_units.address(unit),
        "rent": check_units.unit_rent(unit),
        "url": check_units.unit_url(unit),
        "attempts": [],
    })
    record["attempts"].append({
        "at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": attempt.status,
        "detail": attempt.detail,
        "seconds": round(attempt.seconds, 1),
        "form_url": attempt.form_url,  # where APPLY NOW led (no personal details in it)
        "fields_filled": attempt.filled,  # profile key names only, e.g. "first_name"
        "problems": attempt.problems,  # the site's own labels for fields it couldn't fill
        "recording": str(attempt.recording) if attempt.recording else None,  # data/apply_runs/...
        "timeline": attempt.timeline,  # when each stage happened (UTC), for the run stats
    })


# ------------------------------------------------------------- the browser

APPLY_BUTTON = re.compile(r"^\s*apply\b", re.I)
SUBMIT_BUTTON = re.compile(r"^\s*(submit|send|finish|complete)\b|submit\s+(my\s+)?application", re.I)
NEXT_BUTTON = re.compile(r"^\s*(next|continue|proceed|save\s*(and|&)\s*continue)\b", re.I)
CONFIRMATION = re.compile(
    r"thank\s*you|application\s+(has\s+been\s+|was\s+)?(received|submitted|complete)|"
    r"\bsuccess(ful|fully)?\b|submission\s+(received|successful)|"
    r"check\s+your\s+(e-?mail|inbox)|we([\u2019']ve|\s+have)\s+(received|sent)|sent\s+a\s+link|"
    r"confirmation\s+(number|#|code)", re.I)
# What a site's complaint about a form sounds like (as opposed to, say, a
# "Submitting..." progress message).
SITE_ERROR = re.compile(r"required|invalid|not\s+valid|error|wrong|failed|must|please\s+(enter|provide|check|"
                        r"correct|try)|try\s+again|already", re.I)
# Images, video, fonts and the cookie banner aren't needed to apply, and skipping them
# makes the unit page usable sooner.
SKIPPED_RESOURCE_TYPES = ("image", "media", "font")

# How the page's fields are read: every visible input with the text a person

_LABEL_OF_JS = r"""
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const wordy = s => /[a-z]{2}/i.test(s || '');
const CONTROLS = 'input:not([type=hidden]), select, textarea';
function labelOf(el) {
    if (el.labels && el.labels.length && clean(el.labels[0].innerText)) return clean(el.labels[0].innerText);
    if (clean(el.getAttribute('aria-label'))) return clean(el.getAttribute('aria-label'));
    const by = el.getAttribute('aria-labelledby');
    if (by) {
        const text = clean(by.split(/\s+/).map(id => (document.getElementById(id) || {}).innerText || '').join(' '));
        if (text) return text;
    }
    let node = el;
    for (let depth = 0; depth < 5 && node.parentElement; depth++) {
        node = node.parentElement;
        if (node.querySelectorAll(CONTROLS).length > 1) break;
        let text = node.innerText || '';
        if (el.tagName === 'SELECT') text = text.replace(el.innerText, '');
        if (wordy(text)) return clean(text).slice(0, 120);  // not just the "$" beside a money box
    }
    // A label written just before the box, with no wrapper around the pair.
    let before = el.previousElementSibling;
    for (let i = 0; i < 3 && before; i++, before = before.previousElementSibling) {
        if (before.matches(CONTROLS) || before.querySelector(CONTROLS)) break;
        if (wordy(before.innerText)) return clean(before.innerText).slice(0, 120);
    }
    return clean(el.placeholder || el.name || el.id || el.type);
}
function shown(el) {
    const box = (['checkbox', 'radio'].includes(el.type) ? (el.closest('label, fieldset') || el.parentElement) : el)
        .getBoundingClientRect();
    return box.width > 0 && box.height > 0 && getComputedStyle(el).visibility !== 'hidden';
}
function isEmpty(el, root) {
    if (el.type === 'checkbox') return !el.checked;
    if (el.type === 'radio') return ![...root.querySelectorAll('input[type=radio]')].some(r => r.name === el.name && r.checked);
    const value = (el.value || '').replace(/[\s+$,]/g, '');  // a phone box's lone "+", a "$"
    return value === '' || /^0*\.?0*$/.test(value);
}
"""

_INVENTORY_JS = "root => {" + _LABEL_OF_JS + r"""
    root.querySelectorAll('[data-autoapply-field]').forEach(el => el.removeAttribute('data-autoapply-field'));
    const fields = [];
    for (const el of root.querySelectorAll(CONTROLS)) {
        if (['submit', 'button', 'reset', 'image', 'file'].includes(el.type) || el.disabled) continue;
        if (el.closest('header, nav, footer, [role=contentinfo]') || !shown(el)) continue;
        const label = labelOf(el);
        el.setAttribute('data-autoapply-field', String(fields.length));
        fields.push({
            index: fields.length, label, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(),
            required: el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(label),
            empty: isEmpty(el, root),
            // The rest is only for the recording: how the site built the box (never its value).
            html: {name: el.name || null, id: el.id || null, autocomplete: el.getAttribute('autocomplete'),
                   inputmode: el.getAttribute('inputmode'), placeholder: el.placeholder || null,
                   maxlength: el.maxLength > 0 ? el.maxLength : null, pattern: el.getAttribute('pattern'),
                   class: (el.className || '').toString().slice(0, 120) || null,
                   required_attr: el.required, aria_required: el.getAttribute('aria-required'),
                   options: el.tagName === 'SELECT' ? [...el.options].slice(0, 40).map(o => o.text.trim()) : undefined},
        });
    }
    return fields;
}"""

# Finds the application form: the box labelled First/Last Name (preferring
# one in a pop-up dialog), and marks the <form> or dialog around it.
_FIND_FORM_JS = "root => {" + _LABEL_OF_JS + r"""
    const named = [...root.querySelectorAll(CONTROLS)]
        .filter(el => shown(el) && !el.closest('header, nav, footer') && /first\s*name|last\s*name|full\s*name/i.test(labelOf(el)));
    if (!named.length) return null;
    const dialog = '[role=dialog], dialog, [aria-modal=true]';
    const anchor = named.find(el => el.closest(dialog)) || named[0];
    const box = anchor.closest('form') || anchor.closest(dialog);
    root.querySelectorAll('[data-autoapply-form]').forEach(el => el.removeAttribute('data-autoapply-form'));
    if (!box) return 'frame';
    box.setAttribute('data-autoapply-form', '1');
    return 'form';
}"""



# Every button and button-like link in the form, for the recording.
_BUTTONS_JS = r"""root => [...root.querySelectorAll('button, input[type=submit], input[type=button], a, [role=button]')]
    .filter(el => { const b = el.getBoundingClientRect(); return b.width > 0 && b.height > 0; })
    .slice(0, 40)
    .map(el => ({tag: el.tagName.toLowerCase(), type: el.getAttribute('type'),
                 text: (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 80),
                 href: el.getAttribute('href'), disabled: !!el.disabled,
                 class: (el.className || '').toString().slice(0, 120) || null}))"""


# Set on the SUBMIT button just before it's clicked, to check the click
# actually reached it (it once landed beside it after a screenshot).
_HIT_PROBE_JS = """el => { window.__autoApplyHit = false;
    el.addEventListener('click', () => { window.__autoApplyHit = true; }, {capture: true, once: true}); }"""


@dataclass
class Attempt:
    """How one trip through the form went."""

    # submitted | unconfirmed | rejected | incomplete | blocked | failed
    # | recorded (form recorded; on recordings, the request captured, nothing sent)
    # | filled (--check-form: filled, not sent)
    status: str
    detail: str
    filled: list = field(default_factory=list)  # profile keys that went into the form (and were checked)
    problems: list = field(default_factory=list)  # the site's labels for required fields left empty or wrong
    form_fields: list = field(default_factory=list)  # every field label on the form, for spotting changes
    form_url: str = ""
    filled_screenshot: bytes | None = None  # the filled-in form as SUBMIT was pressed -- shows your details
    result_screenshot: bytes | None = None  # the page at the end -- may show your details
    site_messages: list = field(default_factory=list)  # error text the site showed
    seconds: float = 0.0
    recording: Path | None = None  # data/apply_runs/... folder
    # UTC times, to the millisecond: queued, started, form found, SUBMIT
    # pressed, the site's answer, finished -- and how the form was reached.
    timeline: dict = field(default_factory=dict)
    # The request the site's form built ({"url", "headers", "body"}), for
    # checking direct submit's format. Holds your details: memory only, never
    # printed, logged or saved.
    captured: dict | None = field(default=None, repr=False)


def form_values(profile: dict) -> dict:
    """The profile as it goes into the form: everything but the "_" notes."""
    return {key: value for key, value in profile.items() if not str(key).startswith("_")}


def apply_to_unit(context, unit: dict, profile: dict, record_only: bool = False,
                  runs_dir: str | None = None, site: dict | None = None) -> tuple:
    """One trip through the unit's form, on a new page of an already-open
    browser. record_only: record the form, then fill it and press SUBMIT
    with every outgoing request blocked -- nothing is sent -- to capture the
    request the site's form builds. site: the site's form settings (its
    messages), if known. Returns (Attempt, Recording) -- the recording is
    collected but not yet written: that's the background logger's job.
    Never raises."""
    recorder = apply_recorder.RunRecorder(
        apply_recorder.run_folder(check_units.apartment(unit), "record" if record_only else "apply", runs_dir),
        apply_recorder.Redactor(profile))
    recorder.note("unit", check_units.unit_summary(unit))  # public listing data
    recorder.note("kind", "recording: the form recorded, then filled and SUBMIT pressed with every outgoing "
                          "request blocked -- nothing was sent" if record_only else "application")
    started = time.monotonic()
    pages_before = set(context.pages)
    page = context.new_page()
    try:
        recorder.watch(page)
        attempt = run_form(page, check_units.unit_url(unit), form_values(profile), submit=not record_only,
                           recorder=recorder, apartment=check_units.apartment(unit), capture=record_only,
                           record=record_only, site=site)
    except Exception as e:
        recorder.step("crashed", error=f"{type(e).__name__}: {e}"[:1000])
        attempt = Attempt("failed", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}",
                          result_screenshot=_screenshot(page))
    attempt.seconds = time.monotonic() - started
    recording = recorder.collect(_outcome(attempt), page=page)  # while the page is still open
    for opened in set(context.pages) - pages_before:  # this trip's page, and any pop-up it opened
        try:
            opened.close()
        except Exception:
            pass
    return attempt, recording


def _outcome(attempt: Attempt) -> dict:
    return {"status": attempt.status, "detail": attempt.detail, "seconds": round(attempt.seconds, 2),
            "form_url": attempt.form_url, "fields_filled": attempt.filled, "problems": attempt.problems,
            "site_messages": attempt.site_messages, "form_fields": attempt.form_fields,
            "timeline": attempt.timeline}


# The cookie banner (Ketch) and analytics: blocked so the banner can't cover
# anything and the pages settle faster.
BLOCKED = re.compile(r"ketchcdn\.com|ketchjs\.com|googletagmanager\.com|google-analytics\.com|doubleclick\.net")


def new_context(browser):
    """A browser session that skips images, video, fonts and trackers.
    Service workers are blocked so every request goes through the routes
    (the recordings' "nothing is sent" depends on it)."""
    context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    context.set_default_timeout(15000)

    def route(request_route):
        request = request_route.request
        if request.resource_type in SKIPPED_RESOURCE_TYPES or BLOCKED.search(request.url):
            request_route.abort()
        else:
            request_route.continue_()

    context.route("**/*", route)
    return context


def _warm_up(context, watch=None):
    """Open the listings page once, so DNS, the TLS connections and the
    site's scripts are ready before the first application. Kept open. With a
    SiteWatch, what the page then does on its own is noted for the stats."""
    page = context.new_page()
    try:
        if watch:
            watch.attach(page)
        page.goto(check_units.LISTINGS_URL, wait_until="domcontentloaded", timeout=30000)
        if watch:
            watch.loaded(page)
    except Exception as e:
        print(f"   WARNING: couldn't preload the StuyTown site ({type(e).__name__}); applying will still work")
    return page


def _reload_quietly(page, watch=None) -> None:
    try:
        if watch:
            watch.harvest(page)
        page.reload(wait_until="domcontentloaded", timeout=30000)
        if watch:
            watch.loaded(page)
    except Exception:
        pass


def _hold_outgoing(page) -> list:
    """From now on, every request this page tries to send that isn't a GET
    is stopped in the browser -- it never leaves -- and kept (in memory) as
    the request the site would have sent."""
    held = []

    def hold(route):
        request = route.request
        if request.method in ("GET", "HEAD", "OPTIONS"):
            route.fallback()  # on to the session's own rules (skip images...)
            return
        held.append({"url": request.url, "method": request.method, "headers": dict(request.headers),
                     "body": _json_or_none(request.post_data)})
        route.abort("blockedbyclient")

    page.route("**/*", hold)
    return held


def _json_or_none(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def run_form(page, url: str, values: dict | None, submit: bool, recorder=None, apartment: str = "",
             capture: bool = False, record: bool = False, site: dict | None = None) -> Attempt:
    """Open the unit page, press APPLY NOW, and on the form:
      record True      -> first record the empty form (fields, buttons, HTML, a screenshot)
      values None      -> stop there, fill nothing
      capture True     -> fill it and press SUBMIT with every outgoing request held
                          back in the browser: nothing is sent, and the request the
                          site's form built is returned (Attempt.captured)
      submit False     -> fill it, check every field took its value, screenshot it (--check-form)
      submit True      -> fill it and -- only if nothing required is empty or wrong --
                          press SUBMIT, then read the site's answer to the request.
    Nothing but what's needed happens before SUBMIT: the screenshot and the
    page HTML are taken right after it's pressed."""
    recorder = recorder or apply_recorder.RunRecorder(None, apply_recorder.Redactor(None))
    timeline = {"form_via": "unit page"}
    recorder.step("opening the unit page", url=url)
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    apply_button = _wait_for_apply_button(page, FORM_TIMEOUT_MS)
    if apply_button is None:
        recorder.snapshot("1_unit_page", page)
        recorder.step("no APPLY NOW button", page_url=page.url, buttons=_buttons(page))
        recorder.note("page_facts", _page_facts(page))
        return Attempt("failed", "couldn't find the APPLY NOW button on the unit page",
                       result_screenshot=_screenshot(page))
    recorder.step("APPLY NOW button visible", text=_text_of(apply_button), page_url=page.url)
    if record:
        recorder.snapshot("1_unit_page", page)
    apply_button.click()
    recorder.step("pressed APPLY NOW")
    found = _find_form(page.context, FORM_TIMEOUT_MS)  # a dialog, a new tab, a pop-up, or an iframe
    if found is None:
        last = page.context.pages[-1]
        recorder.snapshot("2_after_apply_now", last)
        recorder.step("the form never appeared", page_url=last.url, buttons=_buttons(last))
        recorder.note("page_facts", _page_facts(last))
        return Attempt("failed", "the application form didn't show up after pressing APPLY NOW",
                       result_screenshot=_screenshot(last))
    page, scope = found
    form_url = page.url
    timeline["form_found_utc"] = _utc_ms()
    recorder.step("form found", page_url=form_url, frames=[f.url for f in page.frames],
                  inside_form_element=not hasattr(scope, "goto"))
    if record:
        fields = _inventory(scope)
        recorder.note("apartment_named_on_form", bool(apartment) and apartment in _page_text(page))
        recorder.note("form_fields", fields)
        recorder.note("form_buttons", _buttons(scope))
        # Nothing is being sent, so there's time to look closer: how the form
        # is sent (action, method, hidden fields, CAPTCHA widgets) and how the
        # page is built and loads.
        recorder.note("form_facts", _form_facts(scope))
        recorder.note("page_facts", _page_facts(page))
        recorder.snapshot("2_form_empty", page)
        recorder.image("2_form_empty", _screenshot(page))
        if values is None:
            return Attempt("recorded", "form recorded; nothing filled or sent",
                           form_fields=[f["label"] for f in fields], form_url=form_url)
    held = _hold_outgoing(page) if capture else None

    filled, problems, labels = [], [], []
    radios = dict(values.get("radio_choices") or {})
    submit_button = None
    for form_page in range(1, MAX_FORM_PAGES + 1):
        page_filled, page_problems, fill_details = _fill_visible_fields(scope, values, radios)
        filled += page_filled
        fields = _inventory(scope)
        labels += [f["label"] for f in fields]
        empty_required = [f["label"] for f in fields if f["required"] and f["empty"]]
        problems = list(dict.fromkeys(page_problems + empty_required))
        submit_button = _visible_button(scope, SUBMIT_BUTTON)
        next_button = None if submit_button else _visible_button(scope, NEXT_BUTTON)
        recorder.step(f"filled page {form_page} of the form", filled=page_filled, problems=problems,
                      fill_details=fill_details,
                      fields_after=[{"label": f["label"], "required": f["required"], "empty": f["empty"]}
                                    for f in fields],
                      submit_button=_text_of(submit_button), next_button=_text_of(next_button))
        if submit_button or next_button is None or problems:
            break
        next_button.click()
        page.wait_for_timeout(800)  # let the next page of the form render

    recorder.note("form_fields", fields)  # as they stand after filling (labels and HTML, never values)
    # Every field StuyTown requires must have been found and filled: if one
    # wasn't, its label probably changed, and the form isn't safe to send.
    not_found = [key for key in REQUIRED_PROFILE_KEYS if values.get(key) not in (None, "") and key not in filled]
    if submit_button is not None and not_found:
        problems += [f"no field found for {key}" for key in not_found]
    found_so_far = {"filled": filled, "problems": problems, "form_fields": list(dict.fromkeys(labels)),
                    "form_url": form_url, "timeline": timeline}
    captcha = _has_captcha(page)
    recorder.note("captcha", captcha)

    def stop(status, detail):  # before SUBMIT: the filled form, as it stands
        recorder.snapshot("3_form_filled", page)
        return Attempt(status, detail, filled_screenshot=_screenshot(page), **found_so_far)

    if submit_button is None:
        why = "required fields are empty" if problems else "no SUBMIT or Next button found"
        return stop("incomplete", f"stopped on page {form_page} of the form: {why}")
    if capture:
        return _capture_request(page, submit_button, held, recorder, problems, found_so_far)
    if not submit:
        state = "everything required is filled" if not problems else "some required fields aren't right"
        return stop("filled", f"{state}; SUBMIT was NOT pressed (form check only)")
    if problems:
        return stop("incomplete", "required fields are empty or didn't take the value, so it wasn't submitted")
    if captcha:
        return stop("blocked", "the form has a CAPTCHA, which this doesn't solve")

    # The site's answer comes back as the response to the request SUBMIT
    # sends: that's what decides the outcome, the moment it arrives.
    answers, lost = [], []
    page.on("response", lambda r: answers.append(r) if _is_submission(r.request) else None)
    page.on("requestfailed", lambda r: lost.append(r) if _is_submission(r) else None)
    messages_before = set(_site_messages(page))
    text_before = _page_text(page)  # to find what the site says once SUBMIT is pressed
    confirmation_already_showing = _confirms(text_before, site)
    recorder.step("pressing SUBMIT")
    timeline["submit_pressed_utc"] = _utc_ms()
    if not _press(submit_button, page, recorder):
        return Attempt("failed", "the SUBMIT click didn't reach the button, so nothing was sent",
                       result_screenshot=_screenshot(page), **found_so_far)
    recorder.step("pressed SUBMIT")
    filled_shot = _screenshot(page)  # right after: the form still shows what was sent
    recorder.snapshot("3_form_filled", page)
    recorder.step("screenshot of the filled form taken")

    def finish(status, detail, captured=None, **extra):
        timeline.setdefault("answer_utc", _utc_ms())
        recorder.step(f"outcome: {status}", page_url=page.url, site_messages=extra.get("site_messages", []))
        # The words the page gained after SUBMIT: the site's real confirmation
        # (or complaint), to tune CONFIRMATION and SITE_ERROR from.
        recorder.note("text_after_submit_new", _new_lines(text_before, _page_text(page)))
        recorder.snapshot("4_after_submit", page)
        recorder.note("form_buttons", _buttons(page))
        recorder.note("page_facts", _page_facts(page))
        return Attempt(status, detail, filled_screenshot=filled_shot, result_screenshot=_screenshot(page),
                       captured=captured, **extra, **found_so_far)

    deadline = time.monotonic() + CONFIRMATION_TIMEOUT_MS / 1000
    complaints, changed, ticks = 0, False, 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(100)
        ticks += 1
        if answers:
            response = answers[0]
            timeline["answer_utc"] = _utc_ms()
            text = _safe_text(response)
            status, detail, server_time = direct_submit.interpret(response.status, text, site)
            timeline["server_submitted_utc"] = server_time
            captured = {"url": response.request.url, "headers": dict(response.request.headers),
                        "body": _json_or_none(response.request.post_data)}
            if status in ("submitted", "unconfirmed"):
                return finish(status, detail, captured=captured)
            if status == "unknown":  # it may have gone through: never sent again, so check your email
                return finish("unconfirmed", detail)
            page.wait_for_timeout(700)  # let the page show its own message
            messages = [m for m in _site_messages(page) if m not in messages_before]
            return finish("rejected", detail, site_messages=messages)
        if lost:
            return finish("unconfirmed", f"the application request failed in the browser ({lost[0].failure}); "
                          "it may not have arrived")
        if ticks % 5:
            continue  # the page itself is only read every 0.5 s
        if not changed and (page.url != form_url or _page_text(page) != text_before):
            changed = True  # how long the site takes to react at all
            timeline["first_change_utc"] = _utc_ms()
            recorder.step("the page changed after SUBMIT", page_url=page.url)
        text = _page_text(page)
        if _confirms(text, site) and (not confirmation_already_showing or not _still_visible(submit_button)):
            return finish("submitted", "the site confirmed the application")
        # The form is still there and showing new error text: the site
        # refused it (nothing was sent). Two checks in a row, to let it settle.
        new_messages = [m for m in _site_messages(page) if m not in messages_before and SITE_ERROR.search(m)]
        complaints = complaints + 1 if new_messages and _still_visible(submit_button) else 0
        if complaints >= 2:
            return finish("rejected", "the site refused the form", site_messages=new_messages)
    return finish("unconfirmed", f"pressed SUBMIT but no confirmation appeared within "
                  f"{CONFIRMATION_TIMEOUT_MS // 1000}s", site_messages=_site_messages(page))


def _capture_request(page, submit_button, held, recorder, problems, found_so_far) -> Attempt:
    """Press SUBMIT with every outgoing request held back (see
    _hold_outgoing): the site's form builds its request, and it's kept
    instead of sent."""
    if problems:
        return Attempt("recorded", "form recorded; not every field took its value, so SUBMIT wasn't pressed",
                       **found_so_far)
    went_out = []
    page.on("response", lambda r: went_out.append(r.request.url) if r.request.method not in (
        "GET", "HEAD", "OPTIONS") else None)
    recorder.step("pressing SUBMIT with every outgoing request held back -- nothing is sent")
    if not _press(submit_button, page, recorder):
        return Attempt("recorded", "form recorded; the SUBMIT click missed, so no request was captured",
                       **found_so_far)
    deadline = time.monotonic() + 5
    while not held and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    page.wait_for_timeout(200)
    if went_out:  # can't happen with the hold in place; said loudly if it ever does
        print(f"   WARNING: a request left the recording's page despite the hold: {went_out[0]}")
        recorder.note("request_went_out", went_out)
    recorder.step("request held back" if held else "no request was made", count=len(held),
                  where=[h["url"].split("?")[0] for h in held])
    captured = next((h for h in held if isinstance(h.get("body"), dict)), None)
    return Attempt("recorded", "form recorded and the site's request captured; nothing was sent" if captured
                   else "form recorded; no request was captured", captured=captured, **found_so_far)


def _is_submission(request) -> bool:
    return request.method not in ("GET", "HEAD", "OPTIONS") and request.resource_type in ("xhr", "fetch")


def _safe_text(response) -> str:
    try:
        return response.text() or ""
    except Exception:
        return ""


def _confirms(text: str, site: dict | None) -> bool:
    """Does the page show the site's confirmation (its own success message
    when known, else what confirmations usually say)?"""
    wanted = (site or {}).get("success_message")
    if wanted and _squash(wanted[:60]) in _squash(text):
        return True
    return bool(CONFIRMATION.search(text or ""))


def _press(button, page, recorder) -> bool:
    """Click and check the click reached the button; once more if it didn't
    (a miss sends nothing, so a second click can't apply twice)."""
    for attempt in (1, 2):
        try:
            button.evaluate(_HIT_PROBE_JS)
        except Exception:
            pass
        button.scroll_into_view_if_needed()
        button.click()
        try:
            hit = page.evaluate("() => window.__autoApplyHit !== false")
        except Exception:
            hit = True  # the page moved on, so the click did something
        if hit:
            return True
        recorder.step("the SUBMIT click missed the button", try_number=attempt)
        page.evaluate("() => window.scrollTo(0, 0)")
    return False


def _buttons(scope) -> list:
    try:
        return _evaluate(scope, _BUTTONS_JS)
    except Exception:
        return []


def _text_of(element) -> str | None:
    if element is None:
        return None
    try:
        return (element.inner_text() or element.get_attribute("value") or "").strip()[:80]
    except Exception:
        return None


def _page_text(page) -> str:
    try:
        return page.evaluate("() => document.body.innerText")
    except Exception:
        return ""


def _new_lines(before: str, after: str, limit: int = 60) -> list:
    """Visible lines on the page now that weren't there before (redacted when
    the recording is written)."""
    old = {line.strip() for line in before.splitlines()}
    new = [line.strip()[:200] for line in after.splitlines() if line.strip() and line.strip() not in old]
    return list(dict.fromkeys(new))[:limit]


def _utc_ms() -> str:
    """Now, in UTC to the millisecond, for the timeline."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# How the page is built and how it loaded: its framework, its timings,
# what it fetched (by type, and the slowest), and whether it keeps a
# service worker or anything in the browser's storage (names only).
_PAGE_FACTS_JS = r"""() => {
    const w = window, nav = performance.getEntriesByType('navigation')[0] || {};
    const ms = v => (typeof v === 'number' ? Math.round(v) : null);
    const byType = {};
    const resources = performance.getEntriesByType('resource');
    for (const r of resources) {
        const t = byType[r.initiatorType] = byType[r.initiatorType] || {count: 0, kb: 0, slowest_ms: 0};
        t.count++; t.kb += (r.transferSize || 0) / 1024; t.slowest_ms = Math.max(t.slowest_ms, Math.round(r.duration));
    }
    Object.values(byType).forEach(t => { t.kb = Math.round(t.kb); });
    const keys = store => { try { return Object.keys(store).slice(0, 40); } catch (e) { return null; } };
    return {
        frameworks: {
            next: !!w.__NEXT_DATA__, nuxt: !!w.__NUXT__,
            react: !!document.querySelector('[data-reactroot]') || !!w.React,
            angular: !!document.querySelector('[ng-version]') || !!w.ng,
            vue: !!w.Vue || !!document.querySelector('[data-v-app]'),
            jquery: !!w.jQuery, gatsby: !!w.___gatsby, svelte: !!document.querySelector('[class*=svelte-]'),
        },
        generator: (document.querySelector('meta[name=generator]') || {}).content || null,
        timing_ms: {response_start: ms(nav.responseStart), dom_content_loaded: ms(nav.domContentLoadedEventEnd),
                    load: ms(nav.loadEventEnd), page_kb: nav.transferSize ? Math.round(nav.transferSize / 1024) : null,
                    since_navigation: ms(performance.now())},
        resources_by_type: byType,
        slowest_resources: resources.slice().sort((a, b) => b.duration - a.duration).slice(0, 8)
            .map(r => ({url: r.name.split('?')[0].slice(0, 200), type: r.initiatorType, ms: Math.round(r.duration),
                        started_ms: Math.round(r.startTime)})),
        service_worker: !!(navigator.serviceWorker && navigator.serviceWorker.controller),
        local_storage_keys: keys(w.localStorage), session_storage_keys: keys(w.sessionStorage),
        cookies_visible_to_scripts: (document.cookie || '').split(';').map(c => c.split('=')[0].trim()).filter(Boolean),
    };
}"""

# How the form is sent: the <form>'s own attributes, its hidden fields
# (names and value lengths -- a long one is usually a token), and any
# CAPTCHA widget with its (public) site key.
_FORM_FACTS_JS = r"""root => {
    const form = root.closest ? (root.closest('form') || root.querySelector('form')) : root.querySelector('form');
    const attrs = el => el ? {action: el.getAttribute('action'), method: el.getAttribute('method'),
                              enctype: el.getAttribute('enctype'), id: el.id || null, name: el.getAttribute('name'),
                              novalidate: el.hasAttribute('novalidate')} : null;
    const doc = root.ownerDocument || root;
    return {
        form: attrs(form),
        forms_on_page: doc.querySelectorAll('form').length,
        hidden_fields: [...(form || doc).querySelectorAll('input[type=hidden]')].slice(0, 30)
            .map(el => ({name: el.name || el.id || null, value_length: (el.value || '').length})),
        captcha_widgets: [...doc.querySelectorAll('.g-recaptcha, [data-sitekey], .h-captcha, .cf-turnstile, '
                                                  + 'iframe[src*=captcha], iframe[src*=challenges]')]
            .slice(0, 5).map(el => ({tag: el.tagName.toLowerCase(),
                                     class: (el.className || '').toString().slice(0, 80) || null,
                                     sitekey: el.getAttribute('data-sitekey'), size: el.getAttribute('data-size'),
                                     src: el.src ? el.src.split('?')[0] : null})),
        scripts_with_captcha: [...doc.querySelectorAll('script[src]')].map(s => s.src.split('?')[0])
            .filter(src => /captcha|turnstile|challenge/i.test(src)).slice(0, 5),
    };
}"""


def _page_facts(page) -> dict:
    try:
        return page.evaluate(_PAGE_FACTS_JS)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def _form_facts(scope) -> dict:
    try:
        return _evaluate(scope, _FORM_FACTS_JS)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


# Finds the page's own APPLY button (not one in the site's header, menu or
# footer, unless allow_site_chrome) and tags it data-autoapply-apply.
_FIND_APPLY_JS = r"""allowSiteChrome => {
    const text = el => (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
    const shown = el => { const b = el.getBoundingClientRect();
        return b.width > 0 && b.height > 0 && getComputedStyle(el).visibility !== 'hidden'; };
    const all = [...document.querySelectorAll('button, a, [role=button], input[type=submit], input[type=button]')]
        .filter(el => !el.disabled && /^apply\b/i.test(text(el)) && shown(el));
    const pick = all.find(el => !el.closest('header, nav, footer')) || (allowSiteChrome ? all[0] : null);
    document.querySelectorAll('[data-autoapply-apply]').forEach(el => el.removeAttribute('data-autoapply-apply'));
    if (!pick) return false;
    pick.setAttribute('data-autoapply-apply', '1');
    return true;
}"""


def _wait_for_apply_button(page, timeout_ms: int):
    """The page's own APPLY NOW button, the moment the page draws it -- the
    browser checks on every frame it paints, instead of every 250 ms. One in
    the site's header, menu or footer only counts if the page never draws
    its own."""
    try:
        page.wait_for_function(_FIND_APPLY_JS, arg=False, polling="raf", timeout=timeout_ms)
    except Exception:
        try:
            if not page.evaluate(_FIND_APPLY_JS, True):
                return None
        except Exception:
            return None
    return page.locator("[data-autoapply-apply]").first


def _visible_button(scope, name: re.Pattern, role: str = "button", allow_site_chrome: bool = True):
    """The first visible match, preferring one outside the site's header,
    menu and footer. Below-the-fold buttons count as visible."""
    buttons = scope.get_by_role(role, name=name)
    visible = [buttons.nth(i) for i in range(min(buttons.count(), 10)) if buttons.nth(i).is_visible()]
    for button in visible:
        if button.evaluate("el => !el.closest('header, nav, footer')"):
            return button
    return visible[0] if visible and allow_site_chrome else None


def _find_form(context, timeout_ms: int, pages=None):
    """(page, scope) for the application form: the <form> around its name
    fields, or the whole frame if there isn't one. Looks in every tab and
    frame, newest tab first, every 50 ms."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for page in pages or reversed(context.pages):
            for frame in page.frames:
                try:
                    where = frame.evaluate(f"() => ({_FIND_FORM_JS})(document)")
                except Exception:
                    continue  # a frame that went away mid-look
                if where == "form":
                    return page, frame.locator("[data-autoapply-form]").first
                if where == "frame":
                    return page, frame
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _find_form_on(page, timeout_ms: int):
    """_find_form, on this page only."""
    return _find_form(page.context, timeout_ms, pages=[page])


def _evaluate(scope, function_js: str):
    """Run `root => ...` against a <form> locator or a whole frame."""
    if hasattr(scope, "goto"):  # a Frame
        return scope.evaluate(f"() => ({function_js})(document)")
    return scope.evaluate(function_js)


def _inventory(scope) -> list:
    try:
        return _evaluate(scope, _INVENTORY_JS)
    except Exception:
        return []  # the form navigated away (e.g. after SUBMIT)


def _fill_visible_fields(scope, values: dict, radios: dict) -> tuple:
    """Fill what's on screen now and check each value took. Returns (profile
    keys filled, labels of fields that wouldn't take their value, details):
    details say, per field, how the value went in (a plain fill, a retype key
    by key, which spelling) and, for boxes that reformat what's typed, the
    shape it ended up in ("+# ### ### ####") -- never the value itself."""
    fields = _inventory(scope)
    used, filled, problems, details = set(), [], [], []

    def fill(key, pattern, value):
        for f in fields:
            if f["index"] in used or not pattern.search(f["label"].lstrip("* ")):
                continue
            used.add(f["index"])
            element = scope.locator(f'[data-autoapply-field="{f["index"]}"]')
            ok, how, shown = _put(element, f, key, value)
            (filled if ok else problems).append(key if ok else f["label"])
            detail = {"label": f["label"], "key": key, "took": ok, "how": how}
            if key in SHAPE_KEYS and shown is not None:
                detail["shape"] = value_shape(shown)
            details.append(detail)
            return

    for key, pattern, _ in FORM_FIELDS:
        value = values.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            fill(key, re.compile(pattern, re.I), value)
    for label, value in (values.get("extra_fields") or {}).items():
        if value not in (None, ""):
            fill(f"extra_fields: {label}", re.compile(re.escape(str(label)), re.I), value)
    for question, answer in list(radios.items()):
        if _choose_radio(scope, str(question), str(answer)):
            filled.append(f"radio_choices: {question}")
            del radios[question]  # answered; later pages don't need it
    return filled, problems, details


# Boxes whose formatting is worth recording (as a shape, never the value).
SHAPE_KEYS = (*PHONE_KEYS, "annual_income", "household_size", "zip", "state", "building", "apartment_no")


def value_shape(text: str) -> str:
    """'+1 (212) 555-0123' -> '+# (###) ###-####', 'NY' -> 'AA': how a box
    formats what's typed, without what was typed."""
    return re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"\d", "#", str(text))))[:40]


def _put(element, f: dict, key: str, value) -> tuple:
    """Put one value in one field, then read it back. Masked boxes (the
    phone's "+", the "$ 0.00" income box) can reformat or reject what's
    typed, so if the first way doesn't stick, it's typed key by key, and
    alternative spellings are tried. Returns (took, how, what the box shows):
    took is True only once the box shows the value."""
    shown = None
    try:
        if f["type"] == "checkbox":
            tick = value if isinstance(value, bool) else str(value).strip().lower() in ("true", "yes", "y", "1")
            element.set_checked(tick, force=True, timeout=5000)
            return element.is_checked() == tick, "checkbox", None
        if isinstance(value, bool):
            return False, "true/false only fits a checkbox", None
        if f["tag"] == "select":
            option = _matching_option(element, str(value))
            if option is not None:
                element.select_option(value=option, timeout=5000)
            return option is not None, "select" if option is not None else "no matching option", None
        for n, text in enumerate(_spellings(key, value), 1):
            spelling = f", spelling {n}" if n > 1 else ""
            element.fill(text, timeout=5000)
            shown = element.input_value()
            if _shows(key, shown, value):
                return True, "fill" + spelling, shown
            element.click(timeout=5000)
            element.press("ControlOrMeta+A")
            element.press("Backspace")
            element.press_sequentially(text, delay=20)
            shown = element.input_value()
            if _shows(key, shown, value):
                return True, "typed key by key" + spelling, shown
        return False, "never showed the value", shown
    except Exception as e:
        return False, f"error: {type(e).__name__}", shown


def _spellings(key: str, value) -> list:
    """Ways of typing a value, best first."""
    if key in PHONE_KEYS:
        digits = phone_digits(value) or re.sub(r"\D", "", str(value))
        return [f"+{digits}", digits[1:]]  # "+12125550123"; some boxes add the "+1" themselves
    if key == "annual_income":
        amount = check_units.number(value) or 0
        whole = f"{amount:.0f}" if amount == int(amount) else f"{amount:.2f}"
        return list(dict.fromkeys([whole, f"{amount:.2f}", f"{amount:.2f}".replace(".", "")]))  # last: a cents-first box
    if key == "household_size":
        return [str(int(check_units.number(value) or 0))]
    return [str(value).strip()]


def _shows(key: str, shown: str, value) -> bool:
    """Does the box now show the value (allowing for its own formatting)?"""
    if key in PHONE_KEYS:
        wanted, got = phone_digits(value), re.sub(r"\D", "", shown)
        # With a leading "+" the country code is part of what's shown: "+212 555..." would be Morocco.
        return got == wanted if shown.strip().startswith("+") else got in (wanted, (wanted or "")[1:])
    if key in ("annual_income", "household_size"):
        return check_units.number(shown) == check_units.number(value)
    return _squash(shown) == _squash(value)


def _squash(text) -> str:
    """'Example  Street' and 'example street' compare equal."""
    return re.sub(r"[^0-9a-z@.]", "", str(text).casefold())


def _matching_option(select, wanted: str) -> str | None:
    options = select.evaluate("el => [...el.options].map(o => [o.value, o.text.trim()])")
    wanted = wanted.strip().casefold()
    for value, text in options:
        if value and wanted in (value.casefold(), text.casefold()):
            return value
    for value, text in options:
        if value and wanted and text.casefold().startswith(wanted):
            return value  # "1" -> "1 person"
    return None


def _choose_radio(scope, question: str, answer: str) -> bool:
    asked = re.compile(re.escape(question), re.I)
    groups = scope.get_by_role("radiogroup", name=asked)
    if not groups.count():
        groups = scope.locator("fieldset").filter(has_text=asked)
    if not groups.count():
        return False  # maybe on a later page of the form
    choice = groups.last.get_by_label(re.compile(rf"^\W*{re.escape(answer)}\W*$", re.I))
    if not choice.count() or not choice.first.is_visible():
        return False
    choice.first.check(force=True, timeout=5000)
    return True


def _has_captcha(page) -> bool:
    """A CAPTCHA a person has to click (invisible ones are left alone)."""
    for frame in page.frames:
        url = frame.url
        if ("recaptcha" in url and "/anchor" in url and "size=invisible" not in url) \
                or ("hcaptcha" in url and "frame=checkbox" in url) or "challenges.cloudflare.com" in url:
            return True
    return False


def _confirmation_showing(page) -> bool:
    for frame in page.frames:
        try:
            texts = frame.get_by_text(CONFIRMATION)
            if any(texts.nth(i).is_visible() for i in range(min(texts.count(), 5))):
                return True
        except Exception:
            continue
    return False


def _site_messages(page) -> list:
    """Error text the site is showing (e.g. "This field is required")."""
    try:
        return page.evaluate("""() => [...document.querySelectorAll(
                '[role=alert], [aria-live], [class*=error i], [class*=invalid i], [class*=helper i]')]
            .filter(el => el.offsetParent && el.innerText.trim())
            .map(el => el.innerText.replace(/\\s+/g, ' ').trim().slice(0, 100)).slice(0, 5)""")
    except Exception:
        return []


def _still_visible(element) -> bool:
    try:
        return element.is_visible()
    except Exception:
        return False


def _screenshot(page) -> bytes | None:
    """A full-page JPEG. Scrolls back to the top afterwards: a full-page
    screenshot otherwise leaves the page so that the next click lands beside
    its target (it made SUBMIT miss in testing)."""
    try:
        shot = page.screenshot(full_page=True, type="jpeg", quality=60)
        if len(shot) > PUSHOVER_IMAGE_LIMIT:
            shot = page.screenshot(type="jpeg", quality=60)  # just the visible part
        return shot
    except Exception:
        return None
    finally:
        try:
            page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass


def _screenshot_of_last_page(context) -> bytes | None:
    try:
        return _screenshot(context.pages[-1]) if context.pages else None
    except Exception:
        return None


# --------------------------------------------------------------- reporting

def result_alert(unit: dict, attempt: Attempt, link: str | None = None) -> dict:
    """One normal-priority alert with the outcome: "Applied" on success, a
    separate "didn't go through" message otherwise. Success carries the
    filled-in form; a failure carries what the page showed at the end."""
    label = check_units.unit_label(unit)
    if attempt.status == "submitted":
        title = f"Applied: {label}"
    elif attempt.status == "unconfirmed":
        title = f"Auto-apply unconfirmed: {label} - check your email"
    elif attempt.status == "filled":
        title = f"[FORM CHECK] Filled the form for {label} - not submitted"
    else:
        title = f"Auto-apply failed: {label} - apply yourself now"
    lines = [check_units._linked_line(unit), html.escape(attempt.detail)]
    if attempt.problems:
        lines.append("Empty or not taking the value: " + html.escape("; ".join(attempt.problems)))
    if attempt.site_messages:
        lines.append("The site says: " + html.escape("; ".join(attempt.site_messages)))
    if attempt.filled and attempt.timeline.get("form_via") == "direct (API)":
        took = attempt.timeline.get("api_seconds")
        lines.append(f"Sent straight to StuyTown's application API ({len(attempt.filled)} fields from your "
                     f"profile)" + (f"; it answered in {took:.1f}s." if took is not None else "."))
    elif attempt.filled:
        lines.append(f"Filled {len(attempt.filled)} fields from your profile in {attempt.seconds:.1f}s.")
    if attempt.status in ("submitted", "unconfirmed"):
        lines.append(NEXT_STEP_REMINDER)
    if attempt.status in ("submitted", "filled"):
        image, name = attempt.filled_screenshot or attempt.result_screenshot, "filled-form.jpg"
    else:
        image, name = attempt.result_screenshot or attempt.filled_screenshot, "page.jpg"
    return {
        "title": title,
        "message": check_units._fit(lines),
        "priority": 0,
        "url": link or check_units.unit_url(unit),
        "url_title": "Open this unit",
        **({"attachment": (name, image, "image/jpeg")} if image else {}),
    }


def save_private_screenshots(unit: dict, attempt: Attempt) -> None:
    """The screenshots, kept in the gitignored private/ folder (they show
    your details; on GitHub's runner they're gone when the run ends)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    for name, shot in (("filled-form", attempt.filled_screenshot), ("result", attempt.result_screenshot)):
        if shot:
            path = Path(PRIVATE_DIR) / f"{stamp}_{check_units.apartment(unit)}_{name}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(shot)


# --------------------------------------------------------------------- CLI


def _serve_selftest_site() -> http.server.ThreadingHTTPServer:
    handler = functools.partial(SelftestSiteHandler, directory=str(SELFTEST_SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class SelftestSiteHandler(http.server.SimpleHTTPRequestHandler):
    """The fake site: the unit page (with this server's address and the
    site's code version filled in), the units API, and an application API
    that answers like StuyTown's: it checks the key and every field, and
    refuses a second application to the same apartment."""

    site_version = "1001"
    api_key = "fake-key-0000"
    units = {}  # unitSpk -> unit, for the units API; any other gets a stand-in
    applications = []  # every request the fake application API got (the tests read it)
    fail_next, fail_status = 0, 503  # the API fails this many of the next requests (tests)
    KEYS = ["unitSpk", "firstName", "lastName", "cellPhone", "householdSize", "email", "totalAnnualEarnings",
            "buildingAddress", "workPhone", "address", "version"]

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/ah-units/"):
            spk = urllib.parse.unquote(path.rsplit("/", 1)[1])
            return self._json(200, self.units.get(spk) or _stand_in_unit(spk))
        if path.startswith("/sb/"):
            return self._send(200, b"/* the site's code */", "application/javascript")
        if path == "/unit.html":
            page = (SELFTEST_SITE / "unit.html").read_text(encoding="utf-8")
            page = page.replace("{{ORIGIN}}", f"http://{self.headers.get('Host')}")
            return self._send(200, page.replace("{{SITE_VERSION}}", self.site_version).encode(), "text/html")
        super().do_GET()

    def do_POST(self):
        cls = SelftestSiteHandler
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if urllib.parse.urlsplit(self.path).path != "/affordable-housing/create-application":
            return self._send(404, b"not found", "text/plain")
        if self.headers.get("Subscription-Key") != cls.api_key:
            return self._json(401, {"message": "Access denied due to missing subscription key."})
        try:
            sent = json.loads(raw)
        except ValueError:
            return self._json(400, {"message": "The request body is not valid JSON."})
        record = {"body": sent, "headers": dict(self.headers), "created": False}
        cls.applications.append(record)
        if cls.fail_next > 0:
            cls.fail_next -= 1
            return self._json(cls.fail_status, {"message": "fake failure"})
        if not isinstance(sent, dict) or list(sent) != cls.KEYS or not isinstance(sent.get("address"), dict):
            return self._json(400, {"message": "One or more validation errors occurred."})
        if any(a["created"] and a["body"]["unitSpk"] == sent["unitSpk"] and a["body"]["email"] == sent["email"]
               for a in cls.applications):
            return self._json(409, {"message": "The applicant already has an open application for this unit."})
        record["created"] = True
        return self._json(200, {
            "token": f"fake-token-{len(cls.applications)}", "unitSpk": sent["unitSpk"],
            "submittedOn": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z",
            "totalAnnualEarnings": check_units.number(sent["totalAnnualEarnings"]),
            "applyingAddress": f"{sent['buildingAddress']}, Apt {sent['unitSpk'].split('~')[-1]}",
            "status": "Created",
            "contacts": [{"id": len(cls.applications), "firstName": sent["firstName"], "lastName": sent["lastName"],
                          "email": sent["email"], "role": "MainApplicant", "cellPhone": sent["cellPhone"],
                          "address": sent["address"]}],
        })

    def _json(self, status: int, value) -> None:
        self._send(status, json.dumps(value).encode(), "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _stand_in_unit(spk: str) -> dict:
    return {"unitSpk": spk, "name": spk.split("~")[-1], "building": {"address": "FAKE FORM, NOT A REAL UNIT"},
            "bedrooms": 1, "bathrooms": 1, "price": 2850.0, "incomeRequirement": 60000, "version": 1}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--selftest", action="store_true",
                        help="fill and submit the FAKE copy of the form in tests/fixtures/apply_site with your profile")
    target.add_argument("--check-form", metavar="UNIT_URL",
                        help="fill a real unit's form on your own computer, screenshot it, never submit")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args(argv)

    try:
        profile = load_profile()
    except ProfileError as e:
        print(f"ERROR: {e}")
        return 1
    if profile is None:
        if not args.selftest:
            print(f"ERROR: no applicant profile (set {PROFILE_ENV}, or create {PROFILE_FILE}).")
            return 1
        print(f"No applicant profile found -- using the made-up one in {EXAMPLE_PROFILE.name}.")
        profile = json.loads(EXAMPLE_PROFILE.read_text(encoding="utf-8"))

    if args.selftest:
        return _selftest(profile)
    return _check_form(args.check_form, profile, headed=args.headed)


def _selftest(profile: dict) -> int:
    """The real applier -- warm browser worker, queue, background alerts and
    logging -- pointed at the fake site: first a recording of an over-limit
    fake unit, which captures the site's request (sending nothing) and
    proves the direct-submit format; then one qualifying fake unit, applied
    to directly (or by the browser, if the format couldn't be proven)."""
    server = _serve_selftest_site()
    base = f"http://127.0.0.1:{server.server_port}"
    check_units.TITLE_PREFIX = "[TEST] "
    check_units.UNIT_PAGE_URL = f"{base}/unit.html"
    check_units.LISTINGS_URL = f"{base}/unit.html"
    global APPLICATIONS_FILE, MAX_RENT
    MAX_RENT = MAX_RENT or 2850  # the fake unit always qualifies: its rent is set to your limit
    APPLICATIONS_FILE = f"{SELFTEST_RUNS_DIR}/applications.json"  # never the real log
    direct_submit.API_FORMAT_FILE = f"{SELFTEST_RUNS_DIR}/apply_api.json"
    check_units.EVENTS_FILE = f"{SELFTEST_RUNS_DIR}/events.json"  # not the real units
    apply_recorder.SITE_VERSIONS_FILE = f"{SELFTEST_RUNS_DIR}/site_versions.json"
    check_units.STATE_FILE = f"{SELFTEST_RUNS_DIR}/last_seen.json"
    for leftover in (APPLICATIONS_FILE, direct_submit.API_FORMAT_FILE, check_units.EVENTS_FILE,
                     apply_recorder.SITE_VERSIONS_FILE):
        if os.path.exists(leftover):
            os.remove(leftover)
    building = {"address": "FAKE FORM, NOT A REAL UNIT"}
    over = {"unitSpk": "P~SELF~U~OVER", "name": "OVER", "building": building, "price": MAX_RENT + 1000,
            "incomeRequirement": 0, "bedrooms": 1, "bathrooms": 1, "version": 2}
    unit = {"unitSpk": "P~SELF~U~TEST", "name": "TEST", "building": building, "price": MAX_RENT,
            "incomeRequirement": 0, "bedrooms": 1, "bathrooms": 1, "version": 1}
    SelftestSiteHandler.units = {u["unitSpk"]: u for u in (over, unit)}
    SelftestSiteHandler.applications = []
    notifier, logger = background.Worker("notifier"), background.Worker("logger")
    applier = Applier(profile, notifier, logger, workers=1, runs_dir=SELFTEST_RUNS_DIR)
    logger.yield_to = applier.busy
    try:
        applier.start()
        print("1. Recording an over-limit fake unit's form (nothing is sent)...")
        applier.dispatch([over])
        deadline = time.monotonic() + 60
        while applier.direct.why_not() and time.monotonic() < deadline:
            time.sleep(0.2)
        print(f"   direct submit: {'ready' if applier.direct.why_not() is None else applier.direct.why_not()}")
        print("2. Applying to the qualifying fake unit...")
        started = time.monotonic()
        applier.dispatch([unit])
        applier.wait_idle(60)
        print(f"   applied in {time.monotonic() - started:.2f}s after being handed the unit")
        applier.stop(10)
        notifier.drain(60)
        logger.drain(60)
    finally:
        server.shutdown()
    attempts = applier.applications.get(unit["unitSpk"], {}).get("attempts", [])
    last = attempts[-1] if attempts else {"status": "none", "detail": "no attempt was made"}
    received = SelftestSiteHandler.applications
    via = (last.get("timeline") or {}).get("form_via")
    print(f"Result: {last['status']} -- {last['detail']} (via {via})")
    print(f"Sent from your profile: {', '.join(last.get('fields_filled') or []) or 'nothing'}")
    print(f"Applications the fake site received: {len(received)} "
          "(the recording must have sent none)")
    if last.get("problems"):
        print(f"Required fields empty or not taking the value: {'; '.join(last['problems'])}")
    if last.get("recording"):
        print(f"Recording (details redacted): {last['recording']}/")
    ok = last["status"] == "submitted" and not last.get("problems") and len(received) == 1
    return 0 if ok else 1


def _check_form(url: str, profile: dict, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    spk = re.search(r"unitSpk=([^&]+)", url)
    unit = {"unitSpk": spk[1] if spk else "manual", "name": spk[1].rsplit("~", 1)[-1] if spk else "manual",
            "building": {"address": "form check from the command line"}}
    recorder = apply_recorder.RunRecorder(apply_recorder.run_folder(check_units.apartment(unit), "check",
                                                                    SELFTEST_RUNS_DIR),
                                          apply_recorder.Redactor(profile))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        try:
            page = new_context(browser).new_page()
            recorder.watch(page)
            started = time.monotonic()
            attempt = run_form(page, url, form_values(profile), submit=False, recorder=recorder,
                               apartment=check_units.apartment(unit))
            attempt.seconds = time.monotonic() - started
            folder = recorder.finish(_outcome(attempt))
        finally:
            browser.close()
    print(f"Result after {attempt.seconds:.1f}s: {attempt.status} -- {attempt.detail}")
    print(f"Form fields seen: {'; '.join(attempt.form_fields) or 'none'}")
    print(f"Filled from your profile: {', '.join(attempt.filled) or 'nothing'}")
    if attempt.problems:
        print(f"Required fields empty or not taking the value: {'; '.join(attempt.problems)}")
    print(f"Recording (details redacted): {folder}/")
    save_private_screenshots(unit, attempt)
    background.notify_with_retries(check_units.notify, result_alert(unit, attempt, link=url), attempts=2)
    return 0 if attempt.status == "filled" and not attempt.problems else 1


if __name__ == "__main__":
    sys.exit(main())
