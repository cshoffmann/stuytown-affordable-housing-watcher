"""
data/summary.md: everything the watcher has collected, on one page you can
read on GitHub (or your phone) -- rewritten at the end of every morning.

    Needs a look   anything from the latest morning that means something may
                   be broken (only here and in the run's log -- never sent to
                   your phone): a form that couldn't be opened or filled, a
                   qualifying unit that wasn't applied to, the listings API
                   pushing back, direct submit unable to learn the request ...
    Mornings       one line per morning: when units were posted, how many
                   qualified, what auto-apply did, how the API held up
    Units          every unit seen (newest first): posted, rent, beds, income
                   minimum, how long it stayed listed, what auto-apply did
    Patterns       posting times, how long units last, how fast we are

It's built from data/events.json (every unit since the watcher started),
data/run_stats/ (each morning's measurements) and data/applications.json.
Nothing personal is in any of them, so nothing personal is in this.

    python summary.py      # rebuild data/summary.md from what's in data/
"""

import glob
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SUMMARY_FILE = "data/summary.md"
UNITS_SHOWN_DAYS = 90
NEW_YORK = ZoneInfo("America/New_York")
# Refusals that don't mean anything is broken: the site's rules said no.
NOT_BROKEN = ("open application", "3 applications", "already")


# ------------------------------------------------------------------ inputs


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_all() -> tuple:
    """(events, each morning's stats oldest first, applications) -- from where
    the rest of the watcher keeps them."""
    import auto_apply
    import check_units
    import run_stats

    files = sorted(glob.glob(os.path.join(run_stats.RUN_STATS_DIR, "*.json")))
    stats = [s for s in (_load(p, None) for p in files) if s]
    return _load(check_units.EVENTS_FILE, []), stats, _load(auto_apply.APPLICATIONS_FILE, {})


def _when(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(NEW_YORK)
    except ValueError:
        return None


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _secs(value) -> str:
    if value is None:
        return "?"
    return f"{value:.1f} s" if value < 120 else f"{value / 60:.0f} min"


# ------------------------------------------------------------ the units


def sightings(events: list, stats: list) -> list:
    """Every unit sighting, newest first: when posted, the unit, how long it
    stayed listed (exact from Phase 1 on, else 'up to'), the run's own
    measurements of it when there are any."""
    measured = {}
    for run in stats:
        for uid, entry in (run.get("units") or {}).items():
            if entry.get("first_seen_utc"):
                measured[(uid, entry["first_seen_utc"][:16])] = entry
    rows, open_rows = [], {}
    for event in events:
        for unit in event.get("units") or []:
            uid = unit.get("unit_id")
            if event.get("event") == "new":
                seen = event.get("detected_at_utc")
                row = {"uid": uid, "posted": _when(seen), "apartment": unit.get("apartment"),
                       "address": unit.get("address"), "rent": unit.get("price"), "beds": unit.get("bedrooms"),
                       "income": unit.get("income_requirement"), "listed_for": None, "listed_for_exact": False,
                       "measured": measured.get((uid, (seen or "")[:16]))}
                rows.append(row)
                open_rows[uid] = row
            elif event.get("event") == "removed" and uid in open_rows:
                row = open_rows.pop(uid)
                if unit.get("listed_for_seconds") is not None:
                    row["listed_for"], row["listed_for_exact"] = unit["listed_for_seconds"], True
                elif row["posted"] and _when(event.get("detected_at_utc")):
                    row["listed_for"] = (_when(event["detected_at_utc"]) - row["posted"]).total_seconds()
    return sorted(rows, key=lambda r: r["posted"] or datetime.min.replace(tzinfo=NEW_YORK), reverse=True)


def _attempt_on(applications: dict, uid: str, day) -> dict | None:
    attempts = [a for a in (applications.get(uid) or {}).get("attempts", [])
                if _when(a.get("at_utc")) and _when(a["at_utc"]).date() == day]
    return attempts[-1] if attempts else None


def _auto_apply_text(row: dict, applications: dict) -> str:
    attempt = _attempt_on(applications, row["uid"], row["posted"].date()) if row["posted"] else None
    if not attempt:
        return ""
    timeline = attempt.get("timeline") or {}
    via = timeline.get("form_via")
    via = "direct" if via and via.startswith("direct") else "browser" if via else None
    took = (row.get("measured") or {}).get("auto_apply", {}).get("submit_seconds_after_first_seen")
    parts = [attempt.get("status", "?")]
    if took is not None:
        parts.append(f"sent {took:.2f} s after it appeared")
    if via:
        parts.append(via)
    return ", ".join(parts)


# ---------------------------------------------------------- needs a look


def issues(run: dict | None) -> list:
    """What, in one morning's stats, means something may be broken."""
    if not run:
        return []
    found = []
    applier = run.get("applier")
    for entry in (run.get("units") or {}).values():
        label = f"Apt {entry.get('apartment')}, {entry.get('address')}"
        done = entry.get("auto_apply")
        if done:
            status, detail = done.get("status"), done.get("detail") or ""
            if status in ("failed", "incomplete", "blocked") or (
                    status == "rejected" and not any(text in detail for text in NOT_BROKEN)):
                found.append(f"{label}: auto-apply {status} ({detail})")
        elif applier is not None and entry.get("qualifies_for_auto_apply"):
            found.append(f"{label} qualified, but auto-apply didn't try it")
    for recording in (applier or {}).get("recordings") or []:
        check = recording.get("direct_submit_check") or ""
        if recording.get("status") == "failed":
            found.append(f"Couldn't open the form of Apt {recording.get('apartment')} ({recording.get('detail')}): "
                         "the site may have changed")
        elif "unexpected" in check or "couldn't rebuild" in check:
            found.append(f"Direct submit couldn't learn the site's request (Apt {recording.get('apartment')}): {check}")
    direct = (applier or {}).get("direct_submit") or {}
    if "code changed" in (direct.get("why_not") or ""):
        found.append(f"StuyTown changed its site code; direct submit is off until a recording proves the request "
                     f"again ({direct['why_not']})")
    if applier is not None and not applier.get("browser_ready_seconds"):
        found.append("The applier's browsers didn't start")
    for warning in (applier or {}).get("profile_warnings") or []:
        found.append(f"Your profile: {warning}")
    api = run.get("listings_api") or {}
    requests, failed = api.get("requests") or 0, sum((api.get("failures") or {}).values())
    pushed = sum(n for status, n in (api.get("statuses") or {}).items() if str(status) in ("403", "429"))
    if requests and failed / requests > 0.01:
        found.append(f"{failed} of {requests} listings checks failed")
    if pushed:
        found.append(f"The listings API asked us to slow down {pushed} times")
    return found


# ---------------------------------------------------------------- render


def render(events: list, stats: list, applications: dict, max_rent: float | None = None,
           now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    rows = sightings(events, stats)
    latest = stats[-1] if stats else None
    direct = ((latest or {}).get("applier") or {}).get("direct_submit") or {}
    limit = f"${max_rent:,.0f}/mo" if max_rent else "not set"
    lines = [
        "# StuyTown watcher: summary",
        "",
        f"Updated {now:%a %b %-d, %-I:%M %p} ET. Rent limit for auto-apply: **{limit}**. "
        + ("Direct submit: **ready**." if direct.get("ready") else
           f"Direct submit: not ready ({direct['why_not']})." if direct.get("why_not") else ""),
        "",
        "## Needs a look",
        "",
    ]
    found = issues(latest)
    lines += [f"- {line}" for line in found] or ["Nothing: the latest morning went as it should."]

    by_day = defaultdict(list)
    for row in rows:
        if row["posted"]:
            by_day[row["posted"].date()].append(row)
    runs_by_day = {}
    for run in stats:
        started = _when((run.get("run") or {}).get("started_utc"))
        if started:
            runs_by_day[started.date()] = run
    lines += ["", "## Mornings", "",
              "| Date | Units posted at (ET) | Qualified | Auto-apply | Checks | API median / p99 |",
              "| --- | --- | --- | --- | --- | --- |"]
    for day in sorted(set(by_day) | set(runs_by_day), reverse=True):
        day_rows = sorted(by_day.get(day, []), key=lambda r: r["posted"])
        run = runs_by_day.get(day) or {}
        api = run.get("listings_api") or {}
        ms = api.get("response_ms") or {}
        qualified = [r for r in day_rows if _qualifies(r, max_rent)]
        applied = Counter(_attempt_on(applications, r["uid"], day)["status"] for r in day_rows
                          if _attempt_on(applications, r["uid"], day))
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            f"{day:%a %b %-d}",
            ", ".join(sorted({f"{r['posted']:%-I:%M:%S}" for r in day_rows})) or "none",
            len(qualified),
            ", ".join(f"{n} {status}" for status, n in applied.items()) or "-",
            f"{api['requests']:,}" if api.get("requests") else "-",
            f"{ms['median']:.0f} / {ms['p99']:.0f} ms" if ms else "-"))

    cutoff = now.date().toordinal() - UNITS_SHOWN_DAYS
    lines += ["", f"## Units (last {UNITS_SHOWN_DAYS} days, newest first)", "",
              "| Posted (ET) | Apartment | Rent | Beds | Min. income | Listed for | Qualified | Auto-apply |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        if not row["posted"] or row["posted"].date().toordinal() < cutoff:
            continue
        listed = _secs(row["listed_for"]) if row["listed_for"] is not None else "still listed"
        if row["listed_for"] is not None and not row["listed_for_exact"]:
            listed = f"up to {listed}"
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            f"{row['posted']:%b %-d %-I:%M:%S %p}", f"{row['apartment']}, {row['address']}",
            f"${row['rent']:,.0f}" if row["rent"] else "?", row["beds"] if row["beds"] is not None else "?",
            f"${row['income']:,.0f}" if row["income"] else "?", listed,
            "yes" if _qualifies(row, max_rent) else "no", _auto_apply_text(row, applications)))

    lines += ["", "## Patterns", ""] + patterns(rows, stats, applications, max_rent)
    return "\n".join(lines) + "\n"


def _qualifies(row: dict, max_rent: float | None) -> bool:
    measured = (row.get("measured") or {}).get("qualifies_for_auto_apply")
    if measured is not None:
        return bool(measured)
    return bool(max_rent and row.get("rent") is not None and row["rent"] <= max_rent)


def patterns(rows: list, stats: list, applications: dict, max_rent: float | None) -> list:
    out = []
    times = Counter(f"{r['posted']:%-I:%M}" for r in rows if r["posted"])
    if times:
        out.append("- **Posting times** (ET, all days): " + ", ".join(
            f"{t} ×{n}" for t, n in sorted(times.items(), key=lambda item: datetime.strptime(item[0], "%I:%M"))))
    weekdays = Counter(f"{r['posted']:%a}" for r in rows if r["posted"])
    if weekdays:
        order = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        out.append("- **Units by weekday:** " + ", ".join(f"{d} {weekdays[d]}" for d in order if weekdays[d]))
    for name, keep in (("at or under your limit", True), ("over your limit", False)):
        group = [r["listed_for"] for r in rows if r["listed_for_exact"] and _qualifies(r, max_rent) == keep]
        if group:
            out.append(f"- **How long units {name} stay listed:** median {_secs(_median(group))} "
                       f"(fastest {_secs(min(group))}, {len(group)} units)")
    posted_at = [e.get("posted_vs_quarter_hour_their_clock_s") for run in stats
                 for e in (run.get("units") or {}).values() if e.get("posted_vs_quarter_hour_their_clock_s")]
    if posted_at:
        out.append("- **When on the quarter hour they're posted** (their clock, s): "
                   + ", ".join(f"{a:+.2f} to {b:+.2f}" for a, b in posted_at[-8:]))
    sent, received, via = [], [], Counter()
    for run in stats:
        for entry in (run.get("units") or {}).values():
            done = entry.get("auto_apply") or {}
            if done.get("status") in ("submitted", "unconfirmed"):
                sent.append(done.get("submit_seconds_after_first_seen"))
                received.append(done.get("site_received_seconds_after_first_seen"))
                via["direct" if str(done.get("via") or "").startswith("direct") else "browser"] += 1
    if sent:
        out.append(f"- **Our applications:** {len(sent)} sent ({', '.join(f'{n} {k}' for k, n in via.items())}); "
                   f"median {_secs(_median(sent))} from the unit appearing to sending"
                   + (f", {_secs(_median(received))} to the site's own clock receiving it" if _median(received)
                      else ""))
    statuses = Counter(a.get("status") for record in applications.values() for a in record.get("attempts", []))
    if statuses:
        out.append("- **All auto-apply results so far:** " + ", ".join(f"{n} {s}" for s, n in statuses.most_common()))
    return out or ["Not enough data yet."]


# ----------------------------------------------------------------- write


def write(max_rent: float | None = None, path: str | None = None) -> str:
    """Rebuild data/summary.md from what's in data/. Never raises."""
    path = path or SUMMARY_FILE
    try:
        events, stats, applications = load_all()
        text = render(events, stats, applications, max_rent)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return text
    except Exception as e:
        print(f"   WARNING: couldn't write the summary: {type(e).__name__}: {e}")
        return ""


def github_step_summary(text: str) -> None:
    """The top of the summary on the GitHub Actions run's own page."""
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target or not text:
        return
    top = text.split("\n## Units")[0]
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(top + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    import auto_apply

    print(write(auto_apply.MAX_RENT))
