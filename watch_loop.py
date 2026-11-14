"""
The live watcher, 7:00 to 10:00am ET. GitHub Actions starts it every morning
(.github/workflows/watch.yml). It runs four things side by side:

    checker    this thread: fetches the listings every 2 seconds (Pace) and
               never stops -- not for alerts, logging, git or applying.
    applier    auto_apply.Applier: browsers opened at 6:59, handed each
               check's listings FIRST; applies to qualifying units at once.
               With auto-apply off it checks every COLLECT_POLL_SECONDS and
               only rehearses: one browser fills the first listed unit's
               form each morning and presses SUBMIT with every request held
               back, so nothing is sent but the applying path stays tested.
    notifier   background: Pushover alerts, with their own retries.
    logger     background: events, applications, recordings -- paused while
               an application is running -- and ONE git commit at the end,
               saying what was listed that day.

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
import dream_units
import run_stats
import summary
import units_map

POLL_INTERVAL_SECONDS = 2  # auto-apply on: every check is one small request; cheap units go in 25-81 s
COLLECT_POLL_SECONDS = 10  # auto-apply off (collecting data): no bursts; the shortest listing so far lasted 25 s
DAILY_LOG_FILE = "data/daily_log.md"
# StuyTown posts units on the quarter hour (7:15, 7:30, 8:00, 8:15 in every
# morning recorded so far, to the second). Around each quarter hour the
# checks are much closer together, with one landing exactly on it.
BURST_POLL_SECONDS = 0.25
BURST_BEFORE_SECONDS = 3
BURST_AFTER_SECONDS = 15
POSTING_EVERY_MINUTES = 15
SLOWEST_POLL_SECONDS = 30  # back-off ceiling when the site asks us to slow down (its own Retry-After wins, up to 120)
HICCUP_POLL_SECONDS = 8  # back-off ceiling for plain errors (timeouts, server errors): retried quickly
STATUS_EVERY_SECONDS = 60  # one "still checking" log line a minute, instead of one per check
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
# After the window: floorplans, the map's buildings, the atlas and the map
# get this long (they only start once the applier has stopped).
REFERENCE_BUDGET_SECONDS = 100


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

    def __init__(self, applier=None, notifier=None, logger=None, pace: Pace | None = None,
                 bursts: bool = True):
        self.applier = applier
        self.bursts = bursts  # faster checks around each quarter hour (auto-apply on only)
        self.notifier = notifier or background.Worker("notifier")
        self.logger = logger or background.Worker("logger", yield_to=applier.busy if applier else None)
        self.pace = pace or Pace()
        self.stats = run_stats.RunStats()
        self.checks = self.failures = 0
        self._new_today = []  # (time ET, unit label, rent, bedrooms) of each new unit, for the day's log
        self._last_check_utc = None  # when the last good check's request went out
        self._last_status = time.monotonic()
        self._last_error = None

    def check(self, units: list, now: datetime | None = None, label: str | None = None) -> check_units.Changes:
        """One check's worth of work for a fetched list of units."""
        now = now or datetime.now(timezone.utc)
        qualifies = auto_apply.qualifies if auto_apply.limits_configured() else None
        changes = check_units.process_snapshot(
            units, now=now, label=label or f"{now_et():%H:%M:%S} ET",
            apply=self.applier.dispatch if self.applier else None,
            qualifies=qualifies, notifier=self.notifier, logger=self.logger, note=dream_units.alert_note,
        )
        for unit in changes.new:
            self._new_today.append((f"{now_et():%H:%M}", check_units.unit_label(unit), check_units.unit_rent(unit),
                                    unit.get("bedrooms")))
        if changes:
            self.stats.observe_changes(changes, now, qualifies, previous=self._last_check_utc)
        self._last_check_utc = now
        return changes

    def run_until(self, end: datetime) -> None:
        print(f"Checking every {self.pace.normal:g}s"
              + (f" (every {BURST_POLL_SECONDS:g}s around each quarter hour)" if self.bursts else "")
              + f" until {end:%H:%M} ET -- {check_units.LISTINGS_URL}")
        client = check_units.ListingsClient()
        next_check = time.monotonic()
        in_burst = False
        while now_et() < end:
            bursting = self.bursts and burst_slot(now_et()) is not None and self.pace.interval <= self.pace.normal
            if bursting and not in_burst:
                client.renew()  # a fresh connection for the burst
            in_burst = bursting
            self.checks += 1
            sent = datetime.now(timezone.utc)  # a unit in this answer was listed by this moment
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
                    self.check(units, now=sent)
                except Exception as e:
                    print(f"[{now_et():%H:%M:%S} ET] Check failed: {e}")
            self._housekeeping()
            next_check = self._next_check(next_check)
            delay = next_check - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_check = time.monotonic()
        client.close()
        print(f"Done: {self.checks} checks, {self.failures} failed.")

    def _next_check(self, previous: float) -> float:
        """When the next check starts (time.monotonic()). A steady beat
        measured start-to-start, so a slow answer doesn't push every later
        check back -- but around each quarter hour, a check every
        BURST_POLL_SECONDS lined up on the quarter hour itself. Never faster
        while the site is asking us to slow down."""
        now, mono = now_et(), time.monotonic()
        if self.pace.interval > self.pace.normal or not self.bursts:
            return previous + self.pace.interval
        slot = burst_slot(now)
        if slot is not None:
            beats = (now - slot).total_seconds() // BURST_POLL_SECONDS + 1
            return mono + (slot + timedelta(seconds=beats * BURST_POLL_SECONDS) - now).total_seconds()
        upcoming = next_burst_start(now)
        return min(previous + self.pace.interval, mono + (upcoming - now).total_seconds())

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

    def day_message(self) -> str:
        """The day's one commit message: 'Sun Nov 8: no new listings (6,141
        checks)', or the count with one line per new unit."""
        today = now_et()
        n = len(self._new_today)
        head = f"{today:%a %b} {today.day}: " + (f"{n} new listing{'s' if n != 1 else ''}" if n else
                                                 "no new listings")
        head += f" ({self.checks:,} checks" + (f", {self.failures:,} failed)" if self.failures else ")")
        lines = [f"{at} {label}, {_beds(beds)}" + (f", ${rent:,.0f}" if rent is not None else "")
                 for at, label, rent, beds in self._new_today[:40]]
        return head + ("\n\n" + "\n".join(lines) if lines else "")

    def write_daily_log(self, path: str | None = None) -> None:
        """Add today's line to data/daily_log.md: how many units, and each
        one's apartment, beds and rent."""
        path = path or DAILY_LOG_FILE
        today = now_et()
        units = "; ".join(f"{at} {label}, {_beds(beds)}" + (f", ${rent:,.0f}" if rent is not None else "")
                          for at, label, rent, beds in self._new_today) or "none"
        row = f"| {today:%a %Y-%m-%d} | {len(self._new_today)} | {units} |\n"
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Daily log\n\nOne line per morning: units listed between 7 and 10 AM.\n\n"
                        "| Day | New units | Units (time ET, apartment, beds, rent) |\n| --- | --- | --- |\n")
        with open(path, "a", encoding="utf-8") as f:
            f.write(row)

    def finish(self, timeout: float = 120 + REFERENCE_BUDGET_SECONDS) -> None:
        """Let a running application finish, send what's queued, write the
        morning's stats, the map and the summary, commit."""
        if self.applier:
            self.applier.stop(timeout=60)
        self.notifier.drain(timeout)
        if self.checks:
            self.logger.submit(self._wrap_up)
        self.logger.submit(commit_and_push, self.day_message())  # the day's only commit
        self.logger.drain(timeout)
        self.notifier.drain(timeout)  # the "needs a look" alert, if any

    def _wrap_up(self) -> None:
        """The morning's stats; new floorplans, the map's buildings, the
        apartment atlas and the map (units_map.refresh_all -- only now, with
        the applier stopped, so none of it can slow an application); and the
        summary page (also shown on the GitHub Actions run's page). Anything
        that needs a look goes on the page and in the log -- not to your
        phone: alerts are only for qualifying units."""
        self.stats.write(self.applier, check_units.load_state())
        self.write_daily_log()
        for line in units_map.refresh_all(budget_seconds=REFERENCE_BUDGET_SECONDS):
            print(f"   {line}")
        text = summary.write(auto_apply.MAX_RENT, rules=auto_apply.RENT_RULES)
        summary.github_step_summary(text)
        found = summary.issues(self.stats.last_summary)
        if found:
            print("Needs a look (see data/summary.md):\n" + "\n".join(f"   - {line}" for line in found))


def _quarter_hour(now: datetime) -> datetime:
    return now.replace(minute=now.minute - now.minute % POSTING_EVERY_MINUTES, second=0, microsecond=0)


def burst_slot(now: datetime) -> datetime | None:
    """The quarter hour whose burst of checks `now` is in, or None."""
    base = _quarter_hour(now)
    for slot in (base, base + timedelta(minutes=POSTING_EVERY_MINUTES)):
        if slot - timedelta(seconds=BURST_BEFORE_SECONDS) <= now < slot + timedelta(seconds=BURST_AFTER_SECONDS):
            return slot
    return None


def next_burst_start(now: datetime) -> datetime:
    upcoming = _quarter_hour(now) + timedelta(minutes=POSTING_EVERY_MINUTES)
    return upcoming - timedelta(seconds=BURST_BEFORE_SECONDS)


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
        _git("commit", "--quiet", "-m", message[:4000])
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
    """The applier, with its browsers open -- applying when auto-apply is on,
    rehearsing only (one browser, nothing ever sent) when it's off -- or None
    if it can't run this morning (it has already said why)."""
    if auto_apply.ENABLED:
        profile = auto_apply._profile_for_run()
        applier = profile and auto_apply.Applier(profile, notifier, logger)
        what = "Auto-apply: {} browsers open"
    else:
        profile = auto_apply.profile_for_recording()
        applier = profile and auto_apply.Applier(profile, notifier, logger, workers=1, record_only=True)
        what = "Rehearsal only (auto-apply is off): {} browser open"
    if not applier:
        return None
    logger.yield_to = applier.busy
    started = time.monotonic()
    applier.start()
    print(what.format(applier.workers) + f" and the site loaded in {time.monotonic() - started:.1f}s")
    return applier


def _beds(beds) -> str:
    return "?" if beds is None else "studio" if beds == 0 else f"{beds} bed"


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
    print(f"Dream units (highlighted, never applied to by themselves): {dream_units.settings_text()}")
    for warning in dream_units.startup_warnings():
        print(f"WARNING: {warning} -- using the rest")
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
    if auto_apply.ENABLED:
        watcher = Watcher(applier, notifier, logger)
    else:  # collecting data and rehearsing: a lighter pace, no bursts
        watcher = Watcher(applier, notifier, logger, pace=Pace(normal=COLLECT_POLL_SECONDS), bursts=False)
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
