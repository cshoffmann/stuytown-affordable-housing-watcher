"""
Automated checks for the checker loop and the background workers -- no real
site, no phone, no git.

Run from the repo folder:
    python -m unittest discover -s tests -v
"""

import http.server
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import apply_recorder  # noqa: E402
import background  # noqa: E402
import check_units  # noqa: E402
import floorplans  # noqa: E402
import run_stats  # noqa: E402
import summary  # noqa: E402
import watch_loop  # noqa: E402

FAKE_UNITS = json.loads((REPO_ROOT / "tests" / "fixtures" / "fake_units.json").read_text(encoding="utf-8"))


class Api(http.server.BaseHTTPRequestHandler):
    """A fake listings API: answers 429 to the first `refuse` requests."""

    protocol_version = "HTTP/1.1"
    units, refuse, requests = [], 0, 0

    def do_GET(self):
        Api.requests += 1
        if Api.requests <= Api.refuse:
            body, status = b"slow down", 429
        else:
            body, status = json.dumps({"unitModels": Api.units, "totalCount": len(Api.units)}).encode(), 200
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeApplier:
    applications = {}

    def __init__(self):
        self.handed = []

    def observations(self):
        return {"workers": 0}

    def dispatch(self, units):
        self.handed.append(len(units))
        return []

    def busy(self):
        return False

    def stop(self, timeout=0):
        pass


class CheckerTests(unittest.TestCase):
    def setUp(self):
        Api.units, Api.refuse, Api.requests = [FAKE_UNITS["5A"]], 0, 0
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Api)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target, name, value in [
            (check_units, "BASE_URL", f"http://127.0.0.1:{server.server_port}/api/ah-units"),
            (check_units, "STATE_FILE", f"{tmp.name}/last_seen.json"),
            (check_units, "EVENTS_FILE", f"{tmp.name}/events.json"),
            (check_units, "notify", lambda **alert: True),
            (run_stats, "RUN_STATS_DIR", f"{tmp.name}/run_stats"),
            (summary, "SUMMARY_FILE", f"{tmp.name}/summary.md"),
            # The wrap-up's map and floorplans: a temp folder, and no network.
            (floorplans, "FLOORPLANS_DIR", f"{tmp.name}/floorplans"),
            (apply_recorder, "RUNS_DIR", f"{tmp.name}/apply_runs"),
            (floorplans, "fetch_floorplan", mock.Mock(side_effect=OSError("offline"))),
            (watch_loop, "DAILY_LOG_FILE", f"{tmp.name}/daily_log.md"),
        ]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_checker(self, seconds, normal=0.05, slowest=0.4):
        inline = background.Worker("inline", inline=True)
        applier = FakeApplier()
        watcher = watch_loop.Watcher(applier, inline, inline, pace=watch_loop.Pace(normal, slowest, slowest))
        out = io.StringIO()
        with redirect_stdout(out):
            watcher.run_until(watch_loop.now_et() + timedelta(seconds=seconds))
        return watcher, applier, out.getvalue().splitlines()

    def test_every_check_hands_the_listings_to_the_applier_and_only_changes_are_logged(self):
        watcher, applier, lines = self.run_checker(0.6)
        self.assertGreaterEqual(watcher.checks, 6)
        self.assertEqual(len(applier.handed), watcher.checks)  # every single check
        changes = [line for line in lines if " listed - " in line]
        self.assertEqual(len(changes), 1)  # 5A appearing; the other checks print nothing

    def test_it_backs_off_when_the_site_refuses_and_speeds_up_again(self):
        Api.refuse = 3
        watcher, applier, lines = self.run_checker(3.5)
        self.assertEqual(watcher.failures, 3)
        self.assertEqual(watcher.pace.interval, 0.05)  # back to normal
        self.assertTrue(any("next checks every 0.4s" in line for line in lines))
        self.assertTrue(any("reachable again" in line for line in lines))
        self.assertGreater(len(applier.handed), 3)

    def test_the_mornings_stats_are_written_at_the_end(self):
        watcher, _, _ = self.run_checker(0.4)
        with mock.patch.object(watch_loop, "commit_and_push", lambda message: None), mock.patch("builtins.print"):
            watcher.finish()
        (written,) = Path(run_stats.RUN_STATS_DIR).iterdir()
        stats = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(stats["listings_api"]["requests"], watcher.checks)
        self.assertEqual(stats["listings_api"]["connections"], {"kept-open connection": watcher.checks - 1,
                                                                "new connection": 1})
        self.assertTrue(stats["units"][FAKE_UNITS["5A"]["unitSpk"]]["still_listed_at_end"])
        self.assertEqual(stats["applier"], {"workers": 0})

    def test_one_commit_a_day_says_what_was_listed(self):
        inline = background.Worker("inline", inline=True)
        commits = []
        watcher = watch_loop.Watcher(FakeApplier(), inline, inline)
        at_815 = datetime(2026, 11, 6, 8, 15, tzinfo=watch_loop._eastern())
        with mock.patch.object(watch_loop, "commit_and_push", commits.append), mock.patch("builtins.print"), \
                mock.patch.object(watch_loop, "now_et", return_value=at_815):
            watcher.check([FAKE_UNITS["5A"]])
            watcher.check([FAKE_UNITS["5A"]])
            watcher.check([FAKE_UNITS["5A"], FAKE_UNITS["12C"]])
            watcher._housekeeping()
            self.assertEqual(commits, [])  # nothing during the morning
            watcher.checks = 3
            watcher.finish()
        self.assertEqual(len(commits), 1)
        self.assertTrue(commits[0].startswith("Fri Nov 6: 2 new listings (3 checks)\n\n08:15 Apt 5A, 287 Avenue C, 2 bed, $1,873"),
                        commits[0])
        log = Path(watch_loop.DAILY_LOG_FILE).read_text()
        self.assertIn("| Fri 2026-11-06 | 2 | 08:15 Apt 5A, 287 Avenue C, 2 bed, $1,873; 08:15 Apt 12C", log)
        self.assertIn("08:15 Apt 12C", commits[0])
        quiet = watch_loop.Watcher(FakeApplier(), inline, inline)
        quiet.checks, quiet.failures = 6141, 2
        with mock.patch.object(watch_loop, "now_et", return_value=at_815):
            self.assertEqual(quiet.day_message(), "Fri Nov 6: no new listings (6,141 checks, 2 failed)")


class CollectingTests(unittest.TestCase):
    def test_with_auto_apply_off_it_checks_every_10_s_and_never_bursts(self):
        inline = background.Worker("inline", inline=True)
        watcher = watch_loop.Watcher(None, inline, inline, pace=watch_loop.Pace(watch_loop.COLLECT_POLL_SECONDS),
                                     bursts=False)
        at_quarter = datetime(2026, 10, 13, 7, 14, 59, tzinfo=watch_loop._eastern())
        with mock.patch.object(watch_loop, "now_et", return_value=at_quarter), \
                mock.patch.object(watch_loop.time, "monotonic", return_value=1000.0):
            self.assertEqual(watcher._next_check(1000.0), 1010.0)


class BurstTests(unittest.TestCase):
    """Units are posted on the quarter hour: the checks bunch up around it."""

    def at(self, hour, minute, second, micro=0):
        return datetime(2026, 10, 13, hour, minute, second, micro, tzinfo=watch_loop._eastern())

    def next_check(self, now, previous, interval=None):
        inline = background.Worker("inline", inline=True)
        watcher = watch_loop.Watcher(FakeApplier(), inline, inline)
        if interval:
            watcher.pace.interval = interval
        with mock.patch.object(watch_loop, "now_et", return_value=now), \
                mock.patch.object(watch_loop.time, "monotonic", return_value=1000.0):
            return watcher._next_check(previous)

    def test_the_burst_runs_from_3s_before_to_15s_after_each_quarter_hour(self):
        self.assertIsNone(watch_loop.burst_slot(self.at(7, 14, 56)))
        self.assertEqual(watch_loop.burst_slot(self.at(7, 14, 57)), self.at(7, 15, 0))
        self.assertEqual(watch_loop.burst_slot(self.at(7, 15, 14, 900000)), self.at(7, 15, 0))
        self.assertIsNone(watch_loop.burst_slot(self.at(7, 15, 15)))
        self.assertEqual(watch_loop.burst_slot(self.at(7, 59, 58)), self.at(8, 0, 0))
        self.assertEqual(watch_loop.next_burst_start(self.at(7, 15, 20)), self.at(7, 29, 57))

    def test_a_check_lands_exactly_on_the_quarter_hour(self):
        self.assertAlmostEqual(self.next_check(self.at(7, 14, 59, 900000), previous=999.0), 1000.1)
        self.assertAlmostEqual(self.next_check(self.at(7, 15, 0, 30000), previous=999.0), 1000.22)

    def test_between_bursts_the_normal_beat_but_never_past_the_next_burst(self):
        self.assertAlmostEqual(self.next_check(self.at(7, 10, 0), previous=999.0), 1001.0)
        self.assertAlmostEqual(self.next_check(self.at(7, 14, 56), previous=999.5), 1001.0)  # burst at 7:14:57

    def test_no_burst_while_the_site_is_asking_to_slow_down(self):
        self.assertAlmostEqual(self.next_check(self.at(7, 15, 0), previous=999.0, interval=8), 1007.0)


class BackgroundWorkerTests(unittest.TestCase):
    def test_a_failing_job_is_dropped_and_the_next_one_still_runs(self):
        worker = background.Worker("test")
        done = []

        def broken():
            raise RuntimeError("boom")

        with mock.patch("builtins.print"), mock.patch("traceback.print_exc"):
            worker.submit(broken)
            worker.submit(done.append, "next")
            self.assertTrue(worker.drain(5))
        self.assertEqual(done, ["next"])
        self.assertEqual(worker.failures, 1)

    def test_it_waits_while_an_application_is_running(self):
        applying = threading.Event()
        applying.set()
        worker = background.Worker("logger", yield_to=applying.is_set)
        done = []
        worker.submit(done.append, "logged")
        time.sleep(0.3)
        self.assertEqual(done, [])  # held back
        applying.clear()
        self.assertTrue(worker.drain(5))
        self.assertEqual(done, ["logged"])

    def test_alerts_are_retried_without_blocking_the_caller(self):
        attempts = []

        def flaky_notify(**alert):
            attempts.append(alert["title"])
            if len(attempts) < 3:
                raise OSError("network down")
            return True

        worker = background.Worker("notifier")
        started = time.monotonic()
        worker.submit(background.notify_with_retries, flaky_notify, {"title": "hi"}, 4, 0.05)
        self.assertLess(time.monotonic() - started, 0.05)  # submit returns at once
        self.assertTrue(worker.drain(5))
        self.assertEqual(attempts, ["hi", "hi", "hi"])

    def test_a_rejected_alert_isnt_retried(self):
        calls = []

        def rejecting_notify(**alert):
            calls.append(1)
            raise check_units.PushoverRejected("bad token")

        with mock.patch("builtins.print"):
            self.assertFalse(background.notify_with_retries(rejecting_notify, {"title": "hi"}, first_wait=0))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
