"""
Simulated morning, on your actual phone, with FAKE listings.

Replays tests/fixtures/morning_scenario.json one check at a time through the
REAL watcher code -- the same fetch, compare, alert, events-log and screenshot
code that runs in GitHub Actions -- against a fake copy of the listings API
running on your own computer. It never touches the real data/ or screenshots/
folders or git: everything goes to tests/output/ (wiped at the start of each
run).

What your phone should get (every title starts with [TEST]):
  1. EMERGENCY  "New StuyTown affordable unit - apply now" for Apt 5A, with an
                "Open this unit to apply" link. Breaks through Do Not Disturb
                and repeats every minute until you tap Acknowledge (test alerts
                stop by themselves after 3 minutes).
  2. normal     "StuyTown listing updated" when 5A's rent changes.
  3. quiet      "StuyTown unit no longer listed" once 5A has been gone ~1 min.
  4. EMERGENCY  "2 new StuyTown affordable units" when 5A is re-listed and 12C
                appears.
Every other check sends nothing -- that's the duplicate protection working.

Run from the repo folder:
    python tests/simulate_morning.py      (no keys set = dry run: alerts are printed, not sent)

    PowerShell:
        $env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python tests/simulate_morning.py
    Mac/Linux:
        PUSHOVER_TOKEN=your-app-token PUSHOVER_USER=your-user-key python3 tests/simulate_morning.py

Options:
    --interval 10     seconds between checks (default 5; 10 = the real pace)
    --no-screenshots  skip the screenshots (they need Playwright)
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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)  # so every output path below starts with tests/output/

import auto_apply  # noqa: E402
import check_units  # noqa: E402
import watch_loop  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"
OUTPUT_DIR = "tests/output"


class FakeListingsApi(http.server.BaseHTTPRequestHandler):
    """Stands in for units.stuytown.com/api/ah-units, answering with whatever
    the current step of the scenario says is listed."""

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


def fake_screenshot_function():
    try:
        import screenshot
    except ImportError:
        print("Screenshots: off -- Playwright isn't installed "
              "(pip install -r requirements.txt, then: playwright install chromium)")
        return None

    def take(path: str) -> bool:
        # The real listings page, but with its listings request answered by
        # the fake data -- so you see the fake unit exactly as the site
        # would show it.
        screenshot.take(path, fake_api_payload=FakeListingsApi.payload)
        return True

    print("Screenshots: on (the real listings page, showing the fake units)")
    return take


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=5, help="seconds between checks (real pace: 10)")
    parser.add_argument("--no-screenshots", action="store_true", help="skip the screenshots")
    args = parser.parse_args()

    units_by_name = json.loads((FIXTURES / "fake_units.json").read_text(encoding="utf-8"))
    steps = json.loads((FIXTURES / "morning_scenario.json").read_text(encoding="utf-8"))["steps"]

    # Fresh, isolated output every run -- never the real data/ or screenshots/.
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    check_units.STATE_FILE = f"{OUTPUT_DIR}/last_seen.json"
    check_units.EVENTS_FILE = f"{OUTPUT_DIR}/events.json"
    check_units.SCREENSHOT_DIR = f"{OUTPUT_DIR}/screenshots"
    check_units.TITLE_PREFIX = "[TEST] "
    check_units.EMERGENCY_EXPIRE_SECONDS = 180  # test emergencies stop repeating after 3 minutes
    os.environ.pop("COMMIT_RESULTS", None)  # a test never commits or pushes
    auto_apply.ENABLED = False  # the fake units' pages don't exist; tests/test_auto_apply.py covers applying

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeListingsApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    check_units.BASE_URL = f"http://127.0.0.1:{server.server_port}/api/ah-units"

    print("Simulated morning: fake listings, real watcher code")
    if check_units.PUSHOVER_TOKEN and check_units.PUSHOVER_USER:
        print("Pushover: keys found -- alerts WILL go to your phone (titles start with [TEST])")
    else:
        print("Pushover: no keys set -- DRY RUN, alerts are printed instead of sent")
    take_screenshot = None if args.no_screenshots else fake_screenshot_function()
    print(f"Output: {OUTPUT_DIR}/   Checks every {args.interval:g}s ({len(steps)} checks)")

    mismatches = 0
    alerts = Counter()
    try:
        for number, step in enumerate(steps, 1):
            units = [units_by_name[name] for name in step["units"]]
            FakeListingsApi.payload = {"count": len(units), "unitModels": units, "totalCount": len(units)}

            print(f"\nCheck {number}/{len(steps)}: {step['what']}")
            changes = watch_loop.poll_once(take_screenshot=take_screenshot, label=step["time"])
            alerts.update(changes.kinds())

            actual = "+".join(changes.kinds()) or "nothing"
            if actual == step["expect"]:
                print(f"   OK - expected: {step['expect']}")
            else:
                mismatches += 1
                print(f"   MISMATCH - expected {step['expect']}, got {actual}")

            if number < len(steps):
                time.sleep(args.interval)
    finally:
        server.shutdown()
        server.server_close()

    print("\n" + "=" * 60)
    print(f"Alerts: {alerts['new']} emergency (new units), {alerts['updated']} updated, "
          f"{alerts['removed']} removed -- every other check stayed silent.")
    print(f"Events log:  {check_units.EVENTS_FILE}")
    if take_screenshot:
        print(f"Screenshots: {check_units.SCREENSHOT_DIR}/")
    if mismatches:
        print(f"{mismatches} check(s) did NOT behave as expected -- see MISMATCH above.")
        return 1
    print("All checks behaved as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
