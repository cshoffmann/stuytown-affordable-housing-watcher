"""
Quick, repeatable notification test -- sends ONE real Emergency-priority
Pushover alert and does nothing else. Unlike test_check_units.py, this
never touches last_seen.json or events.json, so it's safe to run as many
times in a row as you want while you dial in your phone's settings:
silence your phone, run this, see/hear what happens, adjust Pushover's
Emergency-priority sound or your phone's Focus/DND exceptions, run again.

Usage:
    Windows (PowerShell):
        $env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python test_notification.py

    Mac/Linux:
        PUSHOVER_TOKEN=your-app-token PUSHOVER_USER=your-user-key python3 test_notification.py
"""

import check_units

check_units.notify(
    "Test alert -- tune your phone's Do Not Disturb and sound settings "
    "against this message."
)
print("Sent (or printed a skip message above if your env vars aren't set).")
print("Check your phone.")
