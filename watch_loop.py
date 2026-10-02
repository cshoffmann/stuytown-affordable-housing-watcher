"""
Continuous watcher -- checks the live API every POLL_INTERVAL_SECONDS
instead of relying on a fresh process every 5 minutes. Reuses every
function from check_units.py and screenshot.py unchanged; this script is
purely orchestration on top of logic that's already tested elsewhere.

Two modes, controlled by the BOUNDED_LOOP env var:
  - "true" (default): for GitHub Actions. A single job is triggered once
    near 7am ET and this loops internally until just after 10am ET, then
    exits. See .github/workflows/watch.yml.
  - "false": for an always-on server (a cloud VM or a home Pi). Runs
    forever; polls tightly (every POLL_INTERVAL_SECONDS) only during
    7-10am ET, and just checks once a minute the rest of the day so it
    doesn't hammer the site for no reason outside the window that
    actually matters.

Because units here are reportedly given to only the first three
applicants in order, the whole point of this script over check_units.py's
normal 5-minute cadence is cutting detection latency from "up to ~5
minutes" down to "up to ~15 seconds."
"""

import os
import random
import subprocess
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import check_units
import screenshot

POLL_INTERVAL_SECONDS = 15  # average/nominal -- actual sleep has jitter, see sleep_with_jitter()
POLL_JITTER_SECONDS = 3  # actual sleep is POLL_INTERVAL_SECONDS +/- this (so 12-18s), to avoid a perfectly fixed interval
IDLE_CHECK_INTERVAL_SECONDS = 60  # how often to check the clock when outside the window, in always-on mode
WINDOW_END_HOUR = 10
EXIT_BUFFER_MINUTES = 5  # bounded mode keeps looping a few minutes past 10am, just in case


def near_window_start() -> bool:
    """True only in the ~10 minutes around 7:00am ET. Used to suppress the
    redundant one of the two daily triggers in watch.yml (see that file's
    comments) -- without this, both the EDT- and EST-timed cron entries
    would each try to start a full multi-hour loop on the same morning."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    start = now_et.replace(hour=6, minute=55, second=0, microsecond=0)
    end = now_et.replace(hour=7, minute=5, second=0, microsecond=0)
    return start <= now_et <= end


def sleep_with_jitter() -> None:
    time.sleep(POLL_INTERVAL_SECONDS + random.uniform(-POLL_JITTER_SECONDS, POLL_JITTER_SECONDS))


def should_keep_looping(bounded: bool) -> bool:
    if not bounded:
        return True  # always-on server: never exits on its own
    now_et = datetime.now(ZoneInfo("America/New_York"))
    cutoff = now_et.replace(hour=WINDOW_END_HOUR, minute=EXIT_BUFFER_MINUTES, second=0, microsecond=0)
    return now_et < cutoff


def git_commit_and_push(message: str) -> None:
    subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
    subprocess.run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=False,
    )
    subprocess.run(["git", "add", "-A"], check=True)
    nothing_staged = subprocess.run(["git", "diff", "--staged", "--quiet"]).returncode == 0
    if nothing_staged:
        return
    subprocess.run(["git", "commit", "-m", message], check=True)
    subprocess.run(["git", "push"], check=True)


def check_once() -> None:
    units = check_units.fetch_all_units()
    current_ids = {check_units.unit_id(u) for u in units}
    previous_ids = check_units.load_last_seen()

    check_units.save_last_seen(current_ids)

    if previous_ids is None:
        print(f"Baseline -- {len(current_ids)} unit(s) currently listed.")
        git_commit_and_push("Baseline from continuous watcher")
        return

    new_ids = current_ids - previous_ids
    if not new_ids:
        return  # the common case, every ~15 seconds: nothing changed, don't even touch git

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    new_units = [u for u in units if check_units.unit_id(u) in new_ids]
    screenshot_filename = check_units.build_screenshot_filename(timestamp, new_units)

    print(f"NEW UNIT(S) DETECTED: {new_ids} -- notifying immediately")
    check_units.notify(
        f"{len(new_ids)} new unit(s) just posted at StuyTown/PCV affordable "
        f"housing -- affordable-housing.stuytown.com/apartments/"
    )
    check_units.record_event(timestamp, new_units, screenshot_filename)

    # Screenshot failure should never block the notification that already
    # went out above -- that's the part that actually matters.
    os.environ["SCREENSHOT_FILENAME"] = screenshot_filename
    try:
        screenshot.main()
    except Exception as e:
        print(f"Screenshot failed (notification already sent, non-critical): {e}")

    git_commit_and_push(f"New unit(s) detected: {', '.join(sorted(new_ids))}")


def main() -> None:
    bounded = os.environ.get("BOUNDED_LOOP", "true").lower() == "true"

    if bounded and not near_window_start():
        print("Not near the 7am ET window start -- this is the redundant "
              "DST-safety trigger (see watch.yml), exiting without looping.")
        return

    print(f"Starting watcher (bounded={bounded}, poll interval={POLL_INTERVAL_SECONDS}s)...")
    while should_keep_looping(bounded):
        if check_units.within_window():
            try:
                check_once()
            except Exception as e:
                print(f"Error during check (will retry next iteration): {e}")
            sleep_with_jitter()
        else:
            time.sleep(IDLE_CHECK_INTERVAL_SECONDS)
    print("Loop window ended, exiting.")


if __name__ == "__main__":
    main()
