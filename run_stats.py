"""
One file per morning, data/run_stats/<date>.json, with what Phase 2 needs to
know and the individual recordings don't show:

  listings_api  How the listings API behaves when checked every 2 seconds:
                response times (median / p90 / p99 / worst), statuses,
                whether the kept-open connection held, and the caching
                headers. Is it behind a CDN (Age, X-Cache, CF-Cache-Status)?
                Could a cheaper "anything changed?" request (ETag /
                Last-Modified) replace fetching the full list? Is there a
                rate limit (X-RateLimit-*)? How far is its clock from ours?
  pace          Every slow-down and speed-up of the checks, and why.
  units         Every unit seen this morning: when it was first and last
                listed (to within one check), so how long it really stayed
                up -- and, for the ones auto-apply went for, how many
                seconds after it first appeared SUBMIT was pressed.
  applier       How long the browsers took to open; the form address it
                learned; what the idle listings page did on its own (does
                the site poll the API itself, or hold a live connection?).

Nothing personal is in it: it's about the site and the listings.
"""

import hashlib
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

RUN_STATS_DIR = "data/run_stats"
# Headers worth knowing about. Values that change on every response (dates,
# request IDs, ETags) are counted, not listed.
CACHE_HEADERS = ("cache-control", "age", "etag", "last-modified", "expires", "vary", "pragma",
                 "x-cache", "x-cache-hits", "cf-cache-status", "x-served-by", "x-amz-cf-pop", "via", "server",
                 "content-encoding", "connection", "keep-alive", "x-powered-by", "x-aspnet-version",
                 "strict-transport-security", "access-control-allow-origin")
RATE_LIMIT_PREFIXES = ("x-ratelimit", "ratelimit", "retry-after", "x-rate-limit")
PRESENCE_ONLY = ("cf-ray", "x-azure-ref", "x-request-id", "x-amz-cf-id", "x-amzn-requestid", "x-correlation-id")
MAX_VALUES_PER_HEADER = 8


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RunStats:
    def __init__(self):
        self.started_utc = _utc_now()
        self.latencies_ms = []
        self.statuses = Counter()
        self.failures = Counter()
        self.reused = Counter()
        self.sizes = []
        self.headers = defaultdict(Counter)
        self.present = Counter()
        self.clock_skew_s = []
        self._etag_for_body = {}
        self.etag_matches_body = True  # same ETag never seen with different contents
        self._bodies = set()  # digests of every different response
        self.pace = []
        self.units = {}

    # ------------------------------------------------------------- checker

    def observe_responses(self, responses: list) -> None:
        for r in responses:
            self.latencies_ms.append(r["ms"])
            self.statuses[r["status"]] += 1
            self.reused["kept-open connection" if r.get("reused_connection") else "new connection"] += 1
            self.sizes.append(r["bytes"])
            headers = r.get("headers") or {}
            for name, value in headers.items():
                if name in PRESENCE_ONLY:
                    self.present[name] += 1
                elif name in CACHE_HEADERS or name.startswith(RATE_LIMIT_PREFIXES):
                    values = self.headers[name]
                    if name in ("age", "etag", "last-modified", "expires") or len(values) >= MAX_VALUES_PER_HEADER:
                        values["(varies)" if name not in ("age",) else _age_bucket(value)] += 1
                    else:
                        values[value] += 1
            self._observe_identity(headers.get("etag"), r.get("body") or b"")
            self._observe_clock(headers.get("date"))

    def _observe_identity(self, etag, body: bytes) -> None:
        digest = hashlib.sha1(body).hexdigest()
        self._bodies.add(digest)
        if etag:
            known = self._etag_for_body.get(etag)
            if known is not None and known != digest:
                self.etag_matches_body = False
            self._etag_for_body[etag] = digest

    def _observe_clock(self, date_header) -> None:
        if not date_header or len(self.clock_skew_s) > 2000:
            return
        try:
            theirs = parsedate_to_datetime(date_header)
            self.clock_skew_s.append((theirs - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            pass

    def observe_failure(self, error: Exception) -> None:
        self.failures[f"{type(error).__name__}: {str(error)[:80]}"] += 1

    def observe_pace(self, interval: float, reason: str) -> None:
        self.pace.append({"at_utc": _utc_now(), "interval_s": interval, "reason": reason[:120]})

    def observe_changes(self, changes, now: datetime, qualifies=None) -> None:
        """New and removed units of one check made at `now` (to the
        millisecond: SUBMIT is measured against it)."""
        import check_units

        stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        for unit in changes.new:
            uid = check_units.unit_id(unit)
            self.units[uid] = {
                "apartment": check_units.apartment(unit),
                "address": check_units.address(unit),
                "rent": check_units.unit_rent(unit),
                "income_requirement": unit.get("incomeRequirement"),
                "qualifies_for_auto_apply": bool(qualifies(unit)) if qualifies else None,
                "first_seen_utc": stamp,
            }
        for record in changes.removed:
            uid = check_units.unit_id(record["data"])
            entry = self.units.setdefault(uid, {"apartment": check_units.apartment(record["data"]),
                                                "first_seen_utc": record.get("first_seen_utc")})
            entry["last_seen_utc"] = record.get("last_seen_utc")
            entry["removal_confirmed_utc"] = stamp

    # ------------------------------------------------------------- summary

    def summary(self, applier=None, state: dict | None = None) -> dict:
        import check_units

        for uid, record in (state or {}).items():  # still listed at the end
            if uid in self.units:
                self.units[uid].setdefault("last_seen_utc", record.get("last_seen_utc"))
                self.units[uid]["still_listed_at_end"] = True
        for entry in self.units.values():
            if entry.get("first_seen_utc") and entry.get("last_seen_utc"):
                entry["listed_for_seconds"] = check_units._seconds_between(
                    entry["first_seen_utc"][:19] + "Z", entry["last_seen_utc"][:19] + "Z")
        if applier is not None:
            for uid, record in applier.applications.items():
                if uid not in self.units:
                    continue
                attempts = [a for a in record.get("attempts", []) if a.get("at_utc", "") >= self.started_utc[:19]]
                if not attempts:
                    continue
                last = attempts[-1]
                timeline = last.get("timeline") or {}
                entry = self.units[uid]
                entry["auto_apply"] = {"status": last.get("status"), "seconds": last.get("seconds"),
                                       "timeline": timeline}
                pressed = timeline.get("submit_pressed_utc")
                if pressed and entry.get("first_seen_utc"):
                    entry["auto_apply"]["submit_seconds_after_first_seen"] = _seconds(entry["first_seen_utc"], pressed)
                    if entry.get("last_seen_utc"):
                        # Positive: still listed that long after SUBMIT (to within a check).
                        entry["auto_apply"]["still_listed_seconds_after_submit"] = _seconds(pressed,
                                                                                            entry["last_seen_utc"])

        latencies = sorted(self.latencies_ms)
        return {
            "run": {"started_utc": self.started_utc, "ended_utc": _utc_now()},
            "listings_api": {
                "requests": len(latencies),
                "statuses": dict(self.statuses),
                "failures": dict(self.failures),
                "connections": dict(self.reused),
                "response_ms": _percentiles(latencies),
                "response_bytes": {"min": min(self.sizes), "max": max(self.sizes)} if self.sizes else None,
                "distinct_responses": len(self._bodies),  # how many times the listings actually changed, + 1
                "etag": {"seen": "etag" in self.headers, "same_etag_always_same_contents": self.etag_matches_body}
                if "etag" in self.headers else {"seen": False},
                "headers": {name: dict(values) for name, values in sorted(self.headers.items())},
                "headers_present": dict(self.present),
                "their_clock_minus_ours_s": _percentiles(sorted(self.clock_skew_s)),
            },
            "pace": self.pace,
            "units": self.units,
            "applier": applier.observations() if applier is not None else None,
        }

    def write(self, applier=None, state: dict | None = None, directory: str | None = None) -> str:
        directory = directory or RUN_STATS_DIR
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%MZ")
        path = os.path.join(directory, f"{stamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.summary(applier, state), f, indent=2, sort_keys=True, default=str)
            f.write("\n")
        print(f"   Run stats written: {path}")
        return path


def _percentiles(values: list) -> dict | None:
    if not values:
        return None

    def at(q):
        return round(values[min(len(values) - 1, int(q * len(values)))], 1)

    return {"min": round(values[0], 1), "median": round(statistics.median(values), 1), "p90": at(0.9),
            "p99": at(0.99), "max": round(values[-1], 1)}


def _age_bucket(value: str) -> str:
    try:
        age = int(value)
    except ValueError:
        return "(not a number)"
    for limit in (0, 1, 2, 5, 10, 30, 60, 300):
        if age <= limit:
            return f"<= {limit}s"
    return "> 300s"


def _seconds(earlier: str, later: str) -> float | None:
    try:
        parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
        return round((parse(later) - parse(earlier)).total_seconds(), 3)
    except (TypeError, ValueError):
        return None
