"""
Auto-apply: when a listed unit's monthly rent is at or under your limit
(default $3,000), open the unit's page, press Apply Now, fill in the
application from your saved applicant profile and submit it -- then send the
result, with a screenshot of the filled-in form, to your phone.

The watcher (watch_loop.py) calls act_on_listings() on every check, right
after the new-unit alert has gone out. It's OFF unless AUTO_APPLY_MODE says
otherwise:

    off      (default) never opens an application
    dry_run  fills in the whole form and screenshots it, but never presses the
             final Submit -- use this first, to see exactly what it would send
    submit   fills in the form and submits it

Your details come from APPLICANT_PROFILE -- the whole JSON document, stored as
ONE GitHub secret -- or, on your own computer, the gitignored
applicant_profile.json. applicant_profile.example.json shows the shape.

The repo (and its Actions logs) are public, so nothing personal is ever
printed, committed or logged: in GitHub Actions every profile value is masked
in the log, form screenshots go only to your phone (as a Pushover image) and
the gitignored private/ folder, and data/applications.json records only which
units were tried and how it went.

    python auto_apply.py --selftest              # fill + submit a FAKE form (tests/fixtures/apply_site) with your profile
    python auto_apply.py --unit-url URL          # dry run against a real unit page -- never submits
    python auto_apply.py --unit-url URL --headed # same, with the browser window visible
"""

import argparse
import functools
import http.server
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import check_units

MODES = ("off", "dry_run", "submit")
_MODE_SETTING = os.environ.get("AUTO_APPLY_MODE", "").strip().lower()
MODE = _MODE_SETTING if _MODE_SETTING in MODES else "off"
DEFAULT_MAX_RENT = 3000
_MAX_RENT_SETTING = os.environ.get("AUTO_APPLY_MAX_RENT", "").strip()
MAX_RENT = check_units.number(_MAX_RENT_SETTING) or DEFAULT_MAX_RENT  # $/month, inclusive

PROFILE_ENV = "APPLICANT_PROFILE"
PROFILE_FILE = "applicant_profile.json"
REQUIRED_PROFILE_KEYS = ("first_name", "last_name", "email", "phone")
APPLICATIONS_FILE = "data/applications.json"
PRIVATE_DIR = "private/applications"  # gitignored: form screenshots contain your details

# Safety limits. A unit is applied to at most once, ever; one whose attempt
# crashed (timeout, site hiccup) gets one more try on a later check.
MAX_APPLICATIONS_PER_RUN = 3
MAX_FAILED_ATTEMPTS_PER_UNIT = 2
MAX_FORM_PAGES = 6  # Next/Continue presses before giving up on a multi-page form
FORM_TIMEOUT_MS = 20000  # waiting for the Apply button, and for the form after it
CONFIRMATION_TIMEOUT_MS = 30000
PUSHOVER_IMAGE_LIMIT = 5_000_000  # bytes

SELFTEST_SITE = Path(__file__).resolve().parent / "tests" / "fixtures" / "apply_site"
EXAMPLE_PROFILE = Path(__file__).resolve().parent / "applicant_profile.example.json"


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
    missing = [key for key in REQUIRED_PROFILE_KEYS if not str(profile.get(key) or "").strip()]
    if missing:
        raise ProfileError(f"{source} is missing {', '.join(missing)}")
    return profile


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


@functools.cache
def _profile_for_run() -> dict | None:
    """The profile, loaded once per run. A problem is reported (log + one
    alert) and switches auto-apply off for the run; it never stops the
    watcher -- the alerts matter more."""
    try:
        profile = load_profile()
        if profile is None:
            raise ProfileError(f"no applicant profile found (set the {PROFILE_ENV} secret, "
                               f"or create {PROFILE_FILE} on your own computer)")
        return profile
    except ProfileError as e:
        print(f"Auto-apply is OFF for this run: {e}")
        check_units._notify_best_effort({
            "title": "StuyTown auto-apply is off",
            "message": f"{e}. New-unit alerts still work.",
            "priority": 0,
        })
        return None


def startup_check() -> None:
    """Print what auto-apply will do this run (and mask the profile in the
    log before anything else prints). Called once when the watcher starts."""
    if _MODE_SETTING and _MODE_SETTING not in MODES:
        print(f"WARNING: AUTO_APPLY_MODE={_MODE_SETTING!r} isn't one of {', '.join(MODES)} -- treating it as off.")
    if _MAX_RENT_SETTING and check_units.number(_MAX_RENT_SETTING) is None:
        print(f"WARNING: AUTO_APPLY_MAX_RENT={_MAX_RENT_SETTING!r} isn't a number -- using ${DEFAULT_MAX_RENT:,}.")
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
    if rent > MAX_RENT:
        return f"rent ${rent:,.0f} is over ${MAX_RENT:,.0f}"
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
                attempt = apply_to_unit(browser, unit, profile, submit=(MODE == "submit"))
                print(f"   Auto-apply result: {attempt.status} -- {attempt.detail}")
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
        "fields_filled": attempt.filled,  # profile key names only, e.g. "first_name"
        "required_left_empty": attempt.missing,  # the site's own field labels
    })


# ------------------------------------------------------------- the browser

APPLY_BUTTON = re.compile(r"^\s*apply\b", re.I)
SUBMIT_BUTTON = re.compile(r"^\s*(submit|send|finish|complete)\b|submit\s+(my\s+)?application|^\s*apply\s*$", re.I)
NEXT_BUTTON = re.compile(r"^\s*(next|continue|proceed|save\s*(and|&)\s*continue)\b", re.I)
# Fields that mark the application form (as opposed to, say, a newsletter box).
FORM_ANCHOR = re.compile(r"first\s*name|last\s*name|full\s*name|legal\s*name", re.I)
_DIALOG = "[role=dialog], dialog, [aria-modal=true]"
CONFIRMATION = re.compile(
    r"thank\s*you|application\s+(has\s+been\s+|was\s+)?(received|submitted|complete)|"
    r"successfully\s+submitted|confirmation\s+(number|#|code)", re.I)

# Profile key -> label patterns for the form field it goes in (matched against
# the field's label or placeholder, ignoring case). Checked in this order, and
# each form field is filled at most once, so specific patterns come first.
# Fields this doesn't know go in the profile's "extra_fields" instead.
FORM_FIELDS = [
    ("first_name", [r"first\s*name", r"given\s*name"]),
    ("middle_name", [r"middle\s*(name|initial)"]),
    ("last_name", [r"last\s*name", r"surname", r"family\s*name"]),
    ("full_name", [r"^\W*(full\s*|legal\s*|your\s*)?name\W*$"]),
    ("email", [r"e-?mail"]),
    ("email", [r"(confirm|re-?enter|verify)\s*(your\s*)?e-?mail"]),
    ("phone", [r"phone", r"mobile", r"\bcell\b"]),
    ("date_of_birth", [r"date\s*of\s*birth", r"birth\s*date", r"\bdob\b", r"birthday"]),
    ("street_address", [r"street", r"address\s*(line\s*)?1", r"^\W*(current\s*|home\s*|mailing\s*)?address\W*$"]),
    ("apartment", [r"\bapt\b", r"apartment\s*(number|no|#)", r"suite", r"address\s*(line\s*)?2"]),
    ("city", [r"^\W*city", r"\bcity\b"]),
    ("state", [r"^\W*state\b", r"\bstate\b"]),
    ("zip", [r"\bzip", r"postal"]),
    ("annual_income", [r"(annual|yearly|gross|total)\s*(household\s*)?income", r"household\s*income",
                       r"^\W*income\W*$", r"salary"]),
    ("household_size", [r"household\s*size", r"(number|#)\s*of\s*(people|persons|occupants|household)",
                        r"occupants"]),
    ("employer", [r"employer", r"company\s*name"]),
    ("job_title", [r"job\s*title", r"occupation", r"position"]),
    ("move_in_date", [r"move[\s-]*in", r"(desired|preferred)\s*(lease\s*)?start"]),
]

_FILLABLE_JS = """el => {
    if (el.dataset.autofilled || el.disabled || el.readOnly) return false;
    if (!['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) return false;
    if (['hidden', 'submit', 'button', 'reset', 'radio', 'file', 'image'].includes(el.type)) return false;
    if (el.closest('footer, [role=contentinfo]')) return false;  // newsletter boxes and the like
    // A checkbox is often visually hidden behind a styled box: judge it by its label.
    const shown = el.type === 'checkbox' ? (el.closest('label, fieldset') || el.parentElement) : el;
    const box = shown.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && getComputedStyle(el).visibility !== 'hidden';
}"""

# Labels (never values) of visible required fields that are still empty.
_UNFILLED_REQUIRED_JS = """root => {
    const out = [];
    for (const el of root.querySelectorAll('input, select, textarea')) {
        if (el.disabled || ['hidden', 'submit', 'button'].includes(el.type)) continue;
        if (!(el.required || el.getAttribute('aria-required') === 'true')) continue;
        const shown = ['checkbox', 'radio'].includes(el.type) ? (el.closest('label, fieldset') || el.parentElement) : el;
        const box = shown.getBoundingClientRect();
        if (!box.width || !box.height) continue;
        let empty;
        if (el.type === 'checkbox') empty = !el.checked;
        else if (el.type === 'radio') empty = ![...root.querySelectorAll('input[type=radio]')]
            .some(r => r.name === el.name && r.checked);
        else empty = !el.value;
        if (!empty) continue;
        const group = el.type === 'radio' && el.closest('fieldset')?.querySelector('legend');
        const label = (group && group.innerText) || (el.labels && el.labels[0] && el.labels[0].innerText)
            || el.getAttribute('aria-label') || el.placeholder || el.name || el.type;
        out.push(label.replace(/\\s+/g, ' ').trim().slice(0, 80));
    }
    return [...new Set(out)];
}"""


@dataclass
class Attempt:
    """How one application went."""

    status: str  # submitted | unconfirmed | filled (dry run) | incomplete | blocked | failed
    detail: str
    filled: list = field(default_factory=list)  # profile keys that went into the form
    missing: list = field(default_factory=list)  # labels of required fields left empty
    screenshot: bytes | None = None  # JPEG -- contains your details, so phone + private/ only


def form_values(profile: dict, unit: dict | None = None) -> dict:
    """The profile, plus what can be worked out from it: full_name from your
    first/last name, and -- if you left move_in_date empty -- the unit's own
    available date (or today, if that's already passed)."""
    values = dict(profile)
    if not values.get("full_name"):
        values["full_name"] = " ".join(str(values.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
    if not values.get("move_in_date") and unit and unit.get("availableDate"):
        available = str(unit["availableDate"])[:10]
        values["move_in_date"] = max(available, date.today().isoformat())
    return values


def apply_to_unit(browser, unit: dict, profile: dict, submit: bool) -> Attempt:
    """One application in a fresh browser session. Never raises."""
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.set_default_timeout(15000)
    try:
        page = context.new_page()
        try:
            import screenshot  # same cookie-banner/analytics blocking as the listings screenshot
            page.route(screenshot.BLOCKED, lambda route: route.abort())
        except ImportError:
            pass
        return fill_application(page, check_units.unit_url(unit), form_values(profile, unit), submit=submit)
    except Exception as e:
        return Attempt("failed", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}",
                       screenshot=_screenshot_of_last_page(context))
    finally:
        context.close()


def fill_application(page, url: str, values: dict, submit: bool) -> Attempt:
    """Open the unit page, press Apply, fill every page of the form, and --
    only if submit is True and nothing required is left empty -- submit it."""
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    apply_button = _wait_for_button(page, APPLY_BUTTON, FORM_TIMEOUT_MS)
    if apply_button is None:
        return Attempt("failed", "couldn't find an Apply button on the unit page", screenshot=_screenshot(page))
    apply_button.click()

    found = _find_form(page.context, FORM_TIMEOUT_MS)  # same tab, a new tab, a pop-up, or an iframe
    if found is None:
        return Attempt("failed", "the application form didn't show up after pressing Apply",
                       screenshot=_screenshot_of_last_page(page.context))
    page, scope = found

    filled, radios = [], dict(values.get("radio_choices") or {})
    submit_button = missing = None
    for form_page in range(1, MAX_FORM_PAGES + 1):
        filled += _fill_visible_fields(scope, values, radios)
        missing = _unfilled_required(scope)
        submit_button = _visible_button(scope, SUBMIT_BUTTON)
        next_button = None if submit_button else _visible_button(scope, NEXT_BUTTON)
        if submit_button or next_button is None or missing:
            break
        next_button.click()
        page.wait_for_timeout(800)  # let the next page of the form render

    shot = _screenshot(page)
    captcha = _has_captcha(page)
    if submit_button is None:
        why = "required fields are empty" if missing else "no Submit or Next button found"
        return Attempt("incomplete", f"stopped on page {form_page} of the form: {why}", filled, missing, shot)
    if not submit:
        note = " The form has a CAPTCHA, so a real submit would stop there." if captcha else ""
        return Attempt("filled", "dry run: form filled, Submit NOT pressed." + note, filled, missing, shot)
    if missing:
        return Attempt("incomplete", "required fields are still empty, so it wasn't submitted", filled, missing, shot)
    if captcha:
        return Attempt("blocked", "the form has a CAPTCHA, which this doesn't solve", filled, missing, shot)

    confirmation_already_showing = _confirmation_showing(page)
    submit_button.click()
    deadline = time.monotonic() + CONFIRMATION_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        page.wait_for_timeout(500)
        if _confirmation_showing(page) and (not confirmation_already_showing or not _still_visible(submit_button)):
            return Attempt("submitted", "the site confirmed the application", filled, [], _screenshot(page))
    return Attempt("unconfirmed", "pressed Submit but no confirmation appeared within "
                   f"{CONFIRMATION_TIMEOUT_MS // 1000}s", filled, _unfilled_required(scope), _screenshot(page))


def _wait_for_button(page, name: re.Pattern, timeout_ms: int):
    """Wait for the page's own button or link. One in the site's header, menu
    or footer (an "Apply for housing" nav link, say) is only taken if the
    page never draws one of its own -- the real one often appears late."""
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
    menu and footer."""
    buttons = scope.get_by_role(role, name=name)
    visible = [buttons.nth(i) for i in range(min(buttons.count(), 10)) if buttons.nth(i).is_visible()]
    for button in visible:
        if button.evaluate("el => !el.closest('header, nav, footer')"):
            return button
    return visible[0] if visible and allow_site_chrome else None


def _find_form(context, timeout_ms: int):
    """(page, scope) for the application form: the <form> around its name
    field, or the whole frame if there isn't one. Looks in every tab and
    frame, because Apply might open a new tab or embed a third-party form."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for page in reversed(context.pages):  # newest tab first
            for frame in page.frames:
                try:
                    anchors = frame.get_by_label(FORM_ANCHOR)
                    visible = [anchors.nth(i) for i in range(min(anchors.count(), 5)) if anchors.nth(i).is_visible()]
                    # A pop-up dialog beats, say, a contact form further down the page.
                    visible.sort(key=lambda a: not a.evaluate("el => !!el.closest(" + repr(_DIALOG) + ")"))
                    if not visible:
                        continue
                    boxed = visible[0].evaluate(
                        "el => { const f = el.closest('form') || el.closest(" + repr(_DIALOG) + ");"
                        " if (f) f.setAttribute('data-autoapply-form', '1'); return !!f; }")
                    return page, (frame.locator("[data-autoapply-form]").first if boxed else frame)
                except Exception:
                    continue  # a frame that went away mid-look
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def _fill_visible_fields(scope, values: dict, radios: dict) -> list:
    """Fill what's on screen now. Returns the profile keys used."""
    filled = []
    for key, patterns in FORM_FIELDS:
        value = values.get(key)
        if value in (None, "") or isinstance(value, (dict, list)):
            continue
        if any(_fill_one(scope, re.compile(p, re.I), value) for p in patterns):
            filled.append(key)
    for label, value in (values.get("extra_fields") or {}).items():
        if _fill_one(scope, re.compile(re.escape(str(label)), re.I), value):
            filled.append(f"extra_fields: {label}")
    for question, answer in list(radios.items()):
        if _choose_radio(scope, str(question), str(answer)):
            filled.append(f"radio_choices: {question}")
            del radios[question]  # answered; later pages don't need it
    return filled


def _fill_one(scope, label: re.Pattern, value) -> bool:
    for candidates in (scope.get_by_label(label), scope.get_by_placeholder(label)):
        for i in range(min(candidates.count(), 10)):
            element = candidates.nth(i)
            try:
                if element.evaluate(_FILLABLE_JS) and _put(element, value):
                    element.evaluate("el => { el.dataset.autofilled = '1'; }")
                    return True
            except Exception:
                continue  # this one wouldn't take the value; try the next match
    return False


def _put(element, value) -> bool:
    tag, kind = element.evaluate("el => [el.tagName.toLowerCase(), (el.type || '').toLowerCase()]")
    if kind == "checkbox":
        tick = value if isinstance(value, bool) else str(value).strip().lower() in ("true", "yes", "y", "1")
        element.set_checked(tick, force=True, timeout=5000)
        return True
    if isinstance(value, bool):
        return False  # true/false only makes sense for a checkbox
    if tag == "select":
        option = _matching_option(element, str(value))
        if option is None:
            return False
        element.select_option(value=option, timeout=5000)
        return True
    element.fill(_format_for(kind, value), timeout=5000)
    return True


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


def _format_for(kind: str, value) -> str:
    """Dates as YYYY-MM-DD for date pickers and MM/DD/YYYY for text boxes;
    plain digits for number boxes."""
    text = str(value).strip()
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    us = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if kind == "date" and us:
        return f"{us[3]}-{int(us[1]):02d}-{int(us[2]):02d}"
    if kind != "date" and iso:
        return f"{iso[2]}/{iso[3]}/{iso[1]}"
    if kind == "number":
        return text.replace("$", "").replace(",", "")
    return text


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


def _unfilled_required(scope) -> list:
    try:
        if hasattr(scope, "goto"):  # a whole frame, not a <form>
            return scope.evaluate(f"() => ({_UNFILLED_REQUIRED_JS})(document)")
        return scope.evaluate(_UNFILLED_REQUIRED_JS)
    except Exception:
        return []  # the form navigated away (e.g. after Submit)


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
    "blocked": ("Auto-apply couldn't finish {unit} - apply yourself now", 1),
    "failed": ("Auto-apply couldn't finish {unit} - apply yourself now", 1),
}


def result_alert(unit: dict, attempt: Attempt, link: str | None = None) -> dict:
    import html

    title, priority = _RESULT_TITLES[attempt.status]
    lines = [check_units._linked_line(unit), html.escape(attempt.detail)]
    if attempt.missing:
        lines.append("Required, left empty: " + html.escape("; ".join(attempt.missing)))
    if attempt.filled:
        lines.append(f"Filled {len(attempt.filled)} fields from your profile.")
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
                        help="fill and submit the FAKE form in tests/fixtures/apply_site with your profile")
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
                "price": 2850, "availableDate": "2026-11-01T00:00:00Z"}
    else:
        url = args.unit_url
        unit = {"unitSpk": "manual", "name": "manual-test", "building": {"address": "dry run from the command line"}}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            try:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                context.set_default_timeout(15000)
                page = context.new_page()
                attempt = fill_application(page, url, form_values(profile, unit), submit=args.selftest)
                context.close()
            finally:
                browser.close()
    finally:
        if server:
            server.shutdown()

    print(f"Result: {attempt.status} -- {attempt.detail}")
    print(f"Filled from your profile: {', '.join(attempt.filled) or 'nothing'}")
    if attempt.missing:
        print(f"Required fields left empty: {'; '.join(attempt.missing)}")
    _report(unit, attempt, link=url if args.unit_url else check_units.LISTINGS_URL)
    return 0 if attempt.status in ("submitted", "filled") else 1


if __name__ == "__main__":
    sys.exit(main())
