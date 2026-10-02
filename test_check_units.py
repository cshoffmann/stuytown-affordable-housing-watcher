"""
<<<<<<< HEAD
Local test harness for check_units.py -- run this ONCE to confirm the
full pipeline (detection, events.json, Pushover) is wired correctly.
If you just want to repeatedly trigger a notification to tune your
phone's sound/DND settings, use test_notification.py instead -- this
script writes real-looking state and event-log entries every time it
runs, which you don't want to do repeatedly.

Fakes exactly two things -- the live API response, and the current time --
and then runs the REAL main(), unit_id(), load_last_seen(), save_last_seen(),
and notify() unmodified. That means a successful run here proves the
detection logic, the events.json entry it writes, and the actual Pushover
push to your phone all work, not just that the code compiles.
=======
Full-pipeline local test -- simulates the entire GitHub Actions workflow,
not just check_units.py's internals: runs the real detection logic, and
if a "new" unit is found, actually takes a real screenshot too, exactly
like the workflow's conditional screenshot step would.

Run this ONCE per test (not repeatedly) -- for a quick repeatable
notification-only test while tuning your phone, use test_notification.py
instead, which has no file side effects.

Everything this writes goes into test_output/ -- a completely separate
folder from the real data/ and screenshots/ directories check_units.py
and screenshot.py use in production. That's deliberate: this test can
never collide with or overwrite real state, so there's nothing to clean
up or worry about forgetting to delete before the real schedule runs.
(test_output/ is already in .gitignore.)

Requires Playwright for the screenshot part:
    pip install playwright
    playwright install chromium
>>>>>>> continuous-watch-loop

Run this from the same folder as check_units.py:

    Windows (PowerShell):
        $env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python test_check_units.py
<<<<<<< HEAD

    Mac/Linux:
        PUSHOVER_TOKEN=your-app-token PUSHOVER_USER=your-user-key python3 test_check_units.py

Watch your phone -- you should get an Emergency-priority push (bypassing
silent/DND) within a couple seconds of running this.

This is a throwaway test: it creates/overwrites a local last_seen.json and
events.json. Delete both afterward (or just don't commit them) so the real
deployed workflow starts from an honest, real baseline instead of these
fake ones.
=======

    Windows (cmd):
        set PUSHOVER_TOKEN=your-app-token
        set PUSHOVER_USER=your-user-key
        python test_check_units.py

    Mac/Linux:
        PUSHOVER_TOKEN=your-app-token PUSHOVER_USER=your-user-key python3 test_check_units.py
>>>>>>> continuous-watch-loop
"""

import os
from pathlib import Path

import check_units
import screenshot

TEST_DIR = "test_output"
os.makedirs(TEST_DIR, exist_ok=True)

# Redirect every file check_units.py and screenshot.py would normally
# write, into the isolated test folder -- this is the whole reason the
# real data/ and screenshots/ folders are never at risk from testing.
check_units.STATE_FILE = os.path.join(TEST_DIR, "last_seen.json")
check_units.EVENTS_FILE = os.path.join(TEST_DIR, "events.json")
screenshot.OUTPUT_DIR = Path(TEST_DIR) / "screenshots"

# Capture check_units.py's outputs the same way GitHub Actions does
# (steps.check.outputs.*), by pointing GITHUB_OUTPUT at a local file and
# reading it back afterward -- so this script makes the same
# "run check, then conditionally screenshot" decision the real workflow
# YAML makes, using the real mechanism, not a guess.
output_file = os.path.join(TEST_DIR, "github_output.txt")
if os.path.exists(output_file):
    os.remove(output_file)
os.environ["GITHUB_OUTPUT"] = output_file

# Pretend it's within the 7-10am ET window, regardless of when you run this.
check_units.within_window = lambda: True

# Pretend the live API returned one obviously-fake unit.
FAKE_UNIT = {"unitSpk": "TEST-0001", "unitNumber": "TEST 1A - delete me", "bedrooms": 2}
check_units.fetch_all_units = lambda: [FAKE_UNIT]

# Seed an EMPTY (but present) state file in the test folder, so this reads
# as a change from "nothing" to "one unit" -- not as the very first run
# ever, which intentionally never notifies.
with open(check_units.STATE_FILE, "w") as f:
    f.write('{"unit_ids": []}')

print("Running the real check_units.main() with faked data and time...")
check_units.main()
print("Done. If PUSHOVER_TOKEN/PUSHOVER_USER were set correctly, your phone")
print("should have just gotten an Emergency-priority alert.")
<<<<<<< HEAD
=======

# Parse the captured outputs, the same shape steps.check.outputs has in
# the real workflow.
outputs = {}
with open(output_file) as f:
    for line in f:
        if "=" in line:
            key, value = line.strip().split("=", 1)
            outputs[key] = value
print(f"\nCaptured outputs: {outputs}")

if outputs.get("new_unit_found") == "true":
    print("\nnew_unit_found=true -- taking a real screenshot, same as the real workflow step would...")
    os.environ["SCREENSHOT_FILENAME"] = outputs["screenshot_filename"]
    screenshot.main()
else:
    print("\nnew_unit_found=false -- the real workflow would skip the screenshot step here too.")
>>>>>>> continuous-watch-loop

if os.path.exists(check_units.EVENTS_FILE):
    with open(check_units.EVENTS_FILE) as f:
        print(f"\n{check_units.EVENTS_FILE} now contains:")
        print(f.read())

print(f"\nEverything from this run is under {TEST_DIR}/ -- the real data/ and")
print("screenshots/ folders were never touched.")
