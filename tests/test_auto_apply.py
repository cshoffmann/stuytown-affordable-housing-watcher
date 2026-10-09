"""
Automated checks for auto-apply -- no real site, no phone, no git.

The browser tests drive a real (headless) Chromium through FAKE copies of a
StuyTown unit page and its application form, in tests/fixtures/apply_site;
they're skipped if Playwright or its browser isn't installed.

Run from the repo folder:
    python -m unittest discover -s tests -v
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import apply_recorder  # noqa: E402
import auto_apply  # noqa: E402
import check_units  # noqa: E402

FAKE_UNITS = json.loads((REPO_ROOT / "tests" / "fixtures" / "fake_units.json").read_text(encoding="utf-8"))
EXAMPLE_PROFILE = json.loads((REPO_ROOT / "applicant_profile.example.json").read_text(encoding="utf-8"))
APPLY_SITE = REPO_ROOT / "tests" / "fixtures" / "apply_site"
# Every value of the example profile, as it must never appear in public places.
PERSONAL = ("Jane", "Doe", "jane.doe@example.com", "212-555-0123", "Example Street", "95000", "10009")


def unit(apartment="5A", price=2850.0, income_requirement=60000, **extra):
    return {
        **FAKE_UNITS["5A"], "unitSpk": f"P~TEST~U~{apartment}", "name": apartment,
        "price": price, "incomeRequirement": income_requirement, **extra,
    }


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(auto_apply, "MAX_RENT", 3000)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rent_at_or_under_the_limit_qualifies(self):
        self.assertIsNone(auto_apply.skip_reason(unit(price=2999.99), EXAMPLE_PROFILE))
        self.assertIsNone(auto_apply.skip_reason(unit(price=3000), EXAMPLE_PROFILE))

    def test_rent_over_the_limit_is_skipped(self):
        self.assertIn("over $3,000.00", auto_apply.skip_reason(unit(price=3040.84), EXAMPLE_PROFILE))

    def test_the_limit_comes_from_the_setting(self):
        with mock.patch.object(auto_apply, "MAX_RENT", 3500):
            self.assertIsNone(auto_apply.skip_reason(unit(price=3040.84), EXAMPLE_PROFILE))
        with mock.patch.object(auto_apply, "MAX_RENT", None):
            self.assertIn("isn't set", auto_apply.skip_reason(unit(price=100), EXAMPLE_PROFILE))

    def test_without_a_price_the_cheapest_lease_rate_counts(self):
        cheap = unit(price=None, unitRates={"12": 3100, "24": 2950})
        self.assertEqual(check_units.unit_rent(cheap), 2950)
        self.assertIsNone(auto_apply.skip_reason(cheap, EXAMPLE_PROFILE))
        self.assertEqual(auto_apply.skip_reason(unit(price=None, unitRates={}), EXAMPLE_PROFILE), "no rent listed")

    def test_income_below_the_units_minimum_is_skipped_without_saying_your_income(self):
        reason = auto_apply.skip_reason(unit(income_requirement=104257.37), {**EXAMPLE_PROFILE, "annual_income": 95000})
        self.assertIn("$104,257 minimum", reason)
        self.assertNotIn("95", reason)

    def test_a_unit_is_never_applied_to_twice(self):
        def record(*statuses):
            return {"attempts": [{"status": status} for status in statuses]}

        self.assertIsNone(auto_apply.already_handled(None))
        self.assertIsNone(auto_apply.already_handled(record("failed")))  # one retry after a crash
        self.assertIn("gave up", auto_apply.already_handled(record("failed", "failed")))
        self.assertIn("already applied", auto_apply.already_handled(record("submitted")))
        # It may have gone through, so it's never sent a second time.
        self.assertIn("already applied", auto_apply.already_handled(record("unconfirmed")))
        self.assertIn("rejected", auto_apply.already_handled(record("rejected")))
        self.assertIn("incomplete", auto_apply.already_handled(record("incomplete")))


class ProfileTests(unittest.TestCase):
    def load(self, raw=None, file_text=None, github=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        profile_file = Path(tmp.name) / "applicant_profile.json"
        if file_text is not None:
            profile_file.write_text(file_text, encoding="utf-8")
        env = {"GITHUB_ACTIONS": "true"} if github else {}
        if raw is not None:
            env[auto_apply.PROFILE_ENV] = raw
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(auto_apply, "PROFILE_FILE", str(profile_file)), redirect_stdout(out):
            profile = auto_apply.load_profile()
        return profile, out.getvalue()

    def problems_with(self, **changes):
        with self.assertRaises(auto_apply.ProfileError) as caught:
            self.load(json.dumps({**EXAMPLE_PROFILE, **changes}))
        return str(caught.exception)

    def test_secret_holding_the_json_is_read(self):
        profile, _ = self.load(json.dumps(EXAMPLE_PROFILE))
        self.assertEqual(profile["first_name"], "Jane")

    def test_local_file_is_used_when_there_is_no_secret(self):
        profile, _ = self.load(file_text=json.dumps(EXAMPLE_PROFILE))
        self.assertEqual(profile["last_name"], "Doe")
        self.assertIsNone(self.load()[0])

    def test_broken_json_is_reported_without_echoing_any_of_it(self):
        with self.assertRaises(auto_apply.ProfileError) as caught:
            self.load('{"first_name": "Jane Secretname", oops}')
        self.assertIn("isn't valid JSON", str(caught.exception))
        self.assertNotIn("Secretname", str(caught.exception))

    def test_every_field_the_form_requires_must_be_there(self):
        message = self.problems_with(building="", annual_income=None)
        self.assertIn("missing building, annual_income", message)
        self.assertEqual(auto_apply.REQUIRED_PROFILE_KEYS, (
            "first_name", "last_name", "email", "cell_phone", "building", "street_name", "city", "zip",
            "household_size", "annual_income"))

    def test_malformed_details_are_named_but_not_echoed(self):
        message = self.problems_with(email="jane.example.com", cell_phone="867-5309", zip="1009",
                                     household_size="two", annual_income="lots")
        for key in ("email", "cell_phone", "zip", "household_size", "annual_income"):
            self.assertIn(key, message)
        for value in ("jane.example.com", "867-5309", "1009", "lots"):
            self.assertNotIn(value, message)

    def test_phone_numbers_get_the_us_country_code(self):
        self.assertEqual(auto_apply.phone_digits("212-555-0123"), "12125550123")
        self.assertEqual(auto_apply.phone_digits("+1 (212) 555-0123"), "12125550123")
        self.assertIsNone(auto_apply.phone_digits("+44 20 7946 0958"))
        self.assertIsNone(auto_apply.phone_digits("555-0123"))

    def test_every_value_is_masked_in_github_logs(self):
        _, log = self.load(json.dumps(EXAMPLE_PROFILE), github=True)
        for value in ("Jane", "jane.doe@example.com", "212-555-0123", "12125550123", "2125550123",
                      "95000", "Example Street", "10009"):
            self.assertIn(f"::add-mask::{value}\n", log)
        self.assertNotIn("::add-mask::NY\n", log)  # too short: would blank out the whole log
        self.assertNotIn("Made-up example", log)  # "_" keys are notes, not details

    def test_nothing_is_masked_outside_github(self):
        self.assertEqual(self.load(json.dumps(EXAMPLE_PROFILE))[1], "")

    def test_auto_apply_stays_off_for_the_run_without_a_rent_limit(self):
        alerts = []
        auto_apply._profile_for_run.cache_clear()
        self.addCleanup(auto_apply._profile_for_run.cache_clear)
        with mock.patch.object(auto_apply, "MAX_RENT", None), \
                mock.patch.object(check_units, "notify", lambda **a: alerts.append(a) or True), \
                mock.patch("builtins.print"):
            self.assertIsNone(auto_apply._profile_for_run())
        self.assertIn("AUTO_APPLY_MAX_RENT", alerts[0]["message"])


class ReadBackTests(unittest.TestCase):
    """How a value is judged to have 'taken' in a masked box."""

    def test_phone_box_with_plus_needs_the_us_country_code(self):
        self.assertTrue(auto_apply._shows("cell_phone", "+1 (212) 555-0123", "212-555-0123"))
        self.assertFalse(auto_apply._shows("cell_phone", "+212 555 0123", "212-555-0123"))  # that's Morocco
        self.assertTrue(auto_apply._shows("cell_phone", "(212) 555-0123", "212-555-0123"))  # a plain box

    def test_money_box_formatting_is_fine_but_the_amount_must_match(self):
        self.assertTrue(auto_apply._shows("annual_income", "95,000.00", 95000))
        self.assertFalse(auto_apply._shows("annual_income", "950.00", 95000))

    def test_only_complaints_count_as_the_site_refusing_the_form(self):
        for complaint in ("This field is required", "Invalid phone number", "Something went wrong. Please try again",
                          "You have already applied for this apartment"):
            self.assertRegex(complaint, auto_apply.SITE_ERROR)
        for progress in ("Submitting...", "Sending your application", "Loading"):
            self.assertNotRegex(progress, auto_apply.SITE_ERROR)

    def test_ways_of_typing_tried_in_order(self):
        self.assertEqual(auto_apply._spellings("cell_phone", "212-555-0123"), ["+12125550123", "2125550123"])
        self.assertEqual(auto_apply._spellings("annual_income", 95000), ["95000", "95000.00", "9500000"])
        self.assertEqual(auto_apply._spellings("household_size", "2"), ["2"])


class ActOnListingsTests(unittest.TestCase):
    """The watcher's hook, with the browser part replaced by a recorder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.tried, self.recorded, self.sent = [], [], []
        self.status = "submitted"
        for target, name, value in [
            (auto_apply, "APPLICATIONS_FILE", str(self.dir / "applications.json")),
            (auto_apply, "PRIVATE_DIR", str(self.dir / "private")),
            (auto_apply, "MAX_RENT", 3000),
            (auto_apply, "_skips_logged", set()),
            (auto_apply, "_recorded_this_run", set()),
            (auto_apply, "_submitted_this_run", 0),
            (auto_apply, "apply_to_unit", self.fake_apply),
            (auto_apply, "_profile_for_run", lambda: EXAMPLE_PROFILE),
            (check_units, "notify", self.record_alert),
        ]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The browser itself isn't needed: apply_to_unit is faked.
        patcher = mock.patch.dict(sys.modules, {"playwright.sync_api": mock.MagicMock()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_apply(self, browser, listed_unit, profile, record_only=False):
        if record_only:
            self.recorded.append(check_units.apartment(listed_unit))
            return auto_apply.Attempt("recorded", "test")
        self.tried.append(check_units.apartment(listed_unit))
        return auto_apply.Attempt(self.status, "test", filled=["first_name"], form_url="https://example.test/apply",
                                  form_fields=["First Name *"], filled_screenshot=b"\xff\xd8 filled",
                                  result_screenshot=b"\xff\xd8 after", seconds=2.0)

    def record_alert(self, **alert):
        self.sent.append(alert)
        return True

    def run_hook(self, units, enabled=True):
        with mock.patch.object(auto_apply, "ENABLED", enabled), mock.patch("builtins.print"):
            auto_apply.act_on_listings(units)

    def test_off_does_nothing_at_all(self):
        self.run_hook([unit("1A", 2500), unit("9F", 4380)], enabled=False)
        self.assertEqual((self.tried, self.recorded, self.sent), ([], [], []))

    def test_applies_to_qualifying_units_cheapest_first_and_reports_each(self):
        self.run_hook([unit("9F", 4380), unit("2B", 2900), unit("1A", 2500)])
        self.assertEqual(self.tried, ["1A", "2B"])
        titles = [a["title"] for a in self.sent]
        self.assertEqual(titles, ["Applied: Apt 1A, 287 Avenue C", "After SUBMIT: Apt 1A, 287 Avenue C",
                                  "Applied: Apt 2B, 287 Avenue C", "After SUBMIT: Apt 2B, 287 Avenue C"])
        # The filled-in form goes with the main alert, the page after SUBMIT in a quiet second one.
        self.assertEqual(self.sent[0]["attachment"], ("filled-form.jpg", b"\xff\xd8 filled", "image/jpeg"))
        self.assertEqual((self.sent[1]["attachment"][1], self.sent[1]["priority"]), (b"\xff\xd8 after", -1))
        self.assertIn("within 24 hours", self.sent[0]["message"])  # the detailed application comes next

    def test_the_same_unit_on_later_checks_is_not_applied_to_again(self):
        for _ in range(5):
            self.run_hook([unit("1A", 2500)])
        self.assertEqual(self.tried, ["1A"])

    def test_forms_of_units_over_the_limit_are_recorded_two_per_morning_without_applying(self):
        for _ in range(3):
            self.run_hook([unit("9F", 4380), unit("8E", 4100), unit("7D", 3900), unit("1A", 2500)])
        self.assertEqual(self.tried, ["1A"])
        self.assertEqual(self.recorded, ["9F", "8E"])  # each once, and no more than two
        self.assertEqual([a["title"] for a in self.sent if "Applied" in a["title"]], ["Applied: Apt 1A, 287 Avenue C"])
        self.assertEqual(list(json.loads(Path(auto_apply.APPLICATIONS_FILE).read_text())), ["P~TEST~U~1A"])

    def test_a_crashed_attempt_gets_one_more_try(self):
        self.status = "failed"
        for _ in range(4):
            self.run_hook([unit("1A", 2500)])
        self.assertEqual(len(self.tried), auto_apply.MAX_FAILED_ATTEMPTS_PER_UNIT)
        self.assertIn("apply yourself now", self.sent[0]["title"])

    def test_a_refused_form_tells_you_to_apply_yourself_and_isnt_retried(self):
        self.status = "rejected"
        for _ in range(3):
            self.run_hook([unit("1A", 2500)])
        self.assertEqual(len(self.tried), 1)
        self.assertIn("apply yourself now", self.sent[0]["title"])

    def test_stops_after_the_per_run_limit(self):
        self.run_hook([unit(f"{n}A", 2000 + n) for n in range(1, 6)])
        self.assertEqual(len(self.tried), auto_apply.MAX_APPLICATIONS_PER_RUN)

    def test_the_public_log_has_the_unit_and_outcome_but_none_of_your_details(self):
        self.run_hook([unit("1A", 2500)])
        log_text = Path(auto_apply.APPLICATIONS_FILE).read_text(encoding="utf-8")
        record = json.loads(log_text)["P~TEST~U~1A"]
        self.assertEqual((record["apartment"], record["rent"]), ("1A", 2500))
        attempt = record["attempts"][0]
        self.assertEqual((attempt["status"], attempt["form_url"]), ("submitted", "https://example.test/apply"))
        for value in PERSONAL:
            self.assertNotIn(value, log_text)

    def test_runs_right_after_the_new_unit_alert_and_before_the_screenshot(self):
        order = []
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(check_units, "STATE_FILE", f"{tmp}/state.json"), \
                mock.patch.object(check_units, "EVENTS_FILE", f"{tmp}/events.json"), \
                mock.patch.object(check_units, "notify", lambda **a: order.append("alert") or True), \
                mock.patch("builtins.print"):
            check_units.process_snapshot(
                [unit("1A", 2500)],
                take_screenshot=lambda path: order.append("screenshot") or False,
                act_on_listings=lambda units: order.append("auto-apply"),
            )
        self.assertEqual(order, ["alert", "auto-apply", "screenshot"])

    def test_a_crash_in_auto_apply_never_stops_the_watcher(self):
        def broken(units):
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(check_units, "STATE_FILE", f"{tmp}/state.json"), \
                mock.patch.object(check_units, "EVENTS_FILE", f"{tmp}/events.json"), \
                mock.patch("builtins.print"):
            changes = check_units.process_snapshot([unit("1A", 2500)], act_on_listings=broken)
        self.assertEqual(len(changes.new), 1)


class PushoverAttachmentTests(unittest.TestCase):
    def test_image_is_sent_as_multipart(self):
        body, content_type = check_units._multipart({"title": "Hi", "priority": 1}, ("a.jpg", b"JPEGDATA", "image/jpeg"))
        boundary = content_type.split("boundary=")[1]
        self.assertTrue(content_type.startswith("multipart/form-data"))
        self.assertIn(b'name="title"\r\n\r\nHi\r\n', body)
        self.assertIn(b'name="attachment"; filename="a.jpg"\r\nContent-Type: image/jpeg\r\n\r\nJPEGDATA\r\n', body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))


def _browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_browser_available(), "Playwright + Chromium not installed")
class FormFillingTests(unittest.TestCase):
    """The real form-filling code, in a real browser, on the fake copy of
    StuyTown's unit page and form."""

    EXPECTED = {
        "firstName": "Jane", "lastName": "Doe", "email": "jane.doe@example.com",
        "cellPhone": "+1 212 555 0123",  # the US country code, not "+212..."
        "workPhone": "+",  # optional, left empty
        "building": "123", "streetName": "Example Street", "apartmentNo": "4B",
        "city": "New York", "state": "NY", "zip": "10009",
        "householdSize": "1", "income": "95,000.00",
    }

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.server = auto_apply._serve_selftest_site()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def apply(self, profile=EXAMPLE_PROFILE, submit=True, site_options="", record_only=False):
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        context.set_default_timeout(5000)
        page = context.new_page()
        url = f"http://127.0.0.1:{self.server.server_port}/unit.html?unitSpk=TEST{site_options}"
        started = time.monotonic()
        values = None if record_only else auto_apply.form_values(profile)
        attempt = auto_apply.run_form(page, url, values, submit=submit, apartment="TEST")
        self.seconds = time.monotonic() - started
        return attempt, context.pages[-1].evaluate("() => window.submitted || null")

    def test_submit_fills_every_field_and_the_site_confirms(self):
        attempt, submitted = self.apply()
        self.assertEqual(attempt.status, "submitted", attempt.detail)
        self.assertEqual(submitted, self.EXPECTED)
        self.assertEqual(attempt.problems, [])
        self.assertTrue(attempt.filled_screenshot)  # taken before SUBMIT -- and SUBMIT still worked
        self.assertTrue(attempt.result_screenshot)
        self.assertTrue(attempt.form_url.endswith("/apply.html?unitSpk=TEST"))
        self.assertLess(self.seconds, 15)

    def test_it_reads_labels_that_arent_wired_to_their_boxes(self):
        attempt, _ = self.apply(submit=False)
        self.assertEqual(attempt.form_fields, [
            "First Name *", "Last Name *", "Email *", "Cell Phone *", "Work Phone", "Building *",
            "Street name *", "Apartment No.", "City *", "State", "Zip *", "Household Size * (i)",
            "Household Gross Annual Income, $ * $",  # not just the "$" beside the box
        ])

    def test_a_money_box_that_fills_from_the_cents_still_gets_the_right_amount(self):
        attempt, submitted = self.apply(site_options="&income=cents")
        self.assertEqual(attempt.status, "submitted", attempt.detail)
        self.assertEqual(submitted["income"], "95,000.00")

    def test_form_check_fills_everything_but_never_submits(self):
        attempt, submitted = self.apply(submit=False)
        self.assertEqual(attempt.status, "filled", attempt.detail)
        self.assertEqual(attempt.problems, [])
        self.assertIsNone(submitted)

    def test_recording_only_opens_the_form_and_fills_nothing(self):
        attempt, submitted = self.apply(record_only=True)
        self.assertEqual(attempt.status, "recorded", attempt.detail)
        self.assertEqual(len(attempt.form_fields), 13)
        self.assertEqual(attempt.filled, [])
        self.assertIsNone(submitted)

    def test_a_submit_click_that_misses_is_noticed_and_retried(self):
        # The original bug: a plain full-page screenshot right before SUBMIT
        # makes the click land beside the button.
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{self.server.server_port}/apply.html?unitSpk=TEST")
        scope = page.locator("form").first
        auto_apply._fill_visible_fields(scope, auto_apply.form_values(EXAMPLE_PROFILE), {})
        button = auto_apply._visible_button(scope, auto_apply.SUBMIT_BUTTON)
        page.screenshot(full_page=True)
        recorder = apply_recorder.RunRecorder(None, apply_recorder.Redactor(None))
        self.assertTrue(auto_apply._press(button, page, recorder))
        page.wait_for_timeout(500)
        self.assertEqual(page.evaluate("() => !!window.submitted"), True)
        self.assertEqual([s["step"] for s in recorder.steps], ["the SUBMIT click missed the button"])

    def test_an_empty_required_field_stops_it_and_is_named(self):
        attempt, submitted = self.apply({**EXAMPLE_PROFILE, "zip": ""})
        self.assertEqual(attempt.status, "incomplete", attempt.detail)
        self.assertEqual(attempt.problems, ["Zip *"])
        self.assertIsNone(submitted)

    def test_a_renamed_field_stops_it_instead_of_sending_a_half_empty_form(self):
        renamed = [(key, r"^postcode" if key == "zip" else pattern, required)
                   for key, pattern, required in auto_apply.FORM_FIELDS]
        with mock.patch.object(auto_apply, "FORM_FIELDS", renamed):
            attempt, submitted = self.apply()
        self.assertEqual(attempt.status, "incomplete", attempt.detail)
        self.assertIn("no field found for zip", attempt.problems)
        self.assertIsNone(submitted)

    def test_a_refused_submit_is_reported_quickly_as_rejected(self):
        attempt, submitted = self.apply(site_options="&reject=1")
        self.assertEqual(attempt.status, "rejected", attempt.detail)
        self.assertEqual(attempt.site_messages, ["Something went wrong. Please try again later."])
        self.assertIsNone(submitted)
        self.assertLess(self.seconds, 15)  # not the full 30s confirmation wait

    def test_extra_fields_fill_boxes_by_label(self):
        attempt, submitted = self.apply({**EXAMPLE_PROFILE, "work_phone": "",
                                         "extra_fields": {"Work Phone": "+1 646 555 0199"}})
        self.assertEqual(attempt.status, "submitted", attempt.detail)
        self.assertEqual(submitted["workPhone"], "+1 646 555 0199")


class RedactorTests(unittest.TestCase):
    """What's committed to the public repo must not contain your details, in
    any of the forms a page or a request might show them."""

    def test_every_form_of_every_value_is_replaced_with_its_key(self):
        redact = apply_recorder.Redactor(EXAMPLE_PROFILE)
        text = ("JANE doe <jane.doe@example.com> +1 (212) 555-0123 / 2125550123 / +12125550123, "
                "123 Example Street, New York 10009, income $95,000.00 = 95000 = 9500000 cents")
        self.assertEqual(redact(text), (
            "<first_name> <last_name> <<email>> <cell_phone> / <cell_phone> / <cell_phone>, "
            "<building> <street_name>, <city> <zip>, income $<annual_income> = <annual_income> = <annual_income> cents"))

    def test_lookalikes_are_left_alone(self):
        redact = apply_recorder.Redactor(EXAMPLE_PROFILE)
        for text in ("Janet", "1230", "100095", "195000", "95000.5"):
            self.assertEqual(redact(text), text)

    def test_url_encoded_bodies_and_nested_json(self):
        redact = apply_recorder.Redactor(EXAMPLE_PROFILE)
        recorder = apply_recorder.RunRecorder(None, redact)
        body = recorder._redact_body("email=jane.doe%40example.com&street=Example+Street",
                                     "application/x-www-form-urlencoded")
        self.assertEqual(body, "email=<email>&street=<street_name>")
        self.assertEqual(redact.deep({"applicant": {"name": "Jane", "phones": ["212.555.0123"]}}),
                         {"applicant": {"name": "<first_name>", "phones": ["<cell_phone>"]}})


@unittest.skipUnless(_browser_available(), "Playwright + Chromium not installed")
class EndToEndTests(unittest.TestCase):
    """What the watcher does when units are listed: the hook, a fresh browser,
    the fake site standing in for StuyTown, the recordings, the log, the alerts."""

    def test_a_cheap_listing_is_applied_to_once_and_an_expensive_ones_form_is_recorded(self):
        server = auto_apply._serve_selftest_site()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        alerts = []
        with mock.patch.object(check_units, "UNIT_PAGE_URL", f"http://127.0.0.1:{server.server_port}/unit.html"), \
                mock.patch.object(check_units, "notify", lambda **a: alerts.append(a) or True), \
                mock.patch.object(apply_recorder, "RUNS_DIR", f"{tmp.name}/apply_runs"), \
                mock.patch.object(auto_apply, "APPLICATIONS_FILE", f"{tmp.name}/applications.json"), \
                mock.patch.object(auto_apply, "PRIVATE_DIR", f"{tmp.name}/private"), \
                mock.patch.object(auto_apply, "ENABLED", True), mock.patch.object(auto_apply, "MAX_RENT", 3000), \
                mock.patch.object(auto_apply, "_profile_for_run", lambda: EXAMPLE_PROFILE), \
                mock.patch.object(auto_apply, "_skips_logged", set()), \
                mock.patch.object(auto_apply, "_recorded_this_run", set()), \
                mock.patch.object(auto_apply, "_submitted_this_run", 0), mock.patch("builtins.print"):
            for _ in range(2):  # the same listings on the next check
                auto_apply.act_on_listings([unit("1A", 2850), unit("9F", 4380.54)])

        self.assertEqual([a["title"] for a in alerts], ["Applied: Apt 1A, 287 Avenue C",
                                                        "After SUBMIT: Apt 1A, 287 Avenue C"])
        self.assertEqual(alerts[0]["attachment"][1][:2], b"\xff\xd8")  # the filled-in form, as a JPEG
        log = json.loads(Path(f"{tmp.name}/applications.json").read_text(encoding="utf-8"))
        self.assertEqual(list(log), ["P~TEST~U~1A"])
        self.assertEqual(log["P~TEST~U~1A"]["attempts"][0]["status"], "submitted")
        self.assertEqual(len(list(Path(f"{tmp.name}/private").iterdir())), 2)  # filled form + after SUBMIT

        runs = sorted(Path(f"{tmp.name}/apply_runs").iterdir())
        self.assertEqual([r.name.split("_", 1)[1] for r in runs], ["1A_apply", "9F_record"])
        applied, recorded = runs
        self.assertEqual(sorted(f.name for f in applied.iterdir()), [
            "1_unit_page.html", "2_form_empty.html", "3_form_filled.html", "4_after_submit.html",
            "network.json", "report.json"])
        self.assertEqual(sorted(f.name for f in recorded.iterdir()), [
            "1_unit_page.html", "1_unit_page.jpg", "2_form_empty.html", "2_form_empty.jpg",
            "network.json", "report.json"])
        report = json.loads((applied / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["outcome"]["status"], "submitted")
        self.assertIn("pressed SUBMIT", [s["step"] for s in report["steps"]])
        submit_request = [n for n in json.loads((applied / "network.json").read_text()) if n["method"] == "POST"]
        self.assertEqual(json.loads(submit_request[0]["request_body"])["firstName"], "<first_name>")
        self.assertIn("TEST-0001", submit_request[0]["response_body"])  # the site's answer
        # Nothing personal in anything that gets committed.
        for file in applied.iterdir():
            text = file.read_text(encoding="utf-8").casefold()
            for value in ("jane", "jane.doe@example.com", "2125550123", "212 555 0123", "95,000", "95000",
                          "example street", "10009"):
                self.assertNotIn(value, text, f"{value!r} in {file.name}")


if __name__ == "__main__":
    unittest.main()
