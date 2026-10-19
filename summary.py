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
    Dream units    the units that were (or nearly were) a dream (dream_units.py)
    Units          every unit seen (newest first): posted, where, floor, size,
                   rent, finish, layout, features, which way it faces, its
                   dream rating, how long it stayed listed, what auto-apply did
    Buildings      each building seen: zone, Oval side, walk to the L, rents
    Layouts        each layout seen, with its floorplan
    Patterns       posting times, how long units last, how fast we are
    Feature codes  what the listings' feature codes mean

It's built from data/events.json (every unit since the watcher started),
data/run_stats/ (each morning's measurements), data/applications.json and
the reference data (data/reference/, data/floorplans/). Nothing personal is
in any of them, so nothing personal is in this.

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
                       "data": unit.get("data") or {},
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
           now: datetime | None = None, rules: dict | None = None) -> str:
    import auto_apply
    import dream_units

    now = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    limits = (rules or {}, max_rent)
    rows = sightings(events, stats)
    reference = {"buildings": dream_units.load_buildings(), "atlas": dream_units.build_atlas(events),
                 "overrides": dream_units.load_overrides()}
    for row in rows:
        row["rating"] = dream_units.rate(row["data"], **reference) if row["data"] else None
    latest = stats[-1] if stats else None
    direct = ((latest or {}).get("applier") or {}).get("direct_submit") or {}
    limit = auto_apply.limits_text(rules or {}, max_rent) or "not set"
    lines = [
        "# StuyTown watcher: summary",
        "",
        f"Updated {now:%a %b %-d, %-I:%M %p} ET. Auto-apply rent limits: **{limit}**. "
        f"Dream units: {dream_units.settings_text()}. "
        + ("Direct submit: **ready**." if direct.get("ready") else
           f"Direct submit: not ready ({direct['why_not']})." if direct.get("why_not") else ""),
        "",
        f"Map of every building and unit seen: [units.geojson](units.geojson) (GitHub draws it; tap a dot), "
        f"or [on geojson.io]({MAP_VIEWER}).",
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
        qualified = [r for r in day_rows if _qualifies(r, limits)]
        applied = Counter(_attempt_on(applications, r["uid"], day)["status"] for r in day_rows
                          if _attempt_on(applications, r["uid"], day))
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            f"{day:%a %b %-d}",
            ", ".join(sorted({f"{r['posted']:%-I:%M:%S}" for r in day_rows})) or "none",
            len(qualified),
            ", ".join(f"{n} {status}" for status, n in applied.items()) or "-",
            f"{api['requests']:,}" if api.get("requests") else "-",
            f"{ms['median']:.0f} / {ms['p99']:.0f} ms" if ms else "-"))

    lines += ["", "## Dream units", ""] + dream_section(rows)

    cutoff = now.date().toordinal() - UNITS_SHOWN_DAYS
    lines += ["", f"## Units (last {UNITS_SHOWN_DAYS} days, newest first)", "",
              "| Posted (ET) | Apartment | Zone | Floor | Beds | Sq ft | Rent | $/sq ft | Min. income | Finish "
              "| Layout | Features | Faces | Dream rating | Listed for | Qualified | Auto-apply |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        if not row["posted"] or row["posted"].date().toordinal() < cutoff:
            continue
        listed = _secs(row["listed_for"]) if row["listed_for"] is not None else "still listed"
        if row["listed_for"] is not None and not row["listed_for_exact"]:
            listed = f"up to {listed}"
        lines.append("| " + " | ".join([
            f"{row['posted']:%b %-d %-I:%M:%S %p}", f"{row['apartment']}, {row['address']}",
            *unit_cells(row),
            f"${row['income']:,.0f}" if row["income"] else "?",
            *unit_detail_cells(row),
            listed, "yes" if _qualifies(row, limits) else "no", _auto_apply_text(row, applications)]) + " |")

    lines += ["", "## Buildings", ""] + buildings_section(rows, reference["buildings"])
    lines += ["", "## Layouts", ""] + layouts_section(rows)
    lines += ["", "## Patterns", ""] + patterns(rows, stats, applications, limits)
    lines += ["", "## Feature codes", ""] + feature_codes_section(rows)
    return "\n".join(lines) + "\n"


def _qualifies(row: dict, limits) -> bool:
    """Did it qualify for auto-apply: what the morning measured, else its
    rent against the limits ((rules by bedrooms, max rent), or one number)."""
    measured = (row.get("measured") or {}).get("qualifies_for_auto_apply")
    if measured is not None:
        return bool(measured)
    rules, max_rent = limits if isinstance(limits, tuple) else ({}, limits)
    beds = row.get("beds")
    limit = rules.get(int(beds), max_rent) if isinstance(beds, (int, float)) else max_rent
    return bool(limit and row.get("rent") is not None and row["rent"] <= limit)


MAP_VIEWER = ("https://geojson.io/#data=data:text/x-url,https%3A%2F%2Fraw.githubusercontent.com%2Fcshoffmann"
              "%2Fstuytown-affordable-housing-watcher%2Fmain%2Fdata%2Funits.geojson")
FLOORPLANS_FROM_SUMMARY = "floorplans"  # data/floorplans, relative to data/summary.md


def _floorplan_file(data: dict, kind: str = "png") -> str | None:
    import reference_data

    layout = str((data.get("style") or {}).get("name") or "")
    path = (data.get("floorplan") or {}).get("floorplanSvg")
    if not layout or not path:
        return None
    name = f"{reference_data.floorplan_name(layout, path)}.{kind}"
    return name if os.path.exists(os.path.join(reference_data.FLOORPLANS_DIR, name)) else None


def unit_cells(row: dict) -> list:
    """Zone, floor, beds, sq ft, rent, $/sq ft."""
    import dream_units
    import stuytown_geo as geo

    data = row["data"]
    rent = row["rent"]
    sqft = data.get("sqft")
    zone = dream_units.building_info(data).get("zone", "") if data else ""
    floor = str(data.get("unitFloor") or "").lstrip("0") or (str(geo.floor_number(data) or "") if data else "")
    return [zone, floor or "?",
            str(row["beds"]) if row["beds"] is not None else "?", str(sqft or "?"),
            f"${rent:,.0f}" if rent else "?", f"${rent / sqft:.2f}" if rent and sqft else "?"]


def unit_detail_cells(row: dict) -> list:
    """Finish, layout (linked to its floorplan), features, faces, dream rating."""
    import dream_units

    data, rating = row["data"], row.get("rating") or {}
    layout = str((data.get("style") or {}).get("name") or "")
    plan = _floorplan_file(data) or _floorplan_file(data, "svg")
    face = rating.get("facing") or {}
    faces = ("/".join(face.get("compass") or []) + ("" if face.get("certain") else "?")) if face.get("compass") else ""
    tier = rating.get("tier")
    return [dream_units.finish_name(data),
            f"[{layout}]({FLOORPLANS_FROM_SUMMARY}/{plan})" if plan else layout,
            ", ".join(dream_units.features(data)), faces,
            f"**{tier}**" if tier in ("dream", "possible dream") else (tier or "")]


def dream_section(rows: list) -> list:
    import dream_units

    good = [r for r in rows if (r.get("rating") or {}).get("tier") in ("dream", "possible dream", "almost",
                                                                      "great light")]
    out = [f"Dream: {dream_units.settings_text()}; Oval views; windows facing E round to SW (the north side of the "
           "Oval). Highlighted only -- auto-apply follows its own rules. Set with the DREAM_RULES and DREAM_FLOORS "
           "repository variables.", ""]
    if not good:
        matches = sum(1 for r in rows if (r.get("rating") or {}).get("tier") == "price match")
        return out + [f"None yet. {matches} unit{'s' if matches != 1 else ''} matched the dream rent "
                      "but not the rest (see Units)."]
    out += ["| Posted (ET) | Apartment | Rent | Beds | Floor | Faces | Rating | Why |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    order = {t: i for i, t in enumerate(dream_units.TIERS)}
    for r in sorted(good, key=lambda r: (order[r["rating"]["tier"]], -(r["posted"].timestamp() if r["posted"] else 0))):
        rating = r["rating"]
        out.append(f"| {r['posted']:%b %-d %-I:%M %p} | {r['apartment']}, {r['address']} | "
                   f"${r['rent']:,.0f} | {r['beds']} | {rating['floor']} | {'/'.join(rating['facing']['compass'])} | "
                   f"{rating['tier']} | {rating['why']} |")
    return out


def buildings_section(rows: list, buildings: dict | None) -> list:
    import dream_units

    out = []
    if buildings:
        sunny = sorted({a for fp in buildings.get("footprints", []) for a in fp.get("addresses", [])
                        if any(w.get("faces_oval") and w.get("good") for w in fp.get("walls", []))},
                       key=lambda a: (a.split(" ", 1)[-1], int(a.split()[0]) if a.split()[0].isdigit() else 0))
        out += ["**Buildings whose Oval windows get sun** (OpenStreetMap; gold on the map): "
                + (", ".join(" ".join(w.capitalize() for w in a.split()) for a in sunny) or "none found"), ""]
    else:
        out += ["Building outlines and wall directions aren't fetched yet: they come with the next morning's "
                "wrap-up, or run the workflow with **refresh_map** ticked.", ""]
    by_building = defaultdict(list)
    for r in rows:
        if r["data"]:
            by_building[r["address"]].append(r)
    out += ["| Building | Zone | The Oval | Walk to the L | Units seen | Cheapest 1 bed | Cheapest 2 bed | "
            "Oval views listed |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for address, group in sorted(by_building.items(), key=lambda item: -len(item[1])):
        info = dream_units.building_info(group[0]["data"], buildings)
        side = info.get("oval_side") or {}
        if info.get("oval_walls"):
            oval = f"windows face {'/'.join(info['oval_walls'])}"
        elif info.get("zone") == "Oval" and side:
            oval = f"on its {side['side']} side, so facing {side['faces_compass']} (estimated)"
        else:
            oval = ""

        def cheapest(beds):
            rents = [r["rent"] for r in group if r["beds"] == beds and r["rent"]]
            return f"${min(rents):,.0f}" if rents else ""

        views = sum(1 for r in group if dream_units.OVAL_VIEWS in dream_units.codes(r["data"]))
        walk = info.get("walk_to_l")
        out.append(f"| {address} | {info.get('zone', '')} | {oval} | {f'{walk[0]:g} min' if walk else ''} | "
                   f"{len(group)} | {cheapest(1)} | {cheapest(2)} | {views or ''} |")
    return out


def layouts_section(rows: list) -> list:
    layouts = defaultdict(list)
    for r in rows:
        if r["data"]:
            layouts[str((r["data"].get("style") or {}).get("name") or "?")].append(r)
    if not layouts:
        return ["None seen yet."]
    out = ["| Layout | Beds | Sq ft | Seen | Rents | Floorplan |", "| --- | --- | --- | --- | --- | --- |"]
    for layout, group in sorted(layouts.items(), key=lambda item: (item[1][0]["beds"] or 0, item[0])):
        data = group[0]["data"]
        rents = sorted(r["rent"] for r in group if r["rent"])
        png, svg = _floorplan_file(data), _floorplan_file(data, "svg")
        if png:
            plan = (f'<a href="{FLOORPLANS_FROM_SUMMARY}/{png}"><img src="{FLOORPLANS_FROM_SUMMARY}/{png}" '
                    f'width="140" alt="layout {layout}"></a>')
        elif svg:
            plan = f"[SVG]({FLOORPLANS_FROM_SUMMARY}/{svg})"
        else:
            plan = "not downloaded yet"
        out.append(f"| {layout} | {group[0]['beds']} | {data.get('sqft') or '?'} | {len(group)} | "
                   + (f"${rents[0]:,.0f}" + (f"-${rents[-1]:,.0f}" if rents[-1] != rents[0] else "") if rents else "?")
                   + f" | {plan} |")
    return out


def feature_codes_section(rows: list) -> list:
    seen, counts = {}, Counter()
    for r in rows:
        for a in r["data"].get("amenities") or []:
            code = a.get("code")
            if code:
                seen[code] = a.get("friendlyDescription") or ""
                counts[code] += 1
    if not seen:
        return ["None seen yet."]
    out = ["| Code | Means | Units |", "| --- | --- | --- |"]
    for code, n in sorted(counts.items(), key=lambda item: (item[0].startswith(("FS", "z")), -item[1], item[0])):
        out.append(f"| {code} | {seen[code]} | {n} |")
    return out


def patterns(rows: list, stats: list, applications: dict, limits=None) -> list:
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
        group = [r["listed_for"] for r in rows if r["listed_for_exact"] and _qualifies(r, limits) == keep]
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


def write(max_rent: float | None = None, path: str | None = None, rules: dict | None = None) -> str:
    """Rebuild data/summary.md from what's in data/. Never raises."""
    path = path or SUMMARY_FILE
    try:
        events, stats, applications = load_all()
        text = render(events, stats, applications, max_rent, rules=rules)
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
    top = text.split("\n## Units")[0]  # down to the dream units
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(top + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    import auto_apply

    print(write(auto_apply.MAX_RENT, rules=auto_apply.RENT_RULES))
