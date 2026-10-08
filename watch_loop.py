"""
The live watcher: checks the StuyTown affordable listings every 15 seconds
from 7:00 to 10:00am ET, then exits. GitHub Actions starts it every morning
(.github/workflows/watch.yml). All of the "is this new? should I alert?"
logic lives in check_units.py, and applying to cheap units in auto_apply.py
-- this file is the timing, the screenshot, and saving results back to the
repo.

    python watch_loop.py                     # the real thing: if started before 7:00 ET it waits, then checks until 10:00 ET
    python watch_loop.py --minutes 5         # test run: check every 15s for 5 minutes starting now, ignoring the window
    python watch_loop.py --send-test-alert   # also send a [TEST] alert at startup, proving the Pushover keys work

Results (data/, screenshots/) are committed and pushed only when
COMMIT_RESULTS=true, which only the workflow sets -- running this on your
own computer never touches git.
"""

import argparse
import functools
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import auto_apply
import check_units

POLL_INTERVAL_SECONDS = 15
WINDOW_START_HOUR = 7  # 7:00am ET
WINDOW_END_HOUR = 10  # 10:00am ET
# A run started more than this long before 7:00 exits instead of sitting
# idle. Only a manual run can do that -- the schedule starts at 6:13.
MAX_WAIT_BEFORE_WINDOW = timedelta(hours=1)
# GitHub switches off schedules in public repos after 60 days without a
# commit. A quiet stretch with no listings would do exactly that, so after
# this many days without one, the watcher commits a tiny heartbeat file.
KEEPALIVE_AFTER_DAYS = 45
HEARTBEAT_FILE = "data/heartbeat.json"


@functools.cache
def _eastern() -> ZoneInfo:
    # Looked up lazily: Windows needs the tzdata package for this, and the
    # tests don't need it at all.
    return ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(_eastern())


def plan(now: datetime) -> tuple:
    """What a run started at `now` (ET) should do: ("watch", start, end) --
    waiting for `start` first if it's early -- or "too_early" / "done"."""
    start = now.replace(hour=WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    end = now.replace(hour=WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    if now >= end:
        return "done", start, end
    if now < start - MAX_WAIT_BEFORE_WINDOW:
        return "too_early", start, end
    return "watch", start, end


def live_screenshot(path: str) -> bool:
    import screenshot  # here, so Playwright is only needed once a screenshot is actually taken

    screenshot.take(path)
    return True


def poll_once(take_screenshot=live_screenshot, label: str | None = None) -> check_units.Changes:
    """One check: fetch the listings, compare, alert, and save the results.
    Pass take_screenshot=None to skip screenshots."""
    units = check_units.fetch_all_units()
    changes = check_units.process_snapshot(
        units, take_screenshot=take_screenshot, label=label or f"{now_et():%H:%M:%S} ET",
        act_on_listings=auto_apply.act_on_listings,
    )
    if changes:
        commit_and_push(f"Listings changed: {changes.summary()}")
    return changes


def watch_until(end: datetime) -> None:
    print(f"Checking every {POLL_INTERVAL_SECONDS}s until {end:%H:%M} ET -- {check_units.LISTINGS_URL}")
    checks = failures = 0
    next_check = time.monotonic()
    while now_et() < end:
        checks += 1
        try:
            poll_once()
        except Exception as e:
            failures += 1
            print(f"[{now_et():%H:%M:%S} ET] Check failed, trying again next check: {e}")
        # Keep a steady 15s rhythm measured start-to-start, so a slow check
        # (e.g. one that took a screenshot) doesn't push everything later.
        next_check += POLL_INTERVAL_SECONDS
        delay = next_check - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_check = time.monotonic()
    print(f"Done: {checks} checks, {failures} failed.")


def commit_and_push(message: str) -> None:
    """Commit data/ and screenshots/ and push, so the next morning's run
    starts from today's state (and you can browse it on GitHub). Only when
    COMMIT_RESULTS=true. Never raises -- a git hiccup must not stop the
    watcher."""
    if os.environ.get("COMMIT_RESULTS", "").lower() != "true":
        return
    try:
        _git("config", "user.name", "github-actions[bot]")
        _git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        _git("add", "--", "data", "screenshots")
        if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return  # nothing to commit
        _git("commit", "--quiet", "-m", message[:200])
        for _ in range(3):
            if _git("push", "--quiet", check=False).returncode == 0:
                print("   Results committed and pushed.")
                return
            # Something else was pushed to main meanwhile (e.g. a code
            # change from you) -- replay this commit on top and try again.
            if _git("pull", "--rebase", "--autostash", "--quiet", check=False).returncode != 0:
                _git("rebase", "--abort", check=False)
        print("   WARNING: couldn't push results; they'll go out with the next push.")
    except Exception as e:
        print(f"   WARNING: git commit/push failed: {e}")


def _git(*args, check=True):
    return subprocess.run(["git", *args], check=check)


def keep_schedule_alive() -> None:
    if os.environ.get("COMMIT_RESULTS", "").lower() != "true":
        return
    result = subprocess.run(["git", "log", "-1", "--format=%ct"], capture_output=True, text=True)
    last_commit = int(result.stdout.strip() or 0)
    if time.time() - last_commit < KEEPALIVE_AFTER_DAYS * 86400:
        return
    with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_keepalive_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f)
        f.write("\n")
    commit_and_push("Keepalive: no commits in a while (stops GitHub disabling the daily schedule)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Watch the StuyTown affordable listings, 7-10am ET.")
    parser.add_argument("--minutes", type=float, default=0,
                        help="test run: check for this many minutes starting now, ignoring the 7-10am window")
    parser.add_argument("--send-test-alert", action="store_true",
                        help="send a [TEST] alert at startup, to prove the Pushover keys work")
    args = parser.parse_args(argv)

    # First, so the applicant profile is masked in the log before anything else prints.
    auto_apply.startup_check()
    print(f"Pushover credentials loaded: {bool(check_units.PUSHOVER_TOKEN and check_units.PUSHOVER_USER)}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        problem = check_units.validate_pushover_credentials()
        if problem:
            # Fail loudly (red X + an email from GitHub) rather than watch
            # all morning with no way to reach your phone.
            print(f"ERROR: {problem}. Check the PUSHOVER_TOKEN / PUSHOVER_USER repository secrets.")
            return 1

    if args.send_test_alert and check_units.notify(
        "[TEST] StuyTown watcher is running",
        "If you're reading this on your phone, new-unit alerts will reach you too.",
        url=check_units.LISTINGS_URL,
        url_title="Open the listings page",
    ):
        print("Test alert sent.")

    if args.minutes > 0:
        end = now_et() + timedelta(minutes=args.minutes)
    else:
        status, start, end = plan(now_et())
        if status == "done":
            print(f"It's past {WINDOW_END_HOUR}:00 ET, so today's window is over -- nothing to do. "
                  "(Normal for the backup runs that queue behind the main one; "
                  "use --minutes N for a test run.)")
            return 0
        if status == "too_early":
            print(f"More than an hour before the {WINDOW_START_HOUR}:00 ET window -- exiting.")
            return 0
        wait = (start - now_et()).total_seconds()
        if wait > 0:
            print(f"Started early -- waiting {wait / 60:.0f} min for the {WINDOW_START_HOUR}:00 ET window.")
            time.sleep(wait)

    watch_until(end)
    commit_and_push("End of watch window: save state")
    keep_schedule_alive()
    return 0


if __name__ == "__main__":
    sys.exit(main())
