"""
Auto-apply: when a listed unit's monthly rent is at or under your limit, open
the unit's page, press APPLY NOW, fill in StuyTown's application form from
your saved applicant profile and submit it -- then send the result, with a
screenshot, to your phone.

The watcher (watch_loop.py) calls act_on_listings() on every check, right
after the new-unit alert has gone out. Two repository variables control it:

    AUTO_APPLY_MODE      off (default) | dry_run (fill, never press Submit) | submit
    AUTO_APPLY_MAX_RENT  your rent limit in $/month, e.g. 3000 -- required
                         once the mode isn't off; there is no built-in default

Your details come from APPLICANT_PROFILE -- the whole JSON document, stored as
ONE GitHub secret -- or, on your own computer, the gitignored
applicant_profile.json. applicant_profile.example.json shows every key.

The form (affordable-housing.stuytown.com, as recorded 2026-10-08) is one page:

    First Name *   Last Name *   Email *   Cell Phone *   Work Phone
    Building *  Street name *  Apartment No.  City *  State  Zip *
    Household Size *   Household Gross Annual Income, $ *        [SUBMIT]

and submitting it gets you an email with a link to the detailed application,
which has to be completed within 24 hours.

The repo (and its Actions logs) are public, so nothing personal is ever
printed, committed or logged: in GitHub Actions every profile value is masked
in the log, form screenshots go only to your phone (as a Pushover image) and
the gitignored private/ folder, and data/applications.json records only which
units were tried and how it went.

    python auto_apply.py --selftest              # fill + submit a FAKE copy of the form (tests/fixtures/apply_site)
    python auto_apply.py --unit-url URL          # dry run against a real unit page -- never submits
    python auto_apply.py --unit-url URL --headed # same, with the browser window visible
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

import check_units

MODES = ("off", "dry_run", "submit")
_MODE_SETTING = os.environ.get("AUTO_APPLY_MODE", "").strip().lower()
MODE = _MODE_SETTING if _MODE_SETTING in MODES else "off"
_MAX_RENT_SETTING = os.environ.get("AUTO_APPLY_MAX_RENT", "").strip()
MAX_RENT = check_units.number(_MAX_RENT_SETTING)  # $/month, inclusive; None = not set

PROFILE_ENV = "APPLICANT_PROFILE"
PROFILE_FILE = "applicant_profile.json"
APPLICATIONS_FILE = "data/applications.json"
PRIVATE_DIR = "private/applications"  # gitignored: form screenshots contain your details

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
# application per apartment); an attempt that crashed (timeout, site hiccup)
# gets one more try on a later check.
MAX_APPLICATIONS_PER_RUN = 3
MAX_FAILED_ATTEMPTS_PER_UNIT = 2
MAX_FORM_PAGES = 6  # Next/Continue presses, in case the form ever grows pages
FORM_TIMEOUT_MS = 20000  # waiting for APPLY NOW, and for the form after it
CONFIRMATION_TIMEOUT_MS = 30000
PUSHOVER_IMAGE_LIMIT = 5_000_000  # bytes

SELFTEST_SITE = Path(__file__).resolve().parent / "tests" / "fixtures" / "apply_site"
EXAMPLE_PROFILE = Path(__file__).resolve().parent / "applicant_profile.example.json"
NEXT_STEP_REMINDER = ("Watch your email: StuyTown sends a link to the detailed application, "
                      "which has to be completed within 24 hours.")


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
    if _MODE_SETTING and _MODE_SETTING not in MODES:
        print(f"WARNING: AUTO_APPLY_MODE={_MODE_SETTING!r} isn't one of {', '.join(MODES)} -- treating it as off.")
    if MODE == "off":
        print("Auto-apply: off (set AUTO_APPLY_MODE to dry_run or submit to turn it on)")
        return
    if _profile_for_run():
        print(f"Auto-apply: {MODE} for units at or under ${MAX_RENT:,.0f}/mo (applicant profile loaded)")


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
    """Why this unit shouldn't be tried (again) in the current mode, or None."""
    attempts = (record or {}).get("attempts", [])
    if any(a["status"] in ("submitted", "unconfirmed") for a in attempts):
        return "already applied"  # an unconfirmed submit may have gone through: never send a second
    mine = [a for a in attempts if a["mode"] == MODE]
    done = [a for a in mine if a["status"] != "failed"]
    if done:
        return f"already tried in {MODE} mode ({done[-1]['status']})"
    if len(mine) >= MAX_FAILED_ATTEMPTS_PER_UNIT:
        return f"gave up after {len(mine)} failed tries"
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
_submitted_this_run = 0


def act_on_listings(units: list) -> None:
    """The watcher's hook, called on every check with everything listed.
    Applies (or dry-runs) each unit that qualifies and hasn't been handled
    yet, cheapest first."""
    global _submitted_this_run
    if MODE == "off" or not units:
        return
    profile = _profile_for_run()
    if profile is None:
        return
    applications = load_applications()
    todo = []
    for unit in units:
        uid = check_units.unit_id(unit)
        reason = skip_reason(unit, profile) or already_handled(applications.get(uid))
        if reason:
            if uid not in _skips_logged:
                _skips_logged.add(uid)
                print(f"   Auto-apply: skipping {check_units.unit_label(unit)} -- {reason}")
            continue
        todo.append(unit)
    if not todo:
        return

    from playwright.sync_api import sync_playwright  # only once there's something to apply to

    todo.sort(key=check_units.unit_rent)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for unit in todo:
                if MODE == "submit" and _submitted_this_run >= MAX_APPLICATIONS_PER_RUN:
                    print(f"   Auto-apply: already submitted {MAX_APPLICATIONS_PER_RUN} applications this run "
                          f"-- not applying to {check_units.unit_label(unit)}")
                    break
                print(f"   Auto-apply ({MODE}): {check_units.unit_label(unit)} -- {check_units.unit_url(unit)}")
                started = time.monotonic()
                attempt = apply_to_unit(browser, unit, profile, submit=(MODE == "submit"))
                print(f"   Auto-apply result after {time.monotonic() - started:.1f}s: "
                      f"{attempt.status} -- {attempt.detail}")
                if attempt.status in ("submitted", "unconfirmed"):
                    _submitted_this_run += 1
                _record(applications, unit, attempt)
                save_applications(applications)
                _report(unit, attempt)
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
        "mode": MODE,
        "status": attempt.status,
        "detail": attempt.detail,
        "form_url": attempt.form_url,  # where APPLY NOW led (no personal details in it)
        "fields_filled": attempt.filled,  # profile key names only, e.g. "first_name"
        "problems": attempt.problems,  # the site's own labels for fields it couldn't fill
        "form_fields_seen": attempt.form_fields,  # the site's labels, to spot form changes
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
# Images, video and the cookie banner aren't needed to apply, and skipping them
# makes the unit page usable sooner.
SKIPPED_RESOURCE_TYPES = ("image", "media")

# How the page's fields are read: every visible input with the text a person
# would read as its label. StuyTown's labels might not be wired to their
# boxes in the HTML, so besides <label for=...> and aria-label this also
# takes the text of the smallest wrapper that holds just that one field.
# Fields are tagged data-autoapply-field=N so Python can address them.
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


@dataclass
class Attempt:
    """How one application went."""

    status: str  # submitted | unconfirmed | rejected | filled (dry run) | incomplete | blocked | failed
    detail: str
    filled: list = field(default_factory=list)  # profile keys that went into the form (and were checked)
    problems: list = field(default_factory=list)  # the site's labels for required fields left empty or wrong
    form_fields: list = field(default_factory=list)  # every field label on the form, for spotting changes
    form_url: str = ""
    screenshot: bytes | None = None  # JPEG -- contains your details, so phone + private/ only
    site_messages: list = field(default_factory=list)  # error text the site showed (phone only)


def form_values(profile: dict) -> dict:
    """The profile as it goes into the form: everything but the "_" notes."""
    return {key: value for key, value in profile.items() if not str(key).startswith("_")}


def apply_to_unit(browser, unit: dict, profile: dict, submit: bool) -> Attempt:
    """One application in a fresh browser session. Never raises."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.set_default_timeout(15000)
    try:
        page = context.new_page()
        _skip_unneeded_downloads(page)
        return fill_application(page, check_units.unit_url(unit), form_values(profile), submit=submit)
    except Exception as e:
        return Attempt("failed", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}",
                       screenshot=_screenshot_of_last_page(context))
    finally:
        context.close()


def _skip_unneeded_downloads(page) -> None:
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

    page.context.route("**/*", route)


def fill_application(page, url: str, values: dict, submit: bool) -> Attempt:
    """Open the unit page, press APPLY NOW, fill the form, check every field
    took its value, and -- only if submit is True and nothing required is
    empty or wrong -- press SUBMIT and wait for the site to confirm."""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    apply_button = _wait_for_button(page, APPLY_BUTTON, FORM_TIMEOUT_MS)
    if apply_button is None:
        return Attempt("failed", "couldn't find the APPLY NOW button on the unit page", screenshot=_screenshot(page))
    apply_button.scroll_into_view_if_needed()
    apply_button.click()

    found = _find_form(page.context, FORM_TIMEOUT_MS)  # same tab, a new tab, a pop-up, or an iframe
    if found is None:
        return Attempt("failed", "the application form didn't show up after pressing APPLY NOW",
                       screenshot=_screenshot_of_last_page(page.context))
    page, scope = found
    form_url = page.url

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
    # Screenshots are only taken when nothing will be clicked afterwards: a
    # full-page screenshot leaves the page scrolled so that the next click
    # lands beside the button instead of on it.
    captcha = _has_captcha(page)
    if submit_button is None:
        why = "required fields are empty" if problems else "no SUBMIT or Next button found"
        return Attempt("incomplete", f"stopped on page {form_page} of the form: {why}",
                       screenshot=_screenshot(page), **found_so_far)
    if not submit:
        note = " The form has a CAPTCHA, so a real submit would stop there." if captcha else ""
        state = "everything required is filled" if not problems else "some required fields aren't right"
        return Attempt("filled", f"dry run: {state}; SUBMIT was NOT pressed.{note}",
                       screenshot=_screenshot(page), **found_so_far)
    if problems:
        return Attempt("incomplete", "required fields are empty or didn't take the value, so it wasn't submitted",
                       screenshot=_screenshot(page), **found_so_far)
    if captcha:
        return Attempt("blocked", "the form has a CAPTCHA, which this doesn't solve",
                       screenshot=_screenshot(page), **found_so_far)

    confirmation_already_showing = _confirmation_showing(page)
    messages_before = set(_site_messages(page))
    submit_button.click()
    deadline = time.monotonic() + CONFIRMATION_TIMEOUT_MS / 1000
    complaints = 0
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if _confirmation_showing(page) and (not confirmation_already_showing or not _still_visible(submit_button)):
            return Attempt("submitted", "the site confirmed the application", screenshot=_screenshot(page),
                           **found_so_far)
        # The form is still there and showing new error text: the site
        # refused it (nothing was sent). Two checks in a row, to let it settle.
        new_messages = [m for m in _site_messages(page) if m not in messages_before and SITE_ERROR.search(m)]
        complaints = complaints + 1 if new_messages and _still_visible(submit_button) else 0
        if complaints >= 2:
            return Attempt("rejected", "the site refused the form", screenshot=_screenshot(page),
                           site_messages=new_messages, **found_so_far)
    return Attempt("unconfirmed", "pressed SUBMIT but no confirmation appeared within "
                   f"{CONFIRMATION_TIMEOUT_MS // 1000}s", screenshot=_screenshot(page),
                   site_messages=_site_messages(page), **found_so_far)


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
    try:
        shot = page.screenshot(full_page=True, type="jpeg", quality=60)
        if len(shot) > PUSHOVER_IMAGE_LIMIT:
            shot = page.screenshot(type="jpeg", quality=60)  # just the visible part
        return shot
    except Exception:
        return None


def _screenshot_of_last_page(context) -> bytes | None:
    try:
        return _screenshot(context.pages[-1]) if context.pages else None
    except Exception:
        return None


# --------------------------------------------------------------- reporting

_RESULT_TITLES = {
    "submitted": ("Applied: {unit}", 1),
    "unconfirmed": ("Submitted {unit}? No confirmation seen - check now", 1),
    "filled": ("[DRY RUN] Filled the application for {unit}", 0),
    "incomplete": ("Auto-apply couldn't finish {unit} - apply yourself now", 1),
    "rejected": ("Auto-apply couldn't finish {unit} - apply yourself now", 1),
    "blocked": ("Auto-apply couldn't finish {unit} - apply yourself now", 1),
    "failed": ("Auto-apply couldn't finish {unit} - apply yourself now", 1),
}


def result_alert(unit: dict, attempt: Attempt, link: str | None = None) -> dict:
    title, priority = _RESULT_TITLES[attempt.status]
    lines = [check_units._linked_line(unit), html.escape(attempt.detail)]
    if attempt.problems:
        lines.append("Empty or not taking the value: " + html.escape("; ".join(attempt.problems)))
    if attempt.site_messages:
        lines.append("The site says: " + html.escape("; ".join(attempt.site_messages)))
    if attempt.filled:
        lines.append(f"Filled {len(attempt.filled)} fields from your profile.")
    if attempt.status in ("submitted", "unconfirmed"):
        lines.append(NEXT_STEP_REMINDER)
    alert = {
        "title": title.format(unit=check_units.unit_label(unit)),
        "message": check_units._fit(lines),
        "priority": priority,
        "url": link or check_units.unit_url(unit),
        "url_title": "Open this unit",
    }
    if attempt.screenshot:
        alert["attachment"] = ("application.jpg", attempt.screenshot, "image/jpeg")
    return alert


def _report(unit: dict, attempt: Attempt, link: str | None = None) -> None:
    if attempt.screenshot:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        path = Path(PRIVATE_DIR) / f"{stamp}_{check_units.apartment(unit)}_{attempt.status}.jpg"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(attempt.screenshot)
            print(f"   Form screenshot (private, not committed): {path}")
        except OSError as e:
            print(f"   Couldn't save the form screenshot: {e}")
    check_units._notify_best_effort(result_alert(unit, attempt, link))


# --------------------------------------------------------------------- CLI


def _serve_selftest_site() -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_QuietHandler, directory=str(SELFTEST_SITE))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--selftest", action="store_true",
                        help="fill and submit the FAKE copy of the form in tests/fixtures/apply_site with your profile")
    target.add_argument("--unit-url", help="dry run against this real unit page (never submits)")
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
        server = _serve_selftest_site()
        check_units.TITLE_PREFIX = "[TEST] "
        url = f"http://127.0.0.1:{server.server_port}/unit.html?unitSpk=SELFTEST"
        unit = {"unitSpk": "SELFTEST", "name": "TEST", "building": {"address": "Fake form, not a real unit"},
                "price": 2850}
    else:
        url = args.unit_url
        unit = {"unitSpk": "manual", "name": "manual-test", "building": {"address": "dry run from the command line"}}
    started = time.monotonic()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                context.set_default_timeout(15000)
                page = context.new_page()
                attempt = fill_application(page, url, form_values(profile), submit=args.selftest)
                context.close()
            finally:
                browser.close()
    finally:
        if server:
            server.shutdown()

    print(f"Result after {time.monotonic() - started:.1f}s: {attempt.status} -- {attempt.detail}")
    print(f"Form fields seen: {'; '.join(attempt.form_fields) or 'none'}")
    print(f"Filled from your profile: {', '.join(attempt.filled) or 'nothing'}")
    if attempt.problems:
        print(f"Required fields empty or not taking the value: {'; '.join(attempt.problems)}")
    _report(unit, attempt, link=url if args.unit_url else check_units.LISTINGS_URL)
    return 0 if attempt.status in ("submitted", "filled") and not attempt.problems else 1


if __name__ == "__main__":
    sys.exit(main())
