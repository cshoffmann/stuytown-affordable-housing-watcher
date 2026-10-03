"""
Sends ONE test alert to your phone -- built exactly like a real new-unit alert
(Emergency priority, unit details, "Open this unit to apply" link) using the
fake Apt 5A from tests/fixtures/fake_units.json -- and does nothing else: no
files, no state, no git. Run it as often as you like while you tune Do Not
Disturb / Focus exceptions and the Emergency sound in the Pushover app.

The link opens the real site's unit page; since 5A is fake, that page won't
show a unit -- a real alert's link will.

    PowerShell:
        $env:PUSHOVER_TOKEN="your-app-token"; $env:PUSHOVER_USER="your-user-key"; python tests/send_test_notification.py
    Mac/Linux:
        PUSHOVER_TOKEN=your-app-token PUSHOVER_USER=your-user-key python3 tests/send_test_notification.py
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import check_units  # noqa: E402

check_units.TITLE_PREFIX = "[TEST] "
check_units.EMERGENCY_EXPIRE_SECONDS = 180  # stops repeating after 3 minutes even if not acknowledged

fake_unit = json.loads((REPO_ROOT / "tests" / "fixtures" / "fake_units.json").read_text(encoding="utf-8"))["5A"]
if check_units.notify(**check_units.new_units_alert([fake_unit])):
    print("Sent -- check your phone. Tap Acknowledge to stop the repeats.")
else:
    print("Not sent: set PUSHOVER_TOKEN and PUSHOVER_USER first (see the top of this file).")
