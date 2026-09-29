"""
Local test harness for check_units.py.

Fakes exactly two things -- the live API response, and the current time --
and then runs the REAL main(), unit_id(), load_last_seen(), save_last_seen(),
and notify() unmodified. That means a successful run here proves both the
detection logic and the actual ntfy push to your phone work, not just that
the code compiles.

Run this from the same folder as check_units.py:

    Windows (PowerShell):
        $env:NTFY_TOPIC="your-topic-name"; python test_check_units.py

    Mac/Linux:
        NTFY_TOPIC=your-topic-name python3 test_check_units.py

Watch your phone -- you should get a push notification within a couple
seconds of running this.

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
print("Done. If NTFY_TOPIC was set correctly, your phone should have buzzed.")

if os.path.exists(check_units.EVENTS_FILE):
    with open(check_units.EVENTS_FILE) as f:
        print(f"\n{check_units.EVENTS_FILE} now contains:")
        print(f.read())
