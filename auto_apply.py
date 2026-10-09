"""
Auto-apply: when a listed unit's monthly rent is at or under your limit, open
the unit's page, press APPLY NOW, fill in StuyTown's application form from
your saved applicant profile, screenshot the filled-in form, press SUBMIT --
then send you the result with the screenshots.

The watcher (watch_loop.py) calls act_on_listings() on every check, right
after the new-unit alert has gone out. Two repository variables control it:

    AUTO_APPLY_MODE      on | off (default). Off = auto-apply does nothing at all.
    AUTO_APPLY_MAX_RENT  your rent limit in $/month, e.g. 3000 -- required when
                         on; there is no built-in default

Your details come from APPLICANT_PROFILE -- the whole JSON document, stored as
ONE GitHub secret -- or, on your own computer, the gitignored
applicant_profile.json. applicant_profile.example.json shows every key.

The form (affordable-housing.stuytown.com, as recorded 2026-10-08) is one page:

    First Name *   Last Name *   Email *   Cell Phone *   Work Phone
    Building *  Street name *  Apartment No.  City *  State  Zip *
    Household Size *   Household Gross Annual Income, $ *        [SUBMIT]

and submitting it gets you an email with a link to the detailed application,
which has to be completed within 24 hours.

Every trip through the form is recorded by apply_recorder.py into
data/apply_runs/ -- steps and timings, the form's fields, the page HTML, the
network traffic including what SUBMIT sends and what comes back -- with your
details redacted, so a failure can be studied and fixed. While on, it also
records (without filling anything) the form of up to
MAX_FORM_RECORDINGS_PER_RUN units over your limit each morning, so there's
real data even on days nothing qualifies.

The repo (and its Actions logs) are public, so nothing personal is ever
printed, committed or logged: in GitHub Actions every profile value is masked
in the log, screenshots of the filled-in form go only to your phone (as a
Pushover image) and the gitignored private/ folder, and data/applications.json
and data/apply_runs/ never contain your details.

    python auto_apply.py --selftest              # fill + submit a FAKE copy of the form (tests/fixtures/apply_site)
    python auto_apply.py --check-form URL        # fill a real unit's form on your computer -- never submits
    python auto_apply.py --check-form URL --headed   # same, with the browser window visible
"""

import argparse
import functools
import html
import http.server
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import apply_recorder
import check_units

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

# Safety limits. A unit is applied to at most once, ever (the site allows one
# application per apartment); an attempt that crashed or couldn't press
# SUBMIT gets one more try on a later check.
MAX_APPLICATIONS_PER_RUN = 3
MAX_FAILED_ATTEMPTS_PER_UNIT = 2
MAX_FORM_RECORDINGS_PER_RUN = 2  # over-limit units whose (unfilled) form gets recorded
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
        check_units._notify_best_effort({
            "title": "StuyTown auto-apply is off",
            "message": f"{html.escape(str(e))}. New-unit alerts still work.",
            "priority": 0,
        })
        return None


def startup_check() -> None:
    """Print what auto-apply will do this run (and mask the profile in the
    log before anything else prints). Called once when the watcher starts."""
    if _MODE_SETTING and not ENABLED and _MODE_SETTING != "off":
        print(f"WARNING: AUTO_APPLY_MODE={_MODE_SETTING!r} isn't on or off -- treating it as off.")
    if not ENABLED:
        print("Auto-apply: off (set the AUTO_APPLY_MODE repository variable to on to turn it on)")
        return
    if _profile_for_run():
        print(f"Auto-apply: ON for units at or under ${MAX_RENT:,.0f}/mo (applicant profile loaded)")


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


def already_handled(record: dict | None) -> str | None:
    """Why this unit shouldn't be tried (again), or None."""
    attempts = (record or {}).get("attempts", [])
    if any(a["status"] in ("submitted", "unconfirmed") for a in attempts):
        return "already applied"  # an unconfirmed submit may have gone through: never send a second
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


_skips_logged = set()  # unit IDs whose skip reason was already printed this run
_recorded_this_run = set()  # unit IDs whose form was recorded (without applying) this run
_submitted_this_run = 0


def act_on_listings(units: list) -> None:
    """The watcher's hook, called on every check with everything listed.
    Applies to each unit that qualifies and hasn't been handled yet,
    cheapest first -- then, with time to spare, records the form of a unit
    or two over your limit (nothing filled, nothing sent) for diagnosis."""
    global _submitted_this_run
    if not ENABLED or not units:
        return
    profile = _profile_for_run()
    if profile is None:
        return
    applications = load_applications()
    to_apply, to_record = [], []
    for unit in units:
        uid = check_units.unit_id(unit)
        not_eligible = skip_reason(unit, profile)
        reason = not_eligible or already_handled(applications.get(uid))
        if not reason:
            to_apply.append(unit)
            continue
        if uid not in _skips_logged:
            _skips_logged.add(uid)
            print(f"   Auto-apply: skipping {check_units.unit_label(unit)} -- {reason}")
        if not_eligible and uid not in _recorded_this_run \
                and len(_recorded_this_run) + len(to_record) < MAX_FORM_RECORDINGS_PER_RUN:
            to_record.append(unit)
    if not to_apply and not to_record:
        return

    from playwright.sync_api import sync_playwright  # only once there's something to do

    to_apply.sort(key=check_units.unit_rent)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for unit in to_apply:
                if _submitted_this_run >= MAX_APPLICATIONS_PER_RUN:
                    print(f"   Auto-apply: already submitted {MAX_APPLICATIONS_PER_RUN} applications this run "
                          f"-- not applying to {check_units.unit_label(unit)}")
                    break
                print(f"   Auto-apply: applying to {check_units.unit_label(unit)} -- {check_units.unit_url(unit)}")
                attempt = apply_to_unit(browser, unit, profile)
                print(f"   Auto-apply result after {attempt.seconds:.1f}s: {attempt.status} -- {attempt.detail}")
                if attempt.status in ("submitted", "unconfirmed"):
                    _submitted_this_run += 1
                _record(applications, unit, attempt)
                save_applications(applications)
                _report(unit, attempt)
            for unit in to_record:
                _recorded_this_run.add(check_units.unit_id(unit))
                attempt = apply_to_unit(browser, unit, profile, record_only=True)
                print(f"   Auto-apply: recorded the (unfilled) form of {check_units.unit_label(unit)} "
                      f"in {attempt.seconds:.1f}s -- {attempt.status}: {attempt.detail}")
        finally:
            browser.close()


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
    })


# ------------------------------------------------------------- the browser

APPLY_BUTTON = re.compile(r"^\s*apply\b", re.I)
SUBMIT_BUTTON = re.compile(r"^\s*(submit|send|finish|complete)\b|submit\s+(my\s+)?application", re.I)
NEXT_BUTTON = re.compile(r"^\s*(next|continue|proceed|save\s*(and|&)\s*continue)\b", re.I)
CONFIRMATION = re.compile(
    r"thank\s*you|application\s+(has\s+been\s+|was\s+)?(received|submitted|complete)|"
    r"\bsuccess(ful|fully)?\b|submission\s+(received|successful)|"
    r"check\s+your\s+(e-?mail|inbox)|we('ve|\s+have)\s+(received|sent)|confirmation\s+(number|#|code)", re.I)
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
    # | recorded (form recorded, nothing filled) | filled (--check-form: filled, not sent)
    status: str
    detail: str
    filled: list = field(default_factory=list)  # profile keys that went into the form (and were checked)
    problems: list = field(default_factory=list)  # the site's labels for required fields left empty or wrong
    form_fields: list = field(default_factory=list)  # every field label on the form, for spotting changes
    form_url: str = ""
    filled_screenshot: bytes | None = None  # the filled-in form just before SUBMIT -- shows your details
    result_screenshot: bytes | None = None  # the page at the end -- may show your details
    site_messages: list = field(default_factory=list)  # error text the site showed
    seconds: float = 0.0
    recording: Path | None = None  # data/apply_runs/... folder


def form_values(profile: dict) -> dict:
    """The profile as it goes into the form: everything but the "_" notes."""
    return {key: value for key, value in profile.items() if not str(key).startswith("_")}


def apply_to_unit(browser, unit: dict, profile: dict, record_only: bool = False,
                  runs_dir: str | None = None) -> Attempt:
    """One trip through the unit's form in a fresh browser session, recorded
    into data/apply_runs/. record_only: open the form and record it, but fill
    in nothing. Never raises."""
    recorder = apply_recorder.RunRecorder(
        apply_recorder.run_folder(check_units.apartment(unit), "record" if record_only else "apply", runs_dir),
        apply_recorder.Redactor(profile))
    recorder.note("unit", check_units.unit_summary(unit))  # public listing data
    recorder.note("kind", "recording only: nothing filled or sent" if record_only else "application")
    started = time.monotonic()
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        context.set_default_timeout(15000)
        _skip_unneeded_downloads(context)
        recorder.watch(context)
        page = context.new_page()
        attempt = run_form(page, check_units.unit_url(unit), None if record_only else form_values(profile),
                           submit=not record_only, recorder=recorder, apartment=check_units.apartment(unit))
    except Exception as e:
        recorder.step("crashed", error=f"{type(e).__name__}: {e}"[:1000])
        attempt = Attempt("failed", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}",
                          result_screenshot=_screenshot_of_last_page(context))
    attempt.seconds = time.monotonic() - started
    attempt.recording = recorder.finish(_outcome(attempt))  # while the browser is still open
    try:
        context.close()
    except Exception:
        pass
    return attempt


def _outcome(attempt: Attempt) -> dict:
    return {"status": attempt.status, "detail": attempt.detail, "seconds": round(attempt.seconds, 2),
            "form_url": attempt.form_url, "fields_filled": attempt.filled, "problems": attempt.problems,
            "site_messages": attempt.site_messages, "form_fields": attempt.form_fields}


def _skip_unneeded_downloads(context) -> None:
    try:
        import screenshot  # the cookie banner and analytics, as for the listings screenshot

        blocked = screenshot.BLOCKED
    except ImportError:
        blocked = None

    def route(request_route):
        request = request_route.request
        if request.resource_type in SKIPPED_RESOURCE_TYPES or (blocked and blocked.search(request.url)):
            request_route.abort()
        else:
            request_route.continue_()

    context.route("**/*", route)


def run_form(page, url: str, values: dict | None, submit: bool, recorder=None, apartment: str = "") -> Attempt:
    """Open the unit page, press APPLY NOW, and:
      values None      -> record the form, fill nothing
      submit False     -> fill it, check every field took its value, screenshot it (--check-form)
      submit True      -> the same, then -- only if nothing required is empty or
                          wrong -- press SUBMIT and wait for the site's answer."""
    recorder = recorder or apply_recorder.RunRecorder(None, apply_recorder.Redactor(None))
    recorder.step("opening the unit page", url=url)
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    apply_button = _wait_for_button(page, APPLY_BUTTON, FORM_TIMEOUT_MS)
    recorder.snapshot("1_unit_page", page)
    if apply_button is None:
        recorder.step("no APPLY NOW button", page_url=page.url, buttons=_buttons(page))
        return Attempt("failed", "couldn't find the APPLY NOW button on the unit page",
                       result_screenshot=_screenshot(page))
    recorder.step("APPLY NOW button visible", text=_text_of(apply_button), page_url=page.url)
    if values is None:
        recorder.image("1_unit_page", _screenshot(page))  # recording only: no hurry
    apply_button.scroll_into_view_if_needed()
    apply_button.click()
    recorder.step("pressed APPLY NOW")

    found = _find_form(page.context, FORM_TIMEOUT_MS)  # same tab, a new tab, a pop-up, or an iframe
    if found is None:
        last = page.context.pages[-1]
        recorder.snapshot("2_after_apply_now", last)
        recorder.step("the form never appeared", page_url=last.url, buttons=_buttons(last))
        return Attempt("failed", "the application form didn't show up after pressing APPLY NOW",
                       result_screenshot=_screenshot(last))
    page, scope = found
    form_url = page.url
    recorder.step("form found", page_url=form_url, frames=[f.url for f in page.frames],
                  inside_form_element=not hasattr(scope, "goto"))
    recorder.note("apartment_named_on_form", bool(apartment) and apartment in _page_text(page))
    fields = _inventory(scope)
    recorder.note("form_fields", fields)
    recorder.note("form_buttons", _buttons(scope))
    recorder.snapshot("2_form_empty", page)
    if values is None:
        recorder.image("2_form_empty", _screenshot(page))
        return Attempt("recorded", "form recorded; nothing filled or sent",
                       form_fields=[f["label"] for f in fields], form_url=form_url)

    filled, problems, labels = [], [], []
    radios = dict(values.get("radio_choices") or {})
    submit_button = None
    for form_page in range(1, MAX_FORM_PAGES + 1):
        page_filled, page_problems = _fill_visible_fields(scope, values, radios)
        filled += page_filled
        fields = _inventory(scope)
        labels += [f["label"] for f in fields]
        empty_required = [f["label"] for f in fields if f["required"] and f["empty"]]
        problems = list(dict.fromkeys(page_problems + empty_required))
        submit_button = _visible_button(scope, SUBMIT_BUTTON)
        next_button = None if submit_button else _visible_button(scope, NEXT_BUTTON)
        recorder.step(f"filled page {form_page} of the form", filled=page_filled, problems=problems,
                      fields_after=[{"label": f["label"], "required": f["required"], "empty": f["empty"]}
                                    for f in fields],
                      submit_button=_text_of(submit_button), next_button=_text_of(next_button))
        if submit_button or next_button is None or problems:
            break
        next_button.click()
        page.wait_for_timeout(800)  # let the next page of the form render

    # Every field StuyTown requires must have been found and filled: if one
    # wasn't, its label probably changed, and the form isn't safe to send.
    not_found = [key for key in REQUIRED_PROFILE_KEYS if values.get(key) not in (None, "") and key not in filled]
    if submit_button is not None and not_found:
        problems += [f"no field found for {key}" for key in not_found]
    found_so_far = {"filled": filled, "problems": problems, "form_fields": list(dict.fromkeys(labels)),
                    "form_url": form_url}
    recorder.snapshot("3_form_filled", page)
    filled_shot = _screenshot(page)
    recorder.step("screenshot of the filled form taken")
    captcha = _has_captcha(page)
    recorder.note("captcha", captcha)
    if submit_button is None:
        why = "required fields are empty" if problems else "no SUBMIT or Next button found"
        return Attempt("incomplete", f"stopped on page {form_page} of the form: {why}",
                       filled_screenshot=filled_shot, **found_so_far)
    if not submit:
        state = "everything required is filled" if not problems else "some required fields aren't right"
        return Attempt("filled", f"{state}; SUBMIT was NOT pressed (form check only)",
                       filled_screenshot=filled_shot, **found_so_far)
    if problems:
        return Attempt("incomplete", "required fields are empty or didn't take the value, so it wasn't submitted",
                       filled_screenshot=filled_shot, **found_so_far)
    if captcha:
        return Attempt("blocked", "the form has a CAPTCHA, which this doesn't solve",
                       filled_screenshot=filled_shot, **found_so_far)

    confirmation_already_showing = _confirmation_showing(page)
    messages_before = set(_site_messages(page))
    recorder.step("pressing SUBMIT", button=_text_of(submit_button))
    if not _press(submit_button, page, recorder):
        return Attempt("failed", "the SUBMIT click didn't reach the button, so nothing was sent",
                       filled_screenshot=filled_shot, result_screenshot=_screenshot(page), **found_so_far)
    recorder.step("pressed SUBMIT")

    def finish(status, detail, **extra):
        recorder.snapshot("4_after_submit", page)
        recorder.step(f"outcome: {status}", page_url=page.url, site_messages=extra.get("site_messages", []))
        return Attempt(status, detail, filled_screenshot=filled_shot, result_screenshot=_screenshot(page),
                       **extra, **found_so_far)

    deadline = time.monotonic() + CONFIRMATION_TIMEOUT_MS / 1000
    complaints = 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if _confirmation_showing(page) and (not confirmation_already_showing or not _still_visible(submit_button)):
            return finish("submitted", "the site confirmed the application")
        # The form is still there and showing new error text: the site
        # refused it (nothing was sent). Two checks in a row, to let it settle.
        new_messages = [m for m in _site_messages(page) if m not in messages_before and SITE_ERROR.search(m)]
        complaints = complaints + 1 if new_messages and _still_visible(submit_button) else 0
        if complaints >= 2:
            return finish("rejected", "the site refused the form", site_messages=new_messages)
    return finish("unconfirmed", f"pressed SUBMIT but no confirmation appeared within "
                  f"{CONFIRMATION_TIMEOUT_MS // 1000}s", site_messages=_site_messages(page))


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


def _wait_for_button(page, name: re.Pattern, timeout_ms: int):
    """Wait for the page's own button or link. One in the site's header, menu
    or footer (a "How to apply" nav link, say) is only taken if the page
    never draws one of its own -- the real one appears after the page's
    JavaScript runs."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        out_of_time = time.monotonic() >= deadline
        for role in ("button", "link"):
            button = _visible_button(page, name, role, allow_site_chrome=out_of_time)
            if button is not None:
                return button
        if out_of_time:
            return None
        page.wait_for_timeout(250)


def _visible_button(scope, name: re.Pattern, role: str = "button", allow_site_chrome: bool = True):
    """The first visible match, preferring one outside the site's header,
    menu and footer. Below-the-fold buttons count as visible."""
    buttons = scope.get_by_role(role, name=name)
    visible = [buttons.nth(i) for i in range(min(buttons.count(), 10)) if buttons.nth(i).is_visible()]
    for button in visible:
        if button.evaluate("el => !el.closest('header, nav, footer')"):
            return button
    return visible[0] if visible and allow_site_chrome else None


def _find_form(context, timeout_ms: int):
    """(page, scope) for the application form: the <form> around its name
    fields, or the whole frame if there isn't one. Looks in every tab and
    frame, newest tab first."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for page in reversed(context.pages):
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
        time.sleep(0.25)


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
    keys filled, labels of fields that wouldn't take their value)."""
    fields = _inventory(scope)
    used, filled, problems = set(), [], []

    def fill(key, pattern, value):
        for f in fields:
            if f["index"] in used or not pattern.search(f["label"].lstrip("* ")):
                continue
            used.add(f["index"])
            element = scope.locator(f'[data-autoapply-field="{f["index"]}"]')
            if _put(element, f, key, value):
                filled.append(key)
            else:
                problems.append(f["label"])
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
    return filled, problems


def _put(element, f: dict, key: str, value) -> bool:
    """Put one value in one field, then read it back. Masked boxes (the
    phone's "+", the "$ 0.00" income box) can reformat or reject what's
    typed, so if the first way doesn't stick, it's typed key by key, and
    alternative spellings are tried. True only once the box shows the value."""
    try:
        if f["type"] == "checkbox":
            tick = value if isinstance(value, bool) else str(value).strip().lower() in ("true", "yes", "y", "1")
            element.set_checked(tick, force=True, timeout=5000)
            return element.is_checked() == tick
        if isinstance(value, bool):
            return False  # true/false only makes sense for a checkbox
        if f["tag"] == "select":
            option = _matching_option(element, str(value))
            if option is not None:
                element.select_option(value=option, timeout=5000)
            return option is not None
        for text in _spellings(key, value):
            element.fill(text, timeout=5000)
            if _shows(key, element.input_value(), value):
                return True
            element.click(timeout=5000)
            element.press("ControlOrMeta+A")
            element.press("Backspace")
            element.press_sequentially(text, delay=20)
            if _shows(key, element.input_value(), value):
                return True
        return False
    except Exception:
        return False


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

_RESULT_TITLES = {
    "submitted": ("Applied: {unit}", 1),
    "unconfirmed": ("Submitted {unit}? No confirmation seen - check now", 1),
    "rejected": ("Auto-apply couldn't finish {unit} - apply yourself now", 1),
    "incomplete": ("Auto-apply couldn't finish {unit} - apply yourself now", 1),
    "blocked": ("Auto-apply couldn't finish {unit} - apply yourself now", 1),
    "failed": ("Auto-apply couldn't finish {unit} - apply yourself now", 1),
    "filled": ("[FORM CHECK] Filled the form for {unit} - not submitted", 0),
}


def result_alerts(unit: dict, attempt: Attempt, link: str | None = None) -> list:
    """The alert about the outcome, with the filled-in form attached -- and,
    when there is one, a quiet second alert with what the page showed at the
    end."""
    title, priority = _RESULT_TITLES[attempt.status]
    lines = [check_units._linked_line(unit), html.escape(attempt.detail)]
    if attempt.problems:
        lines.append("Empty or not taking the value: " + html.escape("; ".join(attempt.problems)))
    if attempt.site_messages:
        lines.append("The site says: " + html.escape("; ".join(attempt.site_messages)))
    if attempt.filled:
        lines.append(f"Filled {len(attempt.filled)} fields from your profile in {attempt.seconds:.1f}s. "
                     "Attached: the form as filled in.")
    if attempt.status in ("submitted", "unconfirmed"):
        lines.append(NEXT_STEP_REMINDER)
    first_image = attempt.filled_screenshot or attempt.result_screenshot
    alerts = [{
        "title": title.format(unit=check_units.unit_label(unit)),
        "message": check_units._fit(lines),
        "priority": priority,
        "url": link or check_units.unit_url(unit),
        "url_title": "Open this unit",
        **({"attachment": ("filled-form.jpg", first_image, "image/jpeg")} if first_image else {}),
    }]
    if attempt.filled_screenshot and attempt.result_screenshot:
        alerts.append({
            "title": f"After SUBMIT: {check_units.unit_label(unit)}",
            "message": f"What the page showed at the end ({html.escape(attempt.status)}).",
            "priority": -1,  # quiet: the alert above is the one that matters
            "attachment": ("after-submit.jpg", attempt.result_screenshot, "image/jpeg"),
        })
    return alerts


def _report(unit: dict, attempt: Attempt, link: str | None = None) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    for name, shot in (("filled-form", attempt.filled_screenshot), ("result", attempt.result_screenshot)):
        if shot:
            path = Path(PRIVATE_DIR) / f"{stamp}_{check_units.apartment(unit)}_{name}.jpg"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(shot)
            except OSError as e:
                print(f"   Couldn't save a screenshot: {e}")
    if attempt.recording:
        print(f"   Recording of the form (details redacted): {attempt.recording}/")
    for alert in result_alerts(unit, attempt, link):
        check_units._notify_best_effort(alert)


# --------------------------------------------------------------------- CLI


def _serve_selftest_site() -> http.server.ThreadingHTTPServer:
    handler = functools.partial(SelftestSiteHandler, directory=str(SELFTEST_SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class SelftestSiteHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the fake site, and answers its form's POST like an API would."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b'{"ok": true, "applicationId": "TEST-0001"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


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

    from playwright.sync_api import sync_playwright

    server = None
    if args.selftest:
        # The real auto-apply path, pointed at the fake site.
        server = _serve_selftest_site()
        check_units.TITLE_PREFIX = "[TEST] "
        check_units.UNIT_PAGE_URL = f"http://127.0.0.1:{server.server_port}/unit.html"
        unit = {"unitSpk": "SELFTEST", "name": "TEST", "building": {"address": "Fake form, not a real unit"},
                "price": 2850}
        link = check_units.LISTINGS_URL
    else:
        link = args.check_form
        spk = re.search(r"unitSpk=([^&]+)", link)
        unit = {"unitSpk": spk[1] if spk else "manual", "name": spk[1].rsplit("~", 1)[-1] if spk else "manual",
                "building": {"address": "form check from the command line"}}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            try:
                if args.selftest:
                    attempt = apply_to_unit(browser, unit, profile, runs_dir=SELFTEST_RUNS_DIR)
                else:
                    attempt = _check_form(browser, link, unit, profile)
            finally:
                browser.close()
    finally:
        if server:
            server.shutdown()

    print(f"Result after {attempt.seconds:.1f}s: {attempt.status} -- {attempt.detail}")
    print(f"Form fields seen: {'; '.join(attempt.form_fields) or 'none'}")
    print(f"Filled from your profile: {', '.join(attempt.filled) or 'nothing'}")
    if attempt.problems:
        print(f"Required fields empty or not taking the value: {'; '.join(attempt.problems)}")
    _report(unit, attempt, link=link)
    return 0 if attempt.status in ("submitted", "filled") and not attempt.problems else 1


def _check_form(browser, url: str, unit: dict, profile: dict) -> Attempt:
    recorder = apply_recorder.RunRecorder(apply_recorder.run_folder(check_units.apartment(unit), "check",
                                                                    SELFTEST_RUNS_DIR),
                                          apply_recorder.Redactor(profile))
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.set_default_timeout(15000)
    started = time.monotonic()
    recorder.watch(context)
    attempt = run_form(context.new_page(), url, form_values(profile), submit=False, recorder=recorder,
                       apartment=check_units.apartment(unit))
    attempt.seconds = time.monotonic() - started
    attempt.recording = recorder.finish(_outcome(attempt))
    context.close()
    return attempt


if __name__ == "__main__":
    sys.exit(main())
