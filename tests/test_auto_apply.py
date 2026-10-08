"""
Automated checks for auto-apply -- no real site, no phone, no git.

The browser tests drive a real (headless) Chromium through the FAKE unit page
and two-page form in tests/fixtures/apply_site; they're skipped if Playwright
or its browser isn't installed.

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
        self.assertIn("over $3,000", auto_apply.skip_reason(unit(price=3040.84), EXAMPLE_PROFILE))

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

    def test_missing_required_details_are_named(self):
        with self.assertRaises(auto_apply.ProfileError) as caught:
            self.load(json.dumps({**EXAMPLE_PROFILE, "email": "", "phone": None}))
        self.assertIn("email, phone", str(caught.exception))

    def test_every_value_is_masked_in_github_logs(self):
        _, log = self.load(json.dumps(EXAMPLE_PROFILE), github=True)
        self.assertIn("::add-mask::Jane\n", log)
        self.assertIn("::add-mask::jane.doe@example.com\n", log)
        self.assertIn("::add-mask::95000\n", log)
        self.assertIn("::add-mask::Example Company\n", log)
        self.assertNotIn("::add-mask::NY\n", log)  # too short: would blank out the whole log
        self.assertNotIn("Made-up example", log)  # "_" keys are notes, not details

    def test_nothing_is_masked_outside_github(self):
        self.assertEqual(self.load(json.dumps(EXAMPLE_PROFILE))[1], "")

    def test_move_in_date_defaults_to_the_units_available_date(self):
        values = auto_apply.form_values({**EXAMPLE_PROFILE, "move_in_date": ""},
                                        unit(availableDate="2099-11-01T00:00:00Z"))
        self.assertEqual(values["move_in_date"], "2099-11-01")
        self.assertEqual(values["full_name"], "Jane Doe")
        own_date = auto_apply.form_values({**EXAMPLE_PROFILE, "move_in_date": "2099-12-15"}, unit())
        self.assertEqual(own_date["move_in_date"], "2099-12-15")


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
        fake_playwright = mock.MagicMock()
        patcher = mock.patch.dict(sys.modules, {"playwright.sync_api": fake_playwright})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_apply(self, browser, listed_unit, profile, submit):
        self.tried.append((check_units.apartment(listed_unit), submit))
        return auto_apply.Attempt(self.status, "test", filled=["first_name"], screenshot=b"\xff\xd8 fake jpeg")

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
        self.assertEqual(record["attempts"][0]["status"], "submitted")
        for value in ("Jane", "Doe", "jane.doe@example.com", "212-555-0123", "95000", "Example Street"):
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
    """The real form-filling code, in a real browser, on the fake site."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        handler = functools.partial(_QuietHandler, directory=str(APPLY_SITE))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/unit.html?unitSpk=TEST"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def fill(self, profile, submit):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.set_default_timeout(5000)
        page = context.new_page()
        listed = unit(availableDate="2099-11-01T00:00:00Z")
        attempt = auto_apply.fill_application(page, self.url, auto_apply.form_values(profile, listed), submit=submit)
        return attempt, context.pages[-1].evaluate("() => window.submitted || null")

    def test_submit_fills_both_pages_and_the_site_confirms(self):
        attempt, submitted = self.fill(EXAMPLE_PROFILE, submit=True)
        self.assertEqual(attempt.status, "submitted", attempt.detail)
        self.assertTrue(attempt.screenshot)
        self.assertEqual(submitted, {
            "first_name": "Jane", "last_name": "Doe",
            "email": "jane.doe@example.com", "email_confirm": "jane.doe@example.com",
            "phone": "212-555-0123", "date_of_birth": "1990-01-31",  # a date picker takes YYYY-MM-DD
            "street": "123 Example Street", "apt": "4B", "city": "New York", "state": "NY", "zip": "10009",
            "income": "95000", "household_size": "1", "employer": "Example Company",
            "move_in": "11/01/2099",  # a text box gets MM/DD/YYYY (the unit's available date)
            "pets": "no", "certify": "on",
        })

    def test_dry_run_fills_everything_but_never_submits(self):
        attempt, submitted = self.fill(EXAMPLE_PROFILE, submit=False)
        self.assertEqual(attempt.status, "filled", attempt.detail)
        self.assertEqual(attempt.missing, [])
        self.assertIsNone(submitted)

    def test_a_required_field_it_cant_fill_stops_it_and_is_named(self):
        profile = {k: v for k, v in EXAMPLE_PROFILE.items() if k != "employer"}
        attempt, submitted = self.fill(profile, submit=True)
        self.assertEqual(attempt.status, "incomplete", attempt.detail)
        self.assertEqual(attempt.missing, ["Employer *"])
        self.assertIsNone(submitted)

    def test_a_missing_answer_on_page_one_stops_before_next(self):
        profile = {**EXAMPLE_PROFILE, "zip": ""}
        attempt, submitted = self.fill(profile, submit=True)
        self.assertEqual(attempt.status, "incomplete")
        self.assertIn("page 1", attempt.detail)
        self.assertEqual(attempt.missing, ["ZIP Code *"])


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


if __name__ == "__main__":
    unittest.main()
