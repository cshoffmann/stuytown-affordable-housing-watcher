"""
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

Run this from the same folder as check_units.py:

    Windows (PowerShell):
        $env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python test_check_units.py

    Mac/Linux:
        PUSHOVER_TOKEN=your-app-token PUSHOVER_USER=your-user-key python3 test_check_units.py

Watch your phone -- you should get an Emergency-priority push (bypassing
silent/DND) within a couple seconds of running this.

This is a throwaway test: it creates/overwrites a local last_seen.json and
events.json. Delete both afterward (or just don't commit them) so the real
deployed workflow starts from an honest, real baseline instead of these
fake ones.
"""

import os

import check_units

# Pretend it's within the 7-10am ET window, regardless of when you run this.
check_units.within_window = lambda: True

# Pretend the live API returned one obviously-fake unit.
FAKE_UNIT = {"unitSpk": "TEST-0001", "unitNumber": "TEST 1A - delete me", "bedrooms": 2}
check_units.fetch_all_units = lambda: [FAKE_UNIT]

# Seed an EMPTY (but present) state file, so this reads as a change from
# "nothing" to "one unit" -- not as the very first run ever, which
# intentionally never notifies.
with open(check_units.STATE_FILE, "w") as f:
    f.write('{"unit_ids": []}')

print("Running the real check_units.main() with faked data and time...")
check_units.main()
print("Done. If PUSHOVER_TOKEN/PUSHOVER_USER were set correctly, your phone")
print("should have just gotten an Emergency-priority alert.")

if os.path.exists(check_units.EVENTS_FILE):
    with open(check_units.EVENTS_FILE) as f:
        print(f"\n{check_units.EVENTS_FILE} now contains:")
        print(f.read())
