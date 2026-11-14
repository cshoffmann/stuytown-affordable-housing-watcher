"""
Sends ONE test alert to your phone -- built exactly like the real alert the
watcher sends after an application went in ("Applied: Apt ...", high
priority, unit details, "Open this unit" link) using the fake Apt 5A from
tests/fixtures/fake_units.json -- and does nothing else: no files, no state,
no git.

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

import auto_apply  # noqa: E402
import check_units  # noqa: E402

check_units.TITLE_PREFIX = "[TEST] "

fake_unit = json.loads((REPO_ROOT / "tests" / "fixtures" / "fake_units.json").read_text(encoding="utf-8"))["5A"]
attempt = auto_apply.Attempt("submitted", "TEST: nothing was applied to", filled=["first_name"])
if check_units.notify(**auto_apply.result_alert(fake_unit, attempt)):
    print("Sent -- check your phone.")
else:
    print("Not sent: set PUSHOVER_TOKEN and PUSHOVER_USER first (see the top of this file).")
