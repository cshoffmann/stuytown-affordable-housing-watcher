"""
Background workers: everything that isn't checking for units or applying.

The watcher runs two of these next to the checker and the applier:

    notifier  Pushover alerts. Retries by itself, so a slow or failing
              Pushover never holds anything else up.
    logger    events.json, the applications log, form recordings, git
              commits. Waits while an application is in progress, so its
              disk and CPU work never competes with one.

Each job runs in a try/except: a failing job is printed and dropped, and
never reaches the checker or the applier. Tests and the fake morning use
inline=True, which runs each job immediately in the caller's thread.
"""

import queue
import threading
import time
import traceback

# How long a worker that yields to the applier waits for it, at most, before
# running its next job anyway (an application that hangs mustn't stop
# logging for good).
MAX_YIELD_SECONDS = 90


class Worker:
    def __init__(self, name: str, inline: bool = False, yield_to=None):
        """yield_to: a function returning True while something more
        important is running (the applier) -- jobs wait until it's False."""
        self.name = name
        self.inline = inline
        self.yield_to = yield_to
        self.failures = 0
        self._jobs = queue.Queue()
        self._thread = None
        if not inline:
            self._thread = threading.Thread(target=self._run, name=f"background-{name}", daemon=True)
            self._thread.start()

    def submit(self, fn, *args, **kwargs) -> None:
        """Queue fn(*args, **kwargs). Never raises, never blocks."""
        if self.inline:
            self._call(fn, args, kwargs)
        else:
            self._jobs.put((fn, args, kwargs))

    def drain(self, timeout: float = 120) -> bool:
        """Wait until every queued job has run. True if it finished in time."""
        if self.inline:
            return True
        deadline = time.monotonic() + timeout
        while self._jobs.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self._jobs.unfinished_tasks

    def _run(self) -> None:
        while True:
            fn, args, kwargs = self._jobs.get()
            try:
                self._wait_for_turn()
                self._call(fn, args, kwargs)
            finally:
                self._jobs.task_done()

    def _wait_for_turn(self) -> None:
        if not self.yield_to:
            return
        deadline = time.monotonic() + MAX_YIELD_SECONDS
        while time.monotonic() < deadline:
            try:
                if not self.yield_to():
                    return
            except Exception:
                return
            time.sleep(0.1)

    def _call(self, fn, args, kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except Exception as e:
            self.failures += 1
            print(f"   WARNING: background {self.name} job {getattr(fn, '__name__', fn)} failed: {e}")
            if self.failures <= 3:
                traceback.print_exc(limit=3)


def notify_with_retries(notify, alert: dict, attempts: int = 4, first_wait: float = 5) -> bool:
    """Send one alert, retrying a network failure with growing waits (5s,
    15s, 45s). Runs on the notifier, so the waiting never blocks anything
    that matters. A rejected request (bad keys) isn't retried."""
    from check_units import PushoverRejected

    wait = first_wait
    for attempt in range(1, attempts + 1):
        try:
            notify(**alert)
            return True
        except PushoverRejected as e:
            print(f"   WARNING: Pushover refused '{alert.get('title')}': {e}")
            return False
        except Exception as e:
            if attempt == attempts:
                print(f"   WARNING: couldn't send '{alert.get('title')}' after {attempts} tries: {e}")
                return False
            time.sleep(wait)
            wait *= 3
    return False
