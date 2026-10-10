"""
The live watcher, 7:00 to 10:00am ET. GitHub Actions starts it every morning
(.github/workflows/watch.yml). It runs four things side by side:

    checker    this thread: fetches the listings every 2 seconds (Pace) and
               never stops -- not for alerts, logging, git or applying.
    applier    auto_apply.Applier: browsers opened at 6:59, handed each
               check's listings FIRST; applies to qualifying units at once.
    notifier   background: Pushover alerts, with their own retries.
    logger     background: events, applications, recordings, git commits --
               batched, and paused while an application is running.

"Is this new? Which alert?" lives in check_units.py, applying in
auto_apply.py; this file is the timing and the wiring. docs/architecture.md
draws it, next to the old design.

    python watch_loop.py                     # the real thing: if started before 7:00 ET it waits, then checks until 10:00 ET
    python watch_loop.py --minutes 5         # test run: check every 2s for 5 minutes starting now, ignoring the window
    python watch_loop.py --send-test-alert   # also send a [TEST] alert at startup, proving the Pushover keys work

Results (data/) are committed and pushed only when COMMIT_RESULTS=true, which
only the workflow sets -- running this on your own computer never touches git.
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
import background
import check_units
import run_stats

POLL_INTERVAL_SECONDS = 2  # every check is one small request; cheap units have been gone in 15-45s
SLOWEST_POLL_SECONDS = 30  # back-off ceiling when the site asks us to slow down (its own Retry-After wins, up to 120)
HICCUP_POLL_SECONDS = 8  # back-off ceiling for plain errors (timeouts, server errors): retried quickly
STATUS_EVERY_SECONDS = 60  # one "still checking" log line a minute, instead of one per check
COMMIT_EVERY_SECONDS = 300  # results are committed in batches, not on every change
APPLIER_HEAD_START = timedelta(minutes=1)  # browsers open (and the site loaded) this long before 7:00
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


class Pace:
    """How long to wait between checks: POLL_INTERVAL_SECONDS normally.

    - The site asks us to slow down (429 Too Many Requests, 403 refused):
      double the wait, up to SLOWEST_POLL_SECONDS -- or the site's own
      Retry-After -- and come back down gradually, halving after every 3 good
      checks in a row.
    - A plain error (timeout, server error, network): it's the site's
      trouble, not our pace, so retry soon -- double the wait, up to
      HICCUP_POLL_SECONDS -- and go straight back to normal on the first good
      check. That keeps a hiccup at 7:14 from slowing down the checks at 7:15."""

    def __init__(self, normal: float = POLL_INTERVAL_SECONDS, slowest: float = SLOWEST_POLL_SECONDS,
                 hiccup_ceiling: float = HICCUP_POLL_SECONDS):
        self.normal, self.slowest, self.hiccup_ceiling = normal, slowest, hiccup_ceiling
        self.interval = normal
        self._pushed_back = False
        self._good = 0

    def ok(self) -> None:
        if not self._pushed_back:
            self.interval = self.normal
            return
        self._good += 1
        if self._good >= 3:
            self._good = 0
            self.interval = max(self.normal, self.interval / 2)
            self._pushed_back = self.interval > self.normal

    def trouble(self, retry_after: float | None = None, pushed_back: bool = False) -> None:
        self._good = 0
        if pushed_back:
            self._pushed_back = True
            self.interval = max(min(self.slowest, self.interval * 2), min(retry_after or 0, 120))
        else:
            self.interval = max(self.interval, min(self.hiccup_ceiling, self.interval * 2))


class Watcher:
    """The checker, and the wiring between it, the applier and the two
    background workers."""

    def __init__(self, applier=None, notifier=None, logger=None, pace: Pace | None = None):
        self.applier = applier
        self.notifier = notifier or background.Worker("notifier")
        self.logger = logger or background.Worker("logger", yield_to=applier.busy if applier else None)
        self.pace = pace or Pace()
        self.stats = run_stats.RunStats()
        self.checks = self.failures = 0
        self._unsent = []  # change summaries since the last commit
        self._last_status = self._last_commit = time.monotonic()
        self._last_error = None

    def check(self, units: list, now: datetime | None = None, label: str | None = None) -> check_units.Changes:
        """One check's worth of work for a fetched list of units."""
        now = now or datetime.now(timezone.utc)
        qualifies = auto_apply.qualifies if auto_apply.MAX_RENT is not None else None
        changes = check_units.process_snapshot(
            units, now=now, label=label or f"{now_et():%H:%M:%S} ET",
            apply=self.applier.dispatch if self.applier else None,
            qualifies=qualifies, notifier=self.notifier, logger=self.logger,
        )
        if changes:
            self._unsent.append(f"{now_et():%H:%M} {changes.summary()}")
            self.stats.observe_changes(changes, now, qualifies)
        return changes

    def run_until(self, end: datetime) -> None:
        print(f"Checking every {self.pace.normal:g}s until {end:%H:%M} ET -- {check_units.LISTINGS_URL}")
        client = check_units.ListingsClient()
        next_check = time.monotonic()
        while now_et() < end:
            self.checks += 1
            try:
                units = client.fetch_all()
            except check_units.ListingsUnavailable as e:
                self.stats.observe_responses(client.responses)
                self._failed(e, retry_after=e.retry_after, pushed_back=e.status in (403, 429))
            except Exception as e:
                self.stats.observe_responses(client.responses)
                self._failed(e)
            else:
                self.stats.observe_responses(client.responses)
                if self._last_error:
                    print(f"[{now_et():%H:%M:%S} ET] Listings reachable again")
                    self._last_error = None
                before = self.pace.interval
                self.pace.ok()
                if self.pace.interval != before:
                    self.stats.observe_pace(self.pace.interval, "good checks again")
                try:
                    self.check(units)
                except Exception as e:
                    print(f"[{now_et():%H:%M:%S} ET] Check failed: {e}")
            self._housekeeping()
            # A steady beat measured start-to-start; a slow answer doesn't
            # push every later check back.
            next_check += self.pace.interval
            delay = next_check - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_check = time.monotonic()
        client.close()
        print(f"Done: {self.checks} checks, {self.failures} failed.")

    def _failed(self, error: Exception, retry_after: float | None = None, pushed_back: bool = False) -> None:
        self.failures += 1
        self.stats.observe_failure(error)
        before = self.pace.interval
        self.pace.trouble(retry_after, pushed_back)
        if self.pace.interval != before:
            self.stats.observe_pace(self.pace.interval, ("site asked to slow down: " if pushed_back else "error: ")
                                    + str(error))
        message = str(error)[:200]
        if message != self._last_error or self.pace.interval != before:
            print(f"[{now_et():%H:%M:%S} ET] Check failed ({message}); "
                  f"next checks every {self.pace.interval:g}s")
        self._last_error = message

    def _housekeeping(self) -> None:
        now = time.monotonic()
        if now - self._last_status >= STATUS_EVERY_SECONDS:
            self._last_status = now
            print(f"[{now_et():%H:%M:%S} ET] still checking: {self.checks} checks so far, "
                  f"{self.failures} failed, every {self.pace.interval:g}s")
        if now - self._last_commit >= COMMIT_EVERY_SECONDS:
            self._last_commit = now
            self.commit_soon()

    def commit_soon(self, final: bool = False) -> None:
        """Queue a commit of everything since the last one, on the logger --
        after the jobs already queued there, and not while an application runs."""
        summaries, self._unsent = self._unsent, []
        message = ("End of watch window" if final else "Listings changed") + (
            ": " + " | ".join(summaries) if summaries else ": save state")
        self.logger.submit(commit_and_push, message)

    def finish(self, timeout: float = 120) -> None:
        """Let a running application finish, send what's queued, write the
        morning's stats, commit."""
        if self.applier:
            self.applier.stop(timeout=60)
        self.notifier.drain(timeout)
        if self.checks:
            self.logger.submit(self.stats.write, self.applier, check_units.load_state())
        self.commit_soon(final=True)
        self.logger.drain(timeout)


def commit_and_push(message: str) -> None:
    """Commit data/ and push, so the next morning's run starts from today's
    state (and you can browse it on GitHub). Only when COMMIT_RESULTS=true.
    Runs on the background logger; never raises."""
    if os.environ.get("COMMIT_RESULTS", "").lower() != "true":
        return
    try:
        _git("config", "user.name", "github-actions[bot]")
        _git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        _git("add", "--", "data")
        if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return  # nothing to commit
        _git("commit", "--quiet", "-m", message[:300])
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


def start_applier(notifier, logger):
    """The applier, with its browsers open -- or None if auto-apply is off or
    can't run this morning (startup_check has already said why)."""
    if not auto_apply.ENABLED:
        return None
    profile = auto_apply._profile_for_run()
    if profile is None:
        return None
    applier = auto_apply.Applier(profile, notifier, logger)
    logger.yield_to = applier.busy
    started = time.monotonic()
    applier.start()
    print(f"Auto-apply: {applier.workers} browsers open and the site loaded in {time.monotonic() - started:.1f}s")
    return applier


def _sleep_until(when: datetime) -> None:
    wait = (when - now_et()).total_seconds()
    if wait > 0:
        time.sleep(wait)


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
    pushover_problem = None
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pushover_problem = check_units.validate_pushover_credentials()
        if pushover_problem:
            # Alerts can't reach you, but auto-apply doesn't need them: keep
            # going, and fail the run at the end (red X + an email from GitHub).
            print(f"ERROR: {pushover_problem}. Check the PUSHOVER_TOKEN / PUSHOVER_USER repository secrets. "
                  "Watching and auto-applying anyway.")

    if args.send_test_alert and background.notify_with_retries(check_units.notify, {
        "title": "[TEST] StuyTown watcher is running",
        "message": "If you're reading this on your phone, alerts will reach you too.",
        "url": check_units.LISTINGS_URL,
        "url_title": "Open the listings page",
    }, attempts=2):
        print("Test alert sent.")

    if args.minutes > 0:
        start, end = None, now_et() + timedelta(minutes=args.minutes)
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
        if now_et() < start - APPLIER_HEAD_START:
            print(f"Started early -- waiting for {start - APPLIER_HEAD_START:%H:%M} ET to open the browsers.")
            _sleep_until(start - APPLIER_HEAD_START)

    notifier = background.Worker("notifier")
    logger = background.Worker("logger")
    applier = start_applier(notifier, logger)
    watcher = Watcher(applier, notifier, logger)
    if start is not None:
        _sleep_until(start)
    try:
        watcher.run_until(end)
    finally:
        watcher.finish()
    keep_schedule_alive()
    return 1 if pushover_problem else 0


if __name__ == "__main__":
    sys.exit(main())
