"""
Automated checks for the alert/state logic -- no network, no phone, no git.
Uses the fake units in tests/fixtures/fake_units.json.

Run from the repo folder:
    python -m unittest discover -s tests -v
"""

import http.server
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import check_units  # noqa: E402
import watch_loop  # noqa: E402

FAKE_UNITS = json.loads((REPO_ROOT / "tests" / "fixtures" / "fake_units.json").read_text(encoding="utf-8"))
UNIT_5A = FAKE_UNITS["5A"]
UNIT_5A_RENT_CHANGE = FAKE_UNITS["5A_rent_change"]
UNIT_12C = FAKE_UNITS["12C"]
NOW = "2026-10-04T11:55:00Z"


def diff(previous, units):
    return check_units.diff_units(previous, units, NOW)


class StateLogicTests(unittest.TestCase):
    def test_units_already_listed_on_the_first_check_alert(self):
        state, changes = diff({}, [UNIT_5A])
        self.assertEqual(changes.new, [UNIT_5A])
        self.assertIn(UNIT_5A["unitSpk"], state)

    def test_same_listing_on_later_checks_never_alerts_again(self):
        state, _ = diff({}, [UNIT_5A])
        for _ in range(10):
            state, changes = diff(state, [UNIT_5A])
            self.assertFalse(changes)

    def test_only_the_newly_posted_unit_alerts(self):
        state, _ = diff({}, [UNIT_5A])
        _, changes = diff(state, [UNIT_5A, UNIT_12C])
        self.assertEqual(changes.new, [UNIT_12C])
        self.assertEqual(changes.updated, [])

    def test_changed_details_are_one_update_not_a_new_unit(self):
        state, _ = diff({}, [UNIT_5A])
        state, changes = diff(state, [UNIT_5A_RENT_CHANGE])
        self.assertEqual(changes.new, [])
        self.assertEqual([c["fields"] for c in changes.updated], [["price", "unitRates"]])
        # The new data is remembered, so the next check is quiet again.
        _, changes = diff(state, [UNIT_5A_RENT_CHANGE])
        self.assertFalse(changes)

    def test_reordered_keys_and_ignored_fields_are_not_updates(self):
        state, _ = diff({}, [UNIT_5A])
        reordered = dict(reversed(list(UNIT_5A.items())))
        reordered["version"] = 99
        _, changes = diff(state, [reordered])
        self.assertFalse(changes)

    def test_unit_missing_from_one_response_is_not_removed_and_does_not_realert(self):
        state, _ = diff({}, [UNIT_5A])
        state, changes = diff(state, [])  # API hiccup
        self.assertFalse(changes)
        state, changes = diff(state, [UNIT_5A])  # back on the next check
        self.assertFalse(changes)
        self.assertEqual(state[UNIT_5A["unitSpk"]]["missing_polls"], 0)

    def test_unit_gone_for_the_confirm_window_is_removed(self):
        state, _ = diff({}, [UNIT_5A])
        for _ in range(check_units.REMOVAL_CONFIRM_POLLS - 1):
            state, changes = diff(state, [])
            self.assertFalse(changes)
        state, changes = diff(state, [])
        self.assertEqual([r["data"] for r in changes.removed], [UNIT_5A])
        self.assertEqual(state, {})

    def test_unit_relisted_after_removal_alerts_again(self):
        state, _ = diff({}, [UNIT_5A])
        for _ in range(check_units.REMOVAL_CONFIRM_POLLS):
            state, _ = diff(state, [])
        _, changes = diff(state, [UNIT_5A, UNIT_12C])
        self.assertEqual(changes.new, [UNIT_5A, UNIT_12C])

    def test_unit_repeated_in_one_response_alerts_once(self):
        _, changes = diff({}, [UNIT_5A, UNIT_5A])
        self.assertEqual(changes.new, [UNIT_5A])
        self.assertEqual(changes.listed, 1)


class ProcessSnapshotTests(unittest.TestCase):
    """The whole check -- alert, state file, events.json, screenshot -- with
    Pushover replaced by a recorder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for name, value in {
            "STATE_FILE": str(self.dir / "last_seen.json"),
            "EVENTS_FILE": str(self.dir / "events.json"),
            "SCREENSHOT_DIR": str(self.dir / "screenshots"),
        }.items():
            patcher = mock.patch.object(check_units, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = []
        patcher = mock.patch.object(check_units, "notify", side_effect=self.record_alert)
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)
        check_units._update_alerts_sent.clear()  # each test is its own "run"

    def record_alert(self, **alert):
        self.sent.append(alert)
        return True

    def events(self):
        path = Path(check_units.EVENTS_FILE)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def check(self, units, **kwargs):
        with mock.patch("builtins.print"):
            return check_units.process_snapshot(units, **kwargs)

    def test_new_unit_alerts_once_and_logs_metadata_and_screenshot(self):
        shots = []

        def fake_screenshot(path):
            shots.append(path)
            return True

        for _ in range(3):
            self.check([UNIT_5A], take_screenshot=fake_screenshot)

        self.assertEqual(len(self.sent), 1)
        alert = self.sent[0]
        self.assertEqual(alert["priority"], 2)
        self.assertEqual(alert["url"], check_units.unit_url(UNIT_5A))
        self.assertEqual(len(shots), 1)

        [event] = self.events()
        self.assertEqual(event["event"], "new")
        self.assertEqual(event["screenshot"], shots[0])
        self.assertEqual(event["units"][0]["apartment"], "5A")
        self.assertEqual(event["units"][0]["data"], UNIT_5A)  # full API metadata kept

    def test_a_failed_alert_is_retried_on_the_next_check(self):
        self.notify.side_effect = [RuntimeError("Pushover is down"), True]
        with self.assertRaises(RuntimeError):
            self.check([UNIT_5A])
        self.assertEqual(self.events(), [])  # state not advanced, nothing logged
        self.check([UNIT_5A])
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(len(self.events()), 1)

    def test_updates_and_removals_send_lower_priority_alerts(self):
        self.check([UNIT_5A])
        self.check([UNIT_5A_RENT_CHANGE])
        for _ in range(check_units.REMOVAL_CONFIRM_POLLS):
            self.check([])
        self.assertEqual([a["priority"] for a in self.sent], [2, 0, -1])
        self.assertEqual([e["event"] for e in self.events()], ["new", "updated", "removed"])
        self.assertEqual(self.events()[1]["units"][0]["changed"]["price"], {"before": 1873, "after": 1925})

    def test_a_unit_whose_data_keeps_changing_stops_alerting(self):
        self.check([UNIT_5A])
        for _ in range(5):  # flip-flops on every response
            self.check([UNIT_5A_RENT_CHANGE])
            self.check([UNIT_5A])
        update_alerts = [a for a in self.sent if a["priority"] == 0]
        self.assertEqual(len(update_alerts), check_units.MAX_UPDATE_ALERTS_PER_UNIT)
        self.assertEqual(json.loads(Path(check_units.STATE_FILE).read_text())["units"]
                         [UNIT_5A["unitSpk"]]["data"], UNIT_5A)  # state still tracks the latest data

    def test_screenshot_failure_still_logs_the_event(self):
        def broken_screenshot(path):
            raise RuntimeError("browser crashed")

        self.check([UNIT_5A], take_screenshot=broken_screenshot)
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self.events()[0]["screenshot"])


class AlertContentTests(unittest.TestCase):
    def test_unit_link_matches_the_sites_details_button(self):
        self.assertEqual(
            check_units.unit_url({"unitSpk": "P~NYST31~B~287~U~0T-A"}),
            "https://affordable-housing.stuytown.com/apartments/units?unitSpk=P~NYST31~B~287~U~0T-A",
        )

    def test_single_new_unit_alert_links_straight_to_the_unit(self):
        alert = check_units.new_units_alert([UNIT_5A])
        self.assertEqual(alert["priority"], 2)
        self.assertEqual(alert["url"], check_units.unit_url(UNIT_5A))
        self.assertIn("Apt 5A, 287 Avenue C", alert["message"])
        self.assertIn("$1,873/mo", alert["message"])
        self.assertIn(f'href="{check_units.unit_url(UNIT_5A)}"', alert["message"])

    def test_many_new_units_still_fit_pushovers_limit(self):
        units = [dict(UNIT_5A, unitSpk=f"P~TEST~U~{i}", name=f"{i}A") for i in range(40)]
        alert = check_units.new_units_alert(units)
        self.assertLessEqual(len(alert["message"]), check_units.PUSHOVER_MESSAGE_LIMIT)
        self.assertIn("more on the listings page", alert["message"])
        self.assertEqual(alert["url"], check_units.LISTINGS_URL)

    def test_studio_label(self):
        self.assertTrue(check_units.unit_details(UNIT_12C).startswith("Studio / 1 bath, $1,450/mo"))


class FakeApiHandler(http.server.BaseHTTPRequestHandler):
    pages = []

    def do_GET(self):
        page = int(self.path.split("page=")[1].split("&")[0])
        body = json.dumps(FakeApiHandler.pages[page]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchTests(unittest.TestCase):
    def serve(self, pages):
        FakeApiHandler.pages = pages
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeApiHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)  # cleanups run last-in-first-out: shutdown, then close
        patcher = mock.patch.object(check_units, "BASE_URL", f"http://127.0.0.1:{server.server_port}/api/ah-units")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_every_page(self):
        self.serve([
            {"count": 1, "unitModels": [UNIT_5A], "totalCount": 2},
            {"count": 1, "unitModels": [UNIT_12C], "totalCount": 2},
        ])
        self.assertEqual(check_units.fetch_all_units(), [UNIT_5A, UNIT_12C])

    def test_an_error_response_raises_instead_of_reading_as_zero_units(self):
        self.serve([{"error": "maintenance"}])
        with self.assertRaises(ValueError):
            check_units.fetch_all_units()


class ScheduleTests(unittest.TestCase):
    def test_what_a_run_does_depending_on_when_it_starts(self):
        day = datetime(2026, 10, 4)
        self.assertEqual(watch_loop.plan(day.replace(hour=5, minute=30))[0], "too_early")
        self.assertEqual(watch_loop.plan(day.replace(hour=6, minute=13))[0], "watch")  # waits for 7:00
        self.assertEqual(watch_loop.plan(day.replace(hour=8, minute=40))[0], "watch")  # late start
        self.assertEqual(watch_loop.plan(day.replace(hour=10, minute=0))[0], "done")
        _, start, end = watch_loop.plan(day.replace(hour=6, minute=13))
        self.assertEqual((start.hour, end.hour), (7, 10))


if __name__ == "__main__":
    unittest.main()
