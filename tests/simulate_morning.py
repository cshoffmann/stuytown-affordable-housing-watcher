"""
Simulated morning, on your actual phone, with FAKE listings.

Replays tests/fixtures/morning_scenario.json one check at a time through the
REAL watcher code -- the same fetch (over a kept-open connection), compare,
alert and events-log code that runs in GitHub Actions -- against a fake copy
of the listings API running on your own computer. It never touches the real
data/ folder or git: everything goes to tests/output/ (wiped at the start of
each run). Auto-apply stays off (the fake units have no real pages;
tests/test_auto_apply.py covers applying), but the rent limit is set to
$1,500 so both kinds of new-unit alert show up.

What your phone should get (every title starts with [TEST]):
  1. normal     "New StuyTown unit (doesn't qualify)" when 5A ($1,873) is posted.
  2. EMERGENCY  "Qualifying StuyTown unit - apply now" when 12C ($1,450) is
                posted. Breaks through Do Not Disturb and repeats every minute
                until you tap Acknowledge (test alerts stop after 3 minutes).
  3. normal     "New StuyTown unit (doesn't qualify)" for 5A, re-listed at the
                same time.
5A's rent change and its removal are logged in events.json, with no alert.

Run from the repo folder:
    python tests/simulate_morning.py      (no keys set = dry run: alerts are printed, not sent)

    PowerShell:
        $env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python tests/simulate_morning.py
    Mac/Linux:
        PUSHOVER_TOKEN=your-app-token PUSHOVER_USER=your-user-key python3 tests/simulate_morning.py

Options:
    --interval 2      seconds between checks (default 0.5; 2 = the real pace)
"""

import argparse
import http.server
import json
import os
import shutil
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)  # so every output path below starts with tests/output/

import auto_apply  # noqa: E402
import background  # noqa: E402
import check_units  # noqa: E402
import watch_loop  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"
OUTPUT_DIR = "tests/output"
TEST_RENT_LIMIT = 1500


class FakeListingsApi(http.server.BaseHTTPRequestHandler):
    """Stands in for units.stuytown.com/api/ah-units, answering with whatever
    the current step of the scenario says is listed."""

    protocol_version = "HTTP/1.1"  # keep-alive, like the real API
    payload = {"count": 0, "unitModels": [], "totalCount": 0}

    def do_GET(self):
        body = json.dumps(FakeListingsApi.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the output readable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between checks (real pace: 2)")
    args = parser.parse_args()

    units_by_name = json.loads((FIXTURES / "fake_units.json").read_text(encoding="utf-8"))
    steps = json.loads((FIXTURES / "morning_scenario.json").read_text(encoding="utf-8"))["steps"]

    # Fresh, isolated output every run -- never the real data/ folder.
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    check_units.STATE_FILE = f"{OUTPUT_DIR}/last_seen.json"
    check_units.EVENTS_FILE = f"{OUTPUT_DIR}/events.json"
    check_units.TITLE_PREFIX = "[TEST] "
    check_units.EMERGENCY_EXPIRE_SECONDS = 180  # test emergencies stop repeating after 3 minutes
    os.environ.pop("COMMIT_RESULTS", None)  # a test never commits or pushes
    auto_apply.ENABLED = False
    auto_apply.MAX_RENT = TEST_RENT_LIMIT

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeListingsApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    check_units.BASE_URL = f"http://127.0.0.1:{server.server_port}/api/ah-units"

    print("Simulated morning: fake listings, real watcher code")
    if check_units.PUSHOVER_TOKEN and check_units.PUSHOVER_USER:
        print("Pushover: keys found -- alerts WILL go to your phone (titles start with [TEST])")
    else:
        print("Pushover: no keys set -- DRY RUN, alerts are printed instead of sent")
    print(f"Rent limit for this test: ${TEST_RENT_LIMIT:,}   Output: {OUTPUT_DIR}/   "
          f"Checks every {args.interval:g}s ({len(steps)} checks)")

    # Inline workers: each alert and log entry happens right away, so the
    # output reads in order. The real watcher runs them in the background.
    inline = background.Worker("inline", inline=True)
    watcher = watch_loop.Watcher(None, notifier=inline, logger=inline)
    client = check_units.ListingsClient()
    sent = Counter()
    real_notify = check_units.notify

    def counting_notify(**alert):
        sent["emergency" if alert.get("priority") == 2 else "normal"] += 1
        return real_notify(**alert)

    check_units.notify = counting_notify
    mismatches = 0
    try:
        for number, step in enumerate(steps, 1):
            units = [units_by_name[name] for name in step["units"]]
            FakeListingsApi.payload = {"count": len(units), "unitModels": units, "totalCount": len(units)}
            print(f"\nCheck {number}/{len(steps)}: {step['what']}")
            when = datetime.strptime(f"2026-10-06T{step['time']}", "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            changes = watcher.check(client.fetch_all(), now=when, label=step["time"])
            actual = "+".join(changes.kinds()) or "nothing"
            if actual == step["expect"]:
                print(f"   OK - expected: {step['expect']}")
            else:
                mismatches += 1
                print(f"   MISMATCH - expected {step['expect']}, got {actual}")
            if number < len(steps):
                time.sleep(args.interval)
    finally:
        check_units.notify = real_notify
        client.close()
        server.shutdown()
        server.server_close()

    print("\n" + "=" * 60)
    print(f"Alerts: {sent['emergency']} emergency (qualifying units), {sent['normal']} normal "
          "-- updates and removals are logged, not alerted.")
    print(f"Events log:  {check_units.EVENTS_FILE}")
    if mismatches or (sent["emergency"], sent["normal"]) != (1, 2):
        print(f"{mismatches} check(s) did NOT behave as expected, or the alerts weren't 1 emergency + 2 normal.")
        return 1
    print("All checks behaved as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
