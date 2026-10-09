"""
Automated checks for auto-apply -- no real site, no phone, no git.

The browser tests drive a real (headless) Chromium through FAKE copies of a
StuyTown unit page and its application form, in tests/fixtures/apply_site;
they're skipped if Playwright or its browser isn't installed.

Run from the repo folder:
    python -m unittest discover -s tests -v
"""

import functools
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

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
        def record(*attempts):
            return {"attempts": [{"mode": mode, "status": status} for mode, status in attempts]}

        with mock.patch.object(auto_apply, "MODE", "submit"):
            self.assertIsNone(auto_apply.already_handled(None))
            self.assertIsNone(auto_apply.already_handled(record(("dry_run", "filled"))))  # a dry run doesn't count
            self.assertIsNone(auto_apply.already_handled(record(("submit", "failed"))))  # one retry after a crash
            self.assertIn("gave up", auto_apply.already_handled(record(("submit", "failed"), ("submit", "failed"))))
            self.assertIn("already applied", auto_apply.already_handled(record(("submit", "submitted"))))
            # It may have gone through, so it's never sent a second time.
            self.assertIn("already applied", auto_apply.already_handled(record(("submit", "unconfirmed"))))
            self.assertIn("rejected", auto_apply.already_handled(record(("submit", "rejected"))))
            self.assertIn("incomplete", auto_apply.already_handled(record(("submit", "incomplete"))))
        with mock.patch.object(auto_apply, "MODE", "dry_run"):
            self.assertIn("already tried", auto_apply.already_handled(record(("dry_run", "filled"))))


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
        self.tried, self.sent = [], []
        self.status = "submitted"
        for target, name, value in [
            (auto_apply, "APPLICATIONS_FILE", str(self.dir / "applications.json")),
            (auto_apply, "PRIVATE_DIR", str(self.dir / "private")),
            (auto_apply, "MAX_RENT", 3000),
            (auto_apply, "_skips_logged", set()),
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

    def fake_apply(self, browser, listed_unit, profile, submit):
        self.tried.append((check_units.apartment(listed_unit), submit))
        return auto_apply.Attempt(self.status, "test", filled=["first_name"], form_url="https://example.test/apply",
                                  form_fields=["First Name *"], screenshot=b"\xff\xd8 fake jpeg")

    def record_alert(self, **alert):
        self.sent.append(alert)
        return True

    def run_hook(self, units, mode="submit"):
        with mock.patch.object(auto_apply, "MODE", mode), mock.patch("builtins.print"):
            auto_apply.act_on_listings(units)

    def test_off_does_nothing(self):
        self.run_hook([unit()], mode="off")
        self.assertEqual((self.tried, self.sent), ([], []))

    def test_applies_to_qualifying_units_cheapest_first_and_reports_each(self):
        self.run_hook([unit("9F", 4380), unit("2B", 2900), unit("1A", 2500)])
        self.assertEqual(self.tried, [("1A", True), ("2B", True)])
        self.assertEqual([a["title"] for a in self.sent], ["Applied: Apt 1A, 287 Avenue C",
                                                           "Applied: Apt 2B, 287 Avenue C"])
        self.assertEqual(self.sent[0]["attachment"][2], "image/jpeg")  # the screenshot goes to your phone
        self.assertIn("within 24 hours", self.sent[0]["message"])  # the detailed application comes next

    def test_the_same_unit_on_later_checks_is_not_applied_to_again(self):
        for _ in range(5):
            self.run_hook([unit("1A", 2500)])
        self.assertEqual(self.tried, [("1A", True)])

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

    def test_dry_run_never_submits(self):
        self.status = "filled"
        self.run_hook([unit("1A", 2500)], mode="dry_run")
        self.assertEqual(self.tried, [("1A", False)])
        self.assertTrue(self.sent[0]["title"].startswith("[DRY RUN]"))

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

        handler = functools.partial(_QuietHandler, directory=str(APPLY_SITE))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def apply(self, profile=EXAMPLE_PROFILE, submit=True, site_options=""):
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        context.set_default_timeout(5000)
        page = context.new_page()
        url = f"http://127.0.0.1:{self.server.server_port}/unit.html?unitSpk=TEST{site_options}"
        started = time.monotonic()
        attempt = auto_apply.fill_application(page, url, auto_apply.form_values(profile), submit=submit)
        self.seconds = time.monotonic() - started
        return attempt, context.pages[-1].evaluate("() => window.submitted || null")

    def test_submit_fills_every_field_and_the_site_confirms(self):
        attempt, submitted = self.apply()
        self.assertEqual(attempt.status, "submitted", attempt.detail)
        self.assertEqual(submitted, self.EXPECTED)
        self.assertEqual(attempt.problems, [])
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

    def test_dry_run_fills_everything_but_never_submits(self):
        attempt, submitted = self.apply(submit=False)
        self.assertEqual(attempt.status, "filled", attempt.detail)
        self.assertEqual(attempt.problems, [])
        self.assertIsNone(submitted)

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


@unittest.skipUnless(_browser_available(), "Playwright + Chromium not installed")
class EndToEndTests(unittest.TestCase):
    """What the watcher does when a cheap unit is listed: the hook, a fresh
    browser, the fake site standing in for StuyTown, the log and the alert."""

    def test_a_cheap_new_listing_gets_applied_to_once(self):
        handler = functools.partial(_QuietHandler, directory=str(APPLY_SITE))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        alerts = []
        with mock.patch.object(check_units, "UNIT_PAGE_URL", f"http://127.0.0.1:{server.server_port}/unit.html"), \
                mock.patch.object(check_units, "notify", lambda **a: alerts.append(a) or True), \
                mock.patch.object(auto_apply, "APPLICATIONS_FILE", f"{tmp.name}/applications.json"), \
                mock.patch.object(auto_apply, "PRIVATE_DIR", f"{tmp.name}/private"), \
                mock.patch.object(auto_apply, "MODE", "submit"), mock.patch.object(auto_apply, "MAX_RENT", 3000), \
                mock.patch.object(auto_apply, "_profile_for_run", lambda: EXAMPLE_PROFILE), \
                mock.patch.object(auto_apply, "_skips_logged", set()), \
                mock.patch.object(auto_apply, "_submitted_this_run", 0), mock.patch("builtins.print"):
            for _ in range(2):  # the same listing on the next check
                auto_apply.act_on_listings([unit("1A", 2850), unit("9F", 4380.54)])
        self.assertEqual([a["title"] for a in alerts], ["Applied: Apt 1A, 287 Avenue C"])
        self.assertEqual(alerts[0]["attachment"][1][:2], b"\xff\xd8")  # a JPEG screenshot
        log = json.loads(Path(f"{tmp.name}/applications.json").read_text(encoding="utf-8"))
        self.assertEqual(list(log), ["P~TEST~U~1A"])
        self.assertEqual(log["P~TEST~U~1A"]["attempts"][0]["status"], "submitted")
        self.assertEqual(len(list(Path(f"{tmp.name}/private").iterdir())), 1)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


if __name__ == "__main__":
    unittest.main()
