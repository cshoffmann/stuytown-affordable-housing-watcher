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
from datetime import datetime, timedelta, timezone
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


class Clock:
    """Check times: each diff() is a few seconds after the last."""

    def __init__(self):
        self.now = datetime(2026, 10, 4, 11, 55, 0)

    def tick(self, seconds=2):
        self.now += timedelta(seconds=seconds)
        return self.now.strftime("%Y-%m-%dT%H:%M:%SZ")


def diff(previous, units, clock=None, seconds=2):
    return check_units.diff_units(previous, units, clock.tick(seconds) if clock else NOW)


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
        clock = Clock()
        state, _ = diff({}, [UNIT_5A], clock)
        state, changes = diff(state, [], clock)  # API hiccup
        self.assertFalse(changes)
        state, changes = diff(state, [UNIT_5A], clock)  # back on the next check
        self.assertFalse(changes)
        record = state[UNIT_5A["unitSpk"]]
        self.assertEqual(record["missing_polls"], 0)
        self.assertNotIn("missing_since_utc", record)

    def test_a_unit_counts_as_removed_after_a_minute_gone_at_any_checking_pace(self):
        for seconds_between_checks in (2, 10, 30):
            clock = Clock()
            state, _ = diff({}, [UNIT_5A], clock)
            state, changes = diff(state, [], clock, seconds_between_checks)  # first check without it
            gone_for = 0
            while not changes.removed:
                state, changes = diff(state, [], clock, seconds_between_checks)
                gone_for += seconds_between_checks
                self.assertLessEqual(gone_for, 60 + seconds_between_checks)
            self.assertGreaterEqual(gone_for, 60)
            self.assertEqual([r["data"] for r in changes.removed], [UNIT_5A])
            self.assertEqual(state, {})

    def test_one_slow_check_alone_doesnt_remove_a_unit(self):
        clock = Clock()
        state, _ = diff({}, [UNIT_5A], clock)
        state, changes = diff(state, [], clock, seconds=120)  # a single check, after a long pause
        self.assertFalse(changes)

    def test_unit_relisted_after_removal_alerts_again(self):
        clock = Clock()
        state, _ = diff({}, [UNIT_5A], clock)
        state, _ = diff(state, [], clock)
        state, changes = diff(state, [], clock, seconds=61)
        self.assertTrue(changes.removed)
        _, changes = diff(state, [UNIT_5A, UNIT_12C], clock)
        self.assertEqual(changes.new, [UNIT_5A, UNIT_12C])

    def test_unit_repeated_in_one_response_alerts_once(self):
        _, changes = diff({}, [UNIT_5A, UNIT_5A])
        self.assertEqual(changes.new, [UNIT_5A])
        self.assertEqual(changes.listed, 1)


class ProcessSnapshotTests(unittest.TestCase):
    """The whole check -- applier hand-off, alerts, state file, events.json --
    with Pushover replaced by a recorder and the background workers inline."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for name, value in {
            "STATE_FILE": str(self.dir / "last_seen.json"),
            "EVENTS_FILE": str(self.dir / "events.json"),
        }.items():
            patcher = mock.patch.object(check_units, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = []
        patcher = mock.patch.object(check_units, "notify", side_effect=self.record_alert)
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()

    def record_alert(self, **alert):
        self.sent.append(alert)
        return True

    def events(self):
        path = Path(check_units.EVENTS_FILE)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def check(self, units, seconds=2, **kwargs):
        when = datetime.strptime(self.clock.tick(seconds), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        with mock.patch("builtins.print"):
            return check_units.process_snapshot(units, now=when, **kwargs)

    def test_new_unit_alerts_once_and_logs_metadata(self):
        for _ in range(3):
            self.check([UNIT_5A])
        self.assertEqual(len(self.sent), 1)
        alert = self.sent[0]
        self.assertEqual(alert["priority"], 2)  # no rent rule given: every new unit is an Emergency
        self.assertEqual(alert["url"], check_units.unit_url(UNIT_5A))
        [event] = self.events()
        self.assertEqual(event["event"], "new")
        self.assertEqual(event["units"][0]["apartment"], "5A")
        self.assertEqual(event["units"][0]["data"], UNIT_5A)  # full API metadata kept
        self.assertNotIn("screenshot", event)

    def test_only_qualifying_units_get_the_emergency_alert(self):
        cheap = lambda unit: unit["price"] < 1500  # noqa: E731 -- 12C is $1,450, 5A $1,873
        self.check([UNIT_5A, UNIT_12C], qualifies=cheap, apply=lambda units: [UNIT_12C])
        emergency, normal = self.sent
        self.assertEqual((emergency["priority"], normal["priority"]), (2, 0))
        self.assertIn("auto-applying now", emergency["title"])
        self.assertIn("12C", emergency["message"])
        self.assertIn("doesn't qualify", normal["title"])
        self.assertIn("5A", normal["message"])
        self.assertEqual([u["qualifies_for_auto_apply"] for u in self.events()[0]["units"]], [False, True])

    def test_a_qualifying_unit_the_applier_didnt_take_says_apply_now(self):
        self.check([UNIT_12C], qualifies=lambda u: True, apply=lambda units: [])  # e.g. applied to before
        self.assertIn("apply now", self.sent[0]["title"])
        self.assertNotIn("auto-applying", self.sent[0]["title"])

    def test_the_applier_gets_the_listings_before_any_alert_or_log(self):
        order = []
        self.notify.side_effect = lambda **alert: order.append("alert") or True
        with mock.patch.object(check_units, "record_event", lambda event: order.append("log")):
            self.check([UNIT_12C], qualifies=lambda u: True, apply=lambda units: order.append("apply"))
        self.assertEqual(order, ["apply", "alert", "log"])

    def test_a_failing_pushover_never_stops_the_check_or_the_applier(self):
        self.notify.side_effect = RuntimeError("Pushover is down")
        handed = []
        with mock.patch("background.time.sleep"):  # skip the retry waits
            changes = self.check([UNIT_12C], qualifies=lambda u: True, apply=handed.append)
        self.assertEqual(changes.new, [UNIT_12C])
        self.assertEqual(handed, [[UNIT_12C]])  # the applier got the unit
        self.assertEqual(len(self.events()), 1)  # and it was logged
        self.assertTrue(Path(check_units.STATE_FILE).exists())  # the state moved on: no re-alert loop

    def test_a_crash_in_the_applier_hand_off_never_stops_the_check(self):
        def broken(units):
            raise RuntimeError("boom")

        changes = self.check([UNIT_5A], apply=broken)
        self.assertEqual(len(changes.new), 1)
        self.assertEqual(len(self.sent), 1)

    def test_updates_and_removals_are_logged_without_alerts(self):
        self.check([UNIT_5A])
        self.check([UNIT_5A_RENT_CHANGE])
        self.check([])
        self.check([], seconds=61)
        self.assertEqual([a["priority"] for a in self.sent], [2])  # just the new-unit alert
        self.assertEqual([e["event"] for e in self.events()], ["new", "updated", "removed"])
        self.assertEqual(self.events()[1]["units"][0]["changed"]["price"], {"before": 1873, "after": 1925})
        removed = self.events()[2]["units"][0]
        # Listed on the first two checks, 2 s apart: up for 2 s, not the minute it took to confirm.
        self.assertEqual(removed["listed_for_seconds"], 2)


class AlertContentTests(unittest.TestCase):
    def test_unit_link_matches_the_sites_details_button(self):
        self.assertEqual(
            check_units.unit_url({"unitSpk": "P~NYST31~B~287~U~0T-A"}),
            "https://affordable-housing.stuytown.com/apartments/units?unitSpk=P~NYST31~B~287~U~0T-A",
        )

    def test_single_new_unit_alert_links_straight_to_the_unit(self):
        alert = check_units.qualifying_alert([UNIT_5A], auto_applying=False)
        self.assertEqual(alert["priority"], 2)
        self.assertIn("apply now", alert["title"])
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
    protocol_version = "HTTP/1.1"  # keep-alive, like the real API
    pages = []
    status = 200
    headers_to_send = {}
    connections = set()

    def do_GET(self):
        FakeApiHandler.connections.add(self.client_address)
        if FakeApiHandler.status != 200:
            body = b"slow down"
            self.send_response(FakeApiHandler.status)
        else:
            page = int(self.path.split("page=")[1].split("&")[0])
            body = json.dumps(FakeApiHandler.pages[page]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        for name, value in FakeApiHandler.headers_to_send.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchTests(unittest.TestCase):
    def serve(self, pages, status=200, headers=None):
        FakeApiHandler.pages, FakeApiHandler.status = pages, status
        FakeApiHandler.headers_to_send, FakeApiHandler.connections = headers or {}, set()
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
        client = check_units.ListingsClient()
        self.addCleanup(client.close)
        self.assertEqual(client.fetch_all(), [UNIT_5A, UNIT_12C])

    def test_an_error_response_raises_instead_of_reading_as_zero_units(self):
        self.serve([{"error": "maintenance"}])
        with self.assertRaises(ValueError):
            check_units.fetch_all_units()
        check_units._client.close()

    def test_checks_reuse_one_connection(self):
        self.serve([{"count": 1, "unitModels": [UNIT_5A], "totalCount": 1}])
        client = check_units.ListingsClient()
        self.addCleanup(client.close)
        for _ in range(5):
            self.assertEqual(client.fetch_all(), [UNIT_5A])
        self.assertEqual(len(FakeApiHandler.connections), 1)

    def test_each_response_is_kept_for_the_run_stats(self):
        self.serve([{"count": 1, "unitModels": [UNIT_5A], "totalCount": 1}], headers={"Cache-Control": "no-cache"})
        client = check_units.ListingsClient()
        self.addCleanup(client.close)
        client.fetch_all()
        client.fetch_all()
        (response,) = client.responses  # just this check's
        self.assertEqual((response["status"], response["reused_connection"]), (200, True))
        self.assertEqual(response["headers"]["cache-control"], "no-cache")
        self.assertGreater(response["bytes"], 0)

    def test_a_rate_limit_is_reported_with_the_sites_retry_after(self):
        self.serve([], status=429, headers={"Retry-After": "45"})
        client = check_units.ListingsClient()
        self.addCleanup(client.close)
        with self.assertRaises(check_units.ListingsUnavailable) as caught:
            client.fetch_all()
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (429, 45.0))


class PaceTests(unittest.TestCase):
    def test_when_the_site_asks_to_slow_down_it_backs_off_and_recovers_gradually(self):
        pace = watch_loop.Pace(normal=2, slowest=30)
        for expected in (4, 8, 16, 30, 30):
            pace.trouble(pushed_back=True)
            self.assertEqual(pace.interval, expected)
        pace.trouble(retry_after=90, pushed_back=True)  # the site's own Retry-After wins
        self.assertEqual(pace.interval, 90)
        intervals = []
        for _ in range(30):
            pace.ok()
            intervals.append(pace.interval)
        self.assertEqual(intervals[:6], [90, 90, 45, 45, 45, 22.5])  # halves after every 3 good checks
        self.assertEqual(intervals[-1], 2)

    def test_a_plain_error_is_retried_soon_and_forgotten_on_the_first_good_check(self):
        pace = watch_loop.Pace(normal=2, slowest=30, hiccup_ceiling=8)
        for expected in (4, 8, 8, 8):
            pace.trouble()
            self.assertEqual(pace.interval, expected)
        pace.ok()
        self.assertEqual(pace.interval, 2)


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
