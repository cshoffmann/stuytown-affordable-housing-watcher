"""
Which way an apartment faces and how much direct sun it gets, from the sun
map (data/sun/unit_lines.json, built by tools/sun_map/build.py; the
interactive map is data/sun/sun_map.html).

    import unit_sun
    unit_sun.lookup(unit)                       # a unit from the listings API
    unit_sun.lookup("287 AVENUE C", "07-F")     # or an address and apartment
    unit_sun.describe(unit)  # 'Sun 84/100, top 2% on floor 7, #1 of 8 here · faces ESE + SSW (sure) · mornings: strong ...'
    unit_sun.short(unit)     # '84 top 2% · ESE+SSW' for a table cell

Sun score, 0-100: how sunny the apartment is all year, all day, with
mornings counting more: morning + 0.75 x afternoon + a bonus for having
both, in "full-sun hours" (sun straight in counts fully, sun skimming the
wall barely counts; October-March count 1.5x; courtyard trees shade floors
1-5, more in summer). 100 = the sunniest apartments in the complex. "Top
2%" compares it with every apartment line on the same floor.

An apartment number is floor + line letter (07-F: floor 7, line F); every
apartment with the same letter in a building sits in the same spot of the
floor, so faces the same way. How sure the direction is:
    sure        the building's own floor plan, a building of the same design
                confirmed by listings, or a line you confirmed
                (tools/sun_map/inputs/line_notes.json)
    likely      one direction has 80%+ of the odds
    either way  two answers are about as good (e.g. north side or south side)
    unknown     nothing to go on yet
When it isn't sure, the score is the odds-weighted average, and the lookup
also gives the worst and best case and the chance of real morning sun.
Hours: plain hours of direct sun on the average day (21st of every month),
before and after noon, plus Dec 21, the shortest day.

Standard library only and never raises (None / "" when it doesn't know), so
it's safe anywhere, auto-apply included. refresh_market_evidence() saves the
market-rate listings once a morning: their view tags and layouts are what
the sun map learns from when it's rebuilt.
"""

import functools
import gzip
import json
import os
import re
import urllib.request
from datetime import datetime, timezone

LINES_FILE = "data/sun/unit_lines.json"
MARKET_FILE = "data/sun/market_seen.json"
MARKET_URL = ("https://units.stuytown.com/api/units?Order=low-price&page=0&itemsOnPage=1000"
              "&PropertyName=Stuyvesant+Town_Peter+Cooper+Village")
COMPLEXES = ("Stuyvesant Town", "Peter Cooper Village")
COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


@functools.cache
def _load_cached(path: str, mtime: float) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load(path: str = LINES_FILE) -> dict:
    try:
        return _load_cached(path, os.path.getmtime(path))
    except (OSError, ValueError):
        return {}


def normalize_address(text) -> str:
    """'287 Avenue C' / '287 AVENUE  C' -> '287 AVENUE C' (how the listings API writes it)."""
    return re.sub(r"\s+", " ", str(text or "")).strip().upper()


def parse_apartment(name) -> tuple:
    """'07-F' -> (7, 'F'); '0M-D' (Main level) -> (0, 'D'); '0T-A' (Terrace) -> (-1, 'A')."""
    floor, _, line = str(name or "").strip().upper().partition("-")
    line = line.strip() or None
    if floor.isdigit():
        return int(floor), line
    return {"0M": 0, "M": 0, "0T": -1, "T": -1}.get(floor), line


def _unit_parts(unit_or_address, apartment=None) -> tuple:
    if isinstance(unit_or_address, dict):
        unit = unit_or_address
        address = (unit.get("building") or {}).get("address") or unit.get("address")
        apartment = unit.get("name") or unit.get("unitNumber") or unit.get("apartment")
        return normalize_address(address), apartment
    return normalize_address(unit_or_address), apartment


def _faces_text(faces) -> str:
    return " + ".join(sorted(faces, key=lambda f: COMPASS.index(f) if f in COMPASS else 99)) or "?"


GOOD_MORNING_H = 3.0  # "real morning sun": at least this many hours before noon on the average day


def rank_text(pct: int) -> str:
    """Percentile among all lines on the floor -> 'top 4%' (upper half) or 'bottom 10%' (lower half)."""
    return f"top {max(1, 100 - pct)}%" if pct >= 50 else f"bottom {max(1, pct)}%"


def morning_word(hours: float) -> str:
    return "strong" if hours >= 3.5 else "some" if hours >= 1.5 else "little" if hours >= 0.5 else "none"


def lookup(unit_or_address, apartment=None, path: str = LINES_FILE) -> dict | None:
    """Everything the sun map knows about this apartment, or None."""
    try:
        address, apartment = _unit_parts(unit_or_address, apartment)
        floor, line = parse_apartment(apartment)
        building = load(path).get("addresses", {}).get(address)
        if not building or not line or line not in building.get("lines", {}):
            return None
        info = building["lines"][line]
        top = building["top_floor"]
        used_floor = min(max(floor if floor is not None else 1, 1), top)
        options = {}
        expected = [0.0, 0.0, 0.0, 0.0]
        p_good_morning = 0.0
        cases = []  # (score, weight) per spot
        for index, weight in info["spots"]:
            spot = building["spots"][index]
            sun = spot["sun"][used_floor - 1]
            for k in range(4):
                expected[k] += weight * sun[k]
            if sun[0] >= GOOD_MORNING_H:
                p_good_morning += weight
            cases.append((sun[4], weight))
            key = tuple(spot["faces"])
            option = options.setdefault(key, {"faces": list(key), "p": 0.0, "oval": spot["oval"],
                                              "street": spot["street"], "sun": [0.0] * 5})
            option["p"] += weight
            for k in range(5):
                option["sun"][k] += weight * sun[k]
        ranked = sorted(options.values(), key=lambda o: -o["p"])
        for o in ranked:
            o["sun"] = [round(v / o["p"], 1) for v in o["sun"]] if o["p"] else o["sun"]
            o["score"] = int(round(o["sun"][4]))
            o["p"] = round(o["p"], 2)
        likely_cases = [sc for sc, w in cases if w >= 0.1] or [sc for sc, _ in cases]
        score = info["score"][used_floor - 1]
        # rank among this address's lines on the same floor (1 = sunniest)
        here = sorted((ln["score"][used_floor - 1] for ln in building["lines"].values()), reverse=True)
        notes = []
        if floor is not None and floor < 1:
            notes.append("Main/Terrace level: floor 1's sun shown, the real figure is a bit lower")
        if floor is not None and floor >= top:
            notes.append("top floor")
        return {
            "address": address, "apartment": apartment, "line": line, "floor": floor, "top_floor": top,
            "top_floor_known": building.get("top_floor_known", False), "complex": building.get("complex"),
            "confidence": info["confidence"], "options": ranked,
            "faces": ranked[0]["faces"] if ranked else [],
            "p_faces": info.get("p_faces", {}), "p_oval": info.get("p_oval", 0.0), "p_street": info.get("p_street", 0.0),
            "street": sorted({st for o in ranked for st in o["street"]}) if info.get("p_street", 0) >= 0.3 else [],
            # chance each street is outside one of its windows: {"First Ave": 0.5}
            "p_streets": {st: round(sum(o["p"] for o in ranked if st in o["street"]), 2)
                          for st in sorted({st for o in ranked for st in o["street"]})},
            "sun_score": score, "percentile": info["pct"][used_floor - 1],
            "top_percent": max(1, 100 - info["pct"][used_floor - 1]),
            "rank_text": rank_text(info["pct"][used_floor - 1]),
            "rank_in_building": here.index(score) + 1, "lines_in_building": len(here),
            "worst_score": int(min(likely_cases)), "best_score": int(max(likely_cases)),
            "p_good_morning": round(p_good_morning, 2),
            "morning_h": round(expected[0], 1), "afternoon_h": round(expected[1], 1),
            "dec21_h": round(expected[2], 1), "dec21_morning_h": round(expected[3], 1),
            "mornings": morning_word(expected[0]),
            "source": info.get("source", ""), "notes": notes,
        }
    except Exception:
        return None


def _oval_text(p: float) -> str:
    if p >= 0.8:
        return "Oval view"
    if p >= 0.3:
        return f"maybe an Oval view ({p:.0%})"
    return ""


def _faces_phrase(info) -> str:
    opts = info["options"]
    if opts[0]["p"] >= 0.8 or len(opts) == 1:
        return f"faces {_faces_text(opts[0]['faces'])} ({info['confidence']})"
    if sum(o["p"] for o in opts[:2]) >= 0.75:
        return "faces " + " or ".join(f"{_faces_text(o['faces'])} ({o['p']:.0%})" for o in opts[:2])
    top = sorted(info["p_faces"].items(), key=lambda kv: -kv[1])[:3]  # spread over several spots
    return "windows face " + ", ".join(f"{f} {p:.0%}" for f, p in top) + f" ({info['confidence']})"


def describe(unit_or_address, apartment=None, path: str = LINES_FILE) -> str:
    """One line for alerts and the summary, or '' if the sun map doesn't know the apartment:
    'Sun 84/100, top 2% on floor 7, #1 of 8 here · faces ESE + SSW (sure) · mornings: strong
    (4.3 h), afternoons 4.9 h, Dec 21 4 h'."""
    info = lookup(unit_or_address, apartment, path)
    if not info or not info["options"]:
        return ""
    floor_word = f"floor {info['floor']}" if info["floor"] and info["floor"] > 0 else "the lowest floor"
    head = (f"Sun {info['sun_score']}/100, {info['rank_text']} on {floor_word}, "
            f"#{info['rank_in_building']} of {info['lines_in_building']} here")
    if info["best_score"] - info["worst_score"] >= 15:
        head += f" (could be {info['worst_score']}-{info['best_score']})"
    bits = [head, _faces_phrase(info)]
    oval = _oval_text(info["p_oval"])
    if oval:
        bits.append(oval)
    if info["street"]:
        where = "onto " + ", ".join(info["street"])
        bits.append(where if info["p_street"] >= 0.8 else f"maybe {where} ({info['p_street']:.0%})")
    morning = f"mornings: {info['mornings']} ({info['morning_h']:g} h)"
    if 0.1 < info["p_good_morning"] < 0.9:
        morning += f", {info['p_good_morning']:.0%} chance of {GOOD_MORNING_H:g} h+"
    bits.append(f"{morning}, afternoons {info['afternoon_h']:g} h, Dec 21 {info['dec21_h']:g} h")
    return " · ".join(bits)


def short(unit_or_address, apartment=None, path: str = LINES_FILE) -> str:
    """'84 top 2% · ESE+SSW Oval · 4.3/4.9 h' (score, rank on its floor, faces, morning/afternoon hours);
    '?' marks a direction that's a guess; '' if unknown."""
    info = lookup(unit_or_address, apartment, path)
    if not info or not info["options"]:
        return ""
    top = info["options"][0]
    faces = "+".join(top["faces"]) or "?"
    if top["p"] < 0.8:
        faces = f"{faces}? ({top['p']:.0%})"
    oval = " Oval" if info["p_oval"] >= 0.8 else (" Oval?" if info["p_oval"] >= 0.3 else "")
    return (f"{info['sun_score']} {info['rank_text']} · {faces}{oval} · "
            f"{info['morning_h']:g}/{info['afternoon_h']:g} h")


# ------------------------------------------------------------------ evidence for the next rebuild


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def market_record(unit: dict, now: str) -> dict | None:
    building = unit.get("building") or {}
    if (unit.get("property") or {}).get("name") not in COMPLEXES or not building.get("address"):
        return None
    plan = os.path.basename(str((unit.get("floorplan") or {}).get("floorplanSvg") or ""))
    try:
        lat, lon = float(building.get("latitude")), float(building.get("longitude"))
    except (TypeError, ValueError):
        lat = lon = None
    return {"unitSpk": unit.get("unitSpk"), "address": normalize_address(building["address"]),
            "apt": unit.get("unitNumber") or unit.get("name"), "beds": unit.get("bedrooms"), "sqft": unit.get("sqft"),
            "price": unit.get("price"), "layout": plan.rsplit(".", 1)[0] if plan else "",
            "codes": sorted({str(a.get("code")) for a in unit.get("amenities") or [] if isinstance(a, dict) and a.get("code")}),
            "lat": lat, "lon": lon, "first_seen_utc": now, "last_seen_utc": now}


def merge_market(saved: dict, units: list, now: str | None = None) -> tuple:
    """Add today's market listings to the saved ones (keyed by unitSpk). Returns (data, new count)."""
    now = now or _now()
    rows = {r.get("unitSpk") or f"{r.get('address')}|{r.get('apt')}": r for r in saved.get("units", [])}
    new = 0
    for unit in units:
        rec = market_record(unit, now)
        if not rec:
            continue
        key = rec["unitSpk"] or f"{rec['address']}|{rec['apt']}"
        if key in rows:
            rec["first_seen_utc"] = rows[key].get("first_seen_utc") or now
        else:
            new += 1
        rows[key] = rec
    data = dict(saved)
    data.setdefault("about", "Market-rate StuyTown / Peter Cooper Village listings seen on units.stuytown.com: "
                             "evidence for which way each apartment line faces (view tags, layouts, sizes). "
                             "Saved after each morning by unit_sun.refresh_market_evidence().")
    data["units"] = sorted(rows.values(), key=lambda r: (r.get("address") or "", r.get("apt") or ""))
    return data, new


def refresh_market_evidence(timeout: float = 20, path: str | None = None) -> str:
    """Save today's market-rate listings to data/sun/market_seen.json. One request; never raises."""
    path = path or MARKET_FILE
    try:
        req = urllib.request.Request(MARKET_URL, headers={
            "User-Agent": "Mozilla/5.0 (compatible; personal-housing-watcher/1.0)", "Accept": "application/json",
            "Accept-Encoding": "gzip", "Origin": "https://www.stuytown.com", "Referer": "https://www.stuytown.com/"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
        units = json.loads(body).get("unitModels") or []
        try:
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            saved = {}
        data, new = merge_market(saved, units)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=0, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
        return f"sun map evidence: {len(data['units'])} market listings saved ({new} new)"
    except Exception as e:
        return f"sun map evidence: not refreshed ({type(e).__name__}: {e})"


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(json.dumps(lookup(" ".join(sys.argv[1:-1]), sys.argv[-1]), indent=1))
        print(describe(" ".join(sys.argv[1:-1]), sys.argv[-1]))
    else:
        print("usage: python unit_sun.py 287 AVENUE C 07-F")
