"""
Automated checks for the morning's run stats (run_stats.py) -- no network.

Run from the repo folder:
    python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import check_units  # noqa: E402
import run_stats  # noqa: E402

FAKE_UNITS = json.loads((REPO_ROOT / "tests" / "fixtures" / "fake_units.json").read_text(encoding="utf-8"))
UNIT_5A = FAKE_UNITS["5A"]
PERSONAL = ("Jane", "Doe", "jane.doe@example.com", "212-555-0123", "Example Street", "95000", "10009")


def at(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def response(ms=50.0, status=200, body=b'{"unitModels": []}', reused=True, **headers):
    return {"ms": ms, "status": status, "bytes": len(body), "reused_connection": reused, "body": body,
            "headers": {k.replace("_", "-"): v for k, v in headers.items()}}


class FakeApplier:
    def __init__(self, applications):
        self.applications = applications

    def observations(self):
        return {"workers": 2, "browser_ready_seconds": [1.2, 1.3]}


class ListingsApiTests(unittest.TestCase):
    def test_response_times_statuses_and_connections(self):
        stats = run_stats.RunStats()
        stats.observe_responses([response(ms=float(ms)) for ms in range(1, 101)])
        stats.observe_responses([response(ms=900.0, status=429, reused=False)])
        api = stats.summary()["listings_api"]
        self.assertEqual(api["requests"], 101)
        self.assertEqual(api["statuses"], {200: 100, 429: 1})
        self.assertEqual(api["connections"], {"kept-open connection": 100, "new connection": 1})
        self.assertEqual((api["response_ms"]["min"], api["response_ms"]["max"]), (1.0, 900.0))
        self.assertEqual(api["response_ms"]["median"], 51.0)
        self.assertEqual(api["response_ms"]["p90"], 91.0)

    def test_caching_headers_are_listed_and_changing_ones_only_counted(self):
        stats = run_stats.RunStats()
        for n in range(3):
            stats.observe_responses([response(cache_control="max-age=5", etag=f'"v{n}"', age=str(n * 3),
                                              x_request_id=f"id-{n}", x_ratelimit_remaining=str(100 - n),
                                              body=f"body {n}".encode())])
        api = stats.summary()["listings_api"]
        self.assertEqual(api["headers"]["cache-control"], {"max-age=5": 3})
        self.assertEqual(api["headers"]["etag"], {"(varies)": 3})
        self.assertEqual(api["headers"]["age"], {"<= 0s": 1, "<= 5s": 1, "<= 10s": 1})
        self.assertEqual(len(api["headers"]["x-ratelimit-remaining"]), 3)
        self.assertEqual(api["headers_present"], {"x-request-id": 3})
        self.assertEqual(api["distinct_responses"], 3)
        stats.observe_responses([response(body=b"body 0")])  # back to one seen before
        self.assertEqual(stats.summary()["listings_api"]["distinct_responses"], 3)
        self.assertTrue(api["etag"]["same_etag_always_same_contents"])

    def test_an_etag_that_doesnt_follow_the_contents_is_noticed(self):
        stats = run_stats.RunStats()
        stats.observe_responses([response(etag='"same"', body=b"one"), response(etag='"same"', body=b"two")])
        self.assertFalse(stats.summary()["listings_api"]["etag"]["same_etag_always_same_contents"])

    def test_their_clock_is_compared_with_ours(self):
        stats = run_stats.RunStats()
        stats.observe_responses([response(date="Sat, 10 Oct 2026 11:00:00 GMT")])
        self.assertIsNotNone(stats.summary()["listings_api"]["their_clock_minus_ours_s"])

    def test_failures_and_pace_changes(self):
        stats = run_stats.RunStats()
        stats.observe_failure(TimeoutError("timed out"))
        stats.observe_failure(TimeoutError("timed out"))
        stats.observe_pace(4, "error: timed out")
        summary = stats.summary()
        self.assertEqual(summary["listings_api"]["failures"], {"TimeoutError: timed out": 2})
        self.assertEqual([p["interval_s"] for p in summary["pace"]], [4])


class UnitTimingTests(unittest.TestCase):
    def test_how_long_a_unit_stayed_up_and_how_fast_auto_apply_was(self):
        stats = run_stats.RunStats()
        stats.started_utc = "2026-10-10T11:00:00.000Z"
        uid = UNIT_5A["unitSpk"]
        stats.observe_changes(check_units.Changes(new=[UNIT_5A]), at("2026-10-10T11:02:00.400Z"),
                              qualifies=lambda u: True)
        removed = {"first_seen_utc": "2026-10-10T11:02:00Z", "last_seen_utc": "2026-10-10T11:02:30Z",
                   "data": UNIT_5A}
        stats.observe_changes(check_units.Changes(removed=[removed]), at("2026-10-10T11:03:30Z"))
        applier = FakeApplier({uid: {"attempts": [{
            "at_utc": "2026-10-10T11:02:05Z", "status": "submitted", "seconds": 2.1,
            "timeline": {"submit_pressed_utc": "2026-10-10T11:02:03.250Z"}}]}})
        entry = stats.summary(applier)["units"][uid]
        self.assertEqual(entry["listed_for_seconds"], 30)
        self.assertTrue(entry["qualifies_for_auto_apply"])
        self.assertEqual(entry["auto_apply"]["submit_seconds_after_first_seen"], 2.85)
        self.assertEqual(entry["auto_apply"]["still_listed_seconds_after_submit"], 26.75)

    def test_a_unit_still_listed_at_the_end_is_marked_so(self):
        stats = run_stats.RunStats()
        stats.observe_changes(check_units.Changes(new=[UNIT_5A]), at("2026-10-10T11:02:00Z"))
        state = {UNIT_5A["unitSpk"]: {"first_seen_utc": "2026-10-10T11:02:00Z",
                                      "last_seen_utc": "2026-10-10T13:59:58Z", "data": UNIT_5A}}
        entry = stats.summary(state=state)["units"][UNIT_5A["unitSpk"]]
        self.assertTrue(entry["still_listed_at_end"])
        self.assertEqual(entry["listed_for_seconds"], 3 * 3600 - 122)

    def test_the_file_has_nothing_personal_in_it(self):
        stats = run_stats.RunStats()
        stats.observe_responses([response(body=json.dumps({"unitModels": [UNIT_5A]}).encode())])
        stats.observe_changes(check_units.Changes(new=[UNIT_5A]), at("2026-10-10T11:02:00Z"))
        with tempfile.TemporaryDirectory() as tmp, mock.patch("builtins.print"):
            path = stats.write(FakeApplier({}), {}, directory=tmp)
            text = Path(path).read_text(encoding="utf-8")
        self.assertIn(UNIT_5A["unitSpk"], text)
        for value in PERSONAL:
            self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
