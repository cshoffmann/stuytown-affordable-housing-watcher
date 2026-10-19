"""
Dream units: which listings are the ones you'd move for, and why. Shown on
the summary page, on the map and in the alert -- never acted on: auto-apply
only follows AUTO_APPLY_RULES.

A dream unit is all four:

    rent   at or under DREAM_RULES for its bedrooms (default "1:2200, 2:2700")
    floor  in DREAM_FLOORS (default "5-10")
    Oval   listed with Oval views (feature code VBOV)
    sun    its Oval windows face E, ESE, SE, SSE, S, SSW or SW: a building
           on the north side of the Oval

Ratings, best first:

    dream           all four
    possible dream  all four, except it isn't certain which way the windows
                    face: the building looks at the Oval two ways, one sunny
    almost          one of the four missing (it says which); rent may be up
                    to 15% over
    great light     rent and floor fit, and its windows are known to face
                    E-SW and not onto a street (Peter Cooper Village, or Stuy
                    Town away from the Oval)
    price match     rent fits; nothing else known to be dreamy

Which way a unit's windows face comes from, first that knows: your own notes
(data/reference/line_overrides.json); the building's walls that look at the
Oval (data/reference/buildings.json), when the unit or another on its line
is listed with Oval views; or the building's position round the Oval.
Apartments with the same letter (06-E, 09-E) are stacked on top of each other,
so what's learnt about one is kept for its whole line in
data/apartment_atlas.json.
"""

import functools
import json
import os
import re

import check_units
import reference_data
import stuytown_geo as geo

DEFAULT_DREAM_RULES = "1:2200, 2:2700"
DEFAULT_DREAM_FLOORS = "5-10"
ALMOST_OVER = 0.15  # "almost" allows rent this much over the dream rent
OVAL_VIEWS = "VBOV"
ATLAS_FILE = "data/apartment_atlas.json"
OVERRIDES_FILE = "data/reference/line_overrides.json"
TIERS = ("dream", "possible dream", "almost", "great light", "price match")

# Feature codes the listings use, in short. Floor (FS..) and finish (z...)
# codes are left out of feature lists: they have their own columns.
FEATURE_NAMES = {
    "VBOV": "Oval views", "OKU": "open kitchen", "PBGK": "galley kitchen", "DW18": "dishwasher",
    "IBSS": "stainless appliances", "NFF": "wood floors", "MAVM": "renovated bath", "ELFA": "closet system",
    "PBAC": "extra closet", "ATW2": "wall A/C", "ATW3": "3 wall A/Cs", "PS40": "PS 40 zone",
}


def parse_floors(text: str) -> tuple:
    """"5-10" -> ({5, 6, 7, 8, 9, 10}, []); "5-10, 12" adds 12. The second item
    lists what couldn't be read."""
    floors, problems = set(), []
    for part in re.split(r"[,;\s]+", re.sub(r"\s*(?:-|\bto\b)\s*", "-", text or "", flags=re.IGNORECASE)):
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            problems.append(part)
            continue
        low, high = sorted((int(match[1]), int(match[2] or match[1])))
        floors.update(range(low, high + 1))
    return floors, problems


def floors_text(floors) -> str:
    """{5, 6, 7, 12} -> '5-7, 12'."""
    runs, ordered = [], sorted(floors)
    for f in ordered:
        if runs and f == runs[-1][1] + 1:
            runs[-1][1] = f
        else:
            runs.append([f, f])
    return ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in runs)


DREAM_RENT, RULE_PROBLEMS = check_units.parse_rules(os.environ.get("DREAM_RULES", "").strip() or DEFAULT_DREAM_RULES)
if not DREAM_RENT:
    DREAM_RENT = check_units.parse_rules(DEFAULT_DREAM_RULES)[0]
DREAM_FLOORS, FLOOR_PROBLEMS = parse_floors(os.environ.get("DREAM_FLOORS", "").strip() or DEFAULT_DREAM_FLOORS)
if not DREAM_FLOORS:
    DREAM_FLOORS = parse_floors(DEFAULT_DREAM_FLOORS)[0]


def settings_text() -> str:
    """'1 bed <= $2,200, 2 bed <= $2,700; floors 5-10'"""
    rents = ", ".join(f"{'studio' if b == 0 else f'{b} bed'} <= ${r:,.0f}" for b, r in sorted(DREAM_RENT.items()))
    return f"{rents}; floors {floors_text(DREAM_FLOORS)}"


def startup_warnings() -> list:
    found = []
    if RULE_PROBLEMS:
        found.append(f"DREAM_RULES: couldn't read {', '.join(RULE_PROBLEMS)!r} (write it like 1:2200, 2:2700)")
    if FLOOR_PROBLEMS:
        found.append(f"DREAM_FLOORS: couldn't read {', '.join(FLOOR_PROBLEMS)!r} (write it like 5-10)")
    return found


# ------------------------------------------------------- reference data


def codes(unit: dict) -> list:
    return [str(a.get("code")) for a in unit.get("amenities") or [] if isinstance(a, dict) and a.get("code")]


def finish_name(unit: dict) -> str:
    """'Classic' -- the API gives a name, or (in some answers) {"name": "Classic", ...}."""
    finish = unit.get("finish")
    if isinstance(finish, dict):
        finish = finish.get("name") or finish.get("searchCode")
    return str(finish or "")


def features(unit: dict) -> list:
    """['Oval views', 'dishwasher', ...]: the unit's features, short, without floor/finish codes."""
    found = []
    for a in unit.get("amenities") or []:
        code = str(a.get("code") or "")
        if re.fullmatch(r"FS\w\w", code) or code.startswith("z"):
            continue
        found.append(FEATURE_NAMES.get(code) or str(a.get("friendlyDescription") or code))
    return found


@functools.cache
def _buildings_cached(path: str, mtime: float) -> dict | None:
    data = reference_data.read_json(path)
    if not data:
        return None
    for fp in data.get("footprints", []):
        fp["_ring"] = [geo.to_xy(lat, lon) for lat, lon in fp.get("outline") or []]
    return data


def load_buildings() -> dict | None:
    path = reference_data.BUILDINGS_FILE
    try:
        return _buildings_cached(path, os.path.getmtime(path))
    except OSError:
        return None


def load_overrides() -> dict:
    data = reference_data.read_json(OVERRIDES_FILE, {}) or {}
    return {key.upper(): value for key, value in (data.get("lines") or {}).items()
            if isinstance(value, dict) and not key.upper().startswith("EXAMPLE")}


def building_address(unit: dict) -> str:
    return reference_data.normalize_address((unit.get("building") or {}).get("address"))


def line_key(unit: dict) -> str | None:
    """'18 STUYVESANT OVAL|E' -- every apartment on that line faces the same way."""
    letter = geo.line_letter(unit)
    address = building_address(unit)
    return f"{address}|{letter}" if letter and address else None


def position(unit: dict) -> tuple | None:
    b = unit.get("building") or {}
    lat, lon = check_units.number(b.get("latitude")), check_units.number(b.get("longitude"))
    return (lat, lon) if lat is not None and lon is not None else None


def find_footprint(unit: dict, buildings: dict | None):
    """(footprint, its walls on this unit's side) from buildings.json, or (None, [])."""
    if not buildings:
        return None, []
    address = building_address(unit)
    where = position(unit)
    match = next((fp for fp in buildings.get("footprints", []) if address and address in fp.get("addresses", [])), None)
    if match is None and where:
        xy = geo.to_xy(*where)
        match = next((fp for fp in buildings.get("footprints", []) if fp.get("_ring")
                      and reference_data._inside(xy, fp["_ring"])), None)
        if match is None:
            near = sorted(buildings.get("footprints", []), key=lambda fp: geo.distance_m(*where, *fp["center"]))
            if near and geo.distance_m(*where, *near[0]["center"]) < 45:
                match = near[0]
    if match is None:
        return None, []
    walls = match.get("walls", [])
    if len(match.get("addresses", [])) > 1:
        own = [w for w in walls if w.get("address") == address]
        if not own and where:
            own = [w for w in walls if geo.distance_m(*where, *_middle(w)) < 40]
        walls = own or walls
    return match, walls


def _middle(wall) -> tuple:
    (lat1, lon1), (lat2, lon2) = wall["line"]
    return (lat1 + lat2) / 2, (lon1 + lon2) / 2


def sunny(wall: dict) -> bool:
    """Does this wall face E-SW? Worked out from its direction each time, so a
    change to what counts never needs the buildings fetched again."""
    return wall.get("facing") is not None and geo.is_good_facing(wall["facing"])


GOOD_LIGHT_HOURS = 5  # winter sun at floor 8 for a wall to count as good light


def quiet_and_sunny(wall: dict) -> bool:
    """Faces E-SW, not onto a street, and not shaded much by the next building."""
    return (sunny(wall) and not wall.get("street")
            and (wall.get("winter_sun_h") or {}).get("8", 0) >= GOOD_LIGHT_HOURS)


def oval_center(buildings: dict | None) -> tuple:
    center = ((buildings or {}).get("oval") or {}).get("center")
    return tuple(center) if center else geo.OVAL_CENTER


def building_info(unit: dict, buildings: dict | None = None) -> dict:
    """Zone, Oval side, walls and the walk to the L for a unit's building."""
    buildings = buildings if buildings is not None else load_buildings()
    where = position(unit)
    complex_name = geo.complex_of(unit)
    footprint, walls = find_footprint(unit, buildings)
    info = {"complex": complex_name, "address": building_address(unit), "footprint": footprint["id"] if footprint else None}
    if where:
        info["zone"] = footprint["zone"] if footprint and footprint.get("zone") else geo.zone(
            *where, info["address"], complex_name)
        info["walk_to_l"] = geo.walk_to_l(*where)
        if complex_name == "Stuy Town":
            info["oval_side"] = geo.oval_side(*where, oval_center(buildings))
    else:
        info["zone"] = "Oval" if "OVAL" in info["address"] else "unknown"
    info["oval_walls"] = sorted({w["compass"] for w in walls if w.get("faces_oval")})
    info["street_walls"] = sorted({f"{w['compass']} ({w['street']})" for w in walls if w.get("street")})
    info["quiet_sunny_walls"] = sorted({w["compass"] for w in walls if quiet_and_sunny(w)})
    return info


# ------------------------------------------------------- which way it faces


def _bearing_of(name) -> float | None:
    name = str(name).strip().upper()
    if name in geo.COMPASS:
        return geo.COMPASS.index(name) * 22.5
    value = check_units.number(name)
    return value % 360 if value is not None else None


def facing(unit: dict, buildings: dict | None = None, atlas: dict | None = None,
           overrides: dict | None = None) -> dict:
    """Which way the unit's windows face, as far as anything knows:
    {"bearings": [209.0], "compass": ["SSW"], "certain": True, "source": "...",
     "street": False | None, "winter_sun_h": 8.8 | None}."""
    buildings = buildings if buildings is not None else load_buildings()
    overrides = overrides if overrides is not None else load_overrides()
    key = line_key(unit)
    note = overrides.get(key) if key else None
    if note and note.get("facing"):
        bearings = [b for b in map(_bearing_of, note["facing"] if isinstance(note["facing"], list)
                                   else [note["facing"]]) if b is not None]
        if bearings:
            return _facing(bearings, True, "your notes (line_overrides.json)", note.get("street"), None)

    line = (atlas or {}).get("lines", {}).get(key or "", {})
    oval_views = OVAL_VIEWS in codes(unit) or (line.get("oval_views") or {}).get("yes", 0) > 0
    if not oval_views:
        return _facing([], False, "unknown: not listed with Oval views", None, None)
    via = "Oval views" if OVAL_VIEWS in codes(unit) else f"Oval views on its line ({key.split('|')[1]})"
    _, walls = find_footprint(unit, buildings)
    oval_walls = [w for w in walls if w.get("faces_oval")]
    if oval_walls:
        directions = {}
        for w in oval_walls:
            directions.setdefault(w["compass"], w)
        bearings = [w["facing"] for w in directions.values()]
        floor = str(_nearest_sun_floor(geo.floor_number(unit)))
        sun = max((w.get("winter_sun_h") or {}).get(floor, 0) for w in directions.values())
        street = any(w.get("street") for w in directions.values())
        return _facing(bearings, len(bearings) == 1, f"{via} + the building's Oval walls (OpenStreetMap)",
                       street, sun)
    where = position(unit)
    if not where:
        return _facing([], False, f"{via}, but the building's position isn't listed", None, None)
    toward = geo.oval_side(*where, oval_center(buildings))["faces"]  # the ring buildings face the Oval's middle
    return _facing([toward], True, f"{via} + the building's position (estimated)", False, None)


def _nearest_sun_floor(floor) -> int:
    floors = reference_data.SUN_FLOORS
    return min(floors, key=lambda f: abs(f - (floor or 1)))


def _facing(bearings, certain, source, street, sun) -> dict:
    return {"bearings": [round(b, 1) for b in bearings], "compass": [geo.compass(b) for b in bearings],
            "certain": bool(certain and bearings), "source": source, "street": street,
            "winter_sun_h": sun if sun is not None else (max(geo.sun_hours(b) for b in bearings) if bearings else None)}


# ------------------------------------------------------------------ rating


def rate(unit: dict, buildings: dict | None = None, atlas: dict | None = None,
         overrides: dict | None = None) -> dict:
    """{"tier": "dream" | ... | None, "checks": {...}, "why": "...", "facing": {...}}"""
    buildings = buildings if buildings is not None else load_buildings()
    beds = check_units.number(unit.get("bedrooms"))
    rent = check_units.unit_rent(unit)
    floor = geo.floor_number(unit)
    limit = DREAM_RENT.get(int(beds)) if beds is not None else None
    face = facing(unit, buildings, atlas, overrides)
    good = [geo.is_good_facing(b) for b in face["bearings"]]
    sun = True if good and all(good) else ("maybe" if any(good) else (False if good else None))
    checks = {
        "rent": rent is not None and limit is not None and rent <= limit,
        "floor": floor in DREAM_FLOORS,
        "oval_views": OVAL_VIEWS in codes(unit),
        "sun": sun,
    }
    misses = []
    if not checks["rent"]:
        misses.append("no dream rent for this size" if limit is None else
                      "rent isn't listed" if rent is None else f"rent ${rent:,.0f} is over ${limit:,.0f}")
    if not checks["floor"]:
        misses.append(f"floor {floor if floor is not None else '?'} isn't {floors_text(DREAM_FLOORS)}")
    if not checks["oval_views"]:
        misses.append("no Oval views listed")
    if sun is not True:
        misses.append({"maybe": f"its windows face {' or '.join(face['compass'])}",
                       False: f"its windows face {'/'.join(face['compass'])}",
                       None: "which way it faces isn't known"}[sun])

    hours = f" (~{face['winter_sun_h']:g} h of winter sun)" if face.get("winter_sun_h") else ""
    faces = "/".join(face["compass"])
    tier, why = None, ""
    if limit is not None and rent is not None:
        if all(v is True for v in checks.values()):
            tier, why = "dream", f"Oval views, faces {faces}{hours}, floor {floor}"
        elif checks["rent"] and checks["floor"] and checks["oval_views"] and sun == "maybe":
            tier, why = "possible dream", (f"Oval views, floor {floor}; its Oval windows face "
                                           f"{' or '.join(face['compass'])} -- check the floorplan")
        elif (checks["rent"] and checks["floor"] and sun is True and face["certain"]
              and face.get("street") is False):
            tier, why = "great light", f"faces {faces}{hours}, not onto a street"
        elif len(misses) == 1 and (checks["rent"] or rent <= limit * (1 + ALMOST_OVER)):
            tier, why = "almost", misses[0]
        elif checks["rent"]:
            tier, why = "price match", "; ".join(misses)
    return {"tier": tier, "why": why, "checks": checks, "misses": misses, "facing": face,
            "dream_rent": limit, "floor": floor}


def alert_note(unit: dict) -> str:
    """One line for the alert, or '' -- e.g. '* Dream unit: Oval views, faces SSW ...'."""
    try:
        rating = rate(unit, atlas=load_atlas())
    except Exception:
        return ""
    label = ALERT_LABELS.get(rating["tier"])
    return f"* {label}: {rating['why']}" if label else ""


ALERT_LABELS = {"dream": "Dream unit", "possible dream": "Possible dream unit", "almost": "Almost a dream unit",
                "great light": "Great light"}


# ------------------------------------------------------------------- atlas


def build_atlas(events: list, buildings: dict | None = None, overrides: dict | None = None) -> dict:
    """Every line of apartments seen (building + letter): its layout, size,
    floors and rents seen, Oval views, and which way it faces."""
    buildings = buildings if buildings is not None else load_buildings()
    overrides = overrides if overrides is not None else load_overrides()
    lines = {}
    for event in events:
        if event.get("event") != "new":
            continue
        for summary in event.get("units", []):
            unit = summary.get("data") or {}
            key = line_key(unit)
            if not key:
                continue
            line = lines.setdefault(key, {
                "building": building_address(unit), "line": key.split("|")[1], "complex": geo.complex_of(unit),
                "layouts": {}, "bedrooms": unit.get("bedrooms"), "sqft": unit.get("sqft"),
                "oval_views": {"yes": 0, "no": 0}, "seen": []})
            layout = str((unit.get("style") or {}).get("name") or "?")
            line["layouts"][layout] = line["layouts"].get(layout, 0) + 1
            line["oval_views"]["yes" if OVAL_VIEWS in codes(unit) else "no"] += 1
            line["seen"].append({"apartment": unit.get("name"), "floor": geo.floor_number(unit),
                                 "rent": check_units.unit_rent(unit), "finish": finish_name(unit),
                                 "first_seen_utc": event.get("detected_at_utc")})
    atlas = {"about": "Each line of apartments seen (same building, same letter = stacked, facing the same way). "
                      "Built by dream_units.py from data/events.json; your notes go in "
                      "data/reference/line_overrides.json.",
             "lines": lines}
    for key, line in lines.items():
        latest = _latest_unit(events, key)
        if latest is None:
            continue
        face = facing(latest, buildings, atlas, overrides)
        line["facing"] = face["compass"]
        line["facing_certain"] = face["certain"]
        line["facing_from"] = face["source"]
        line["looks_onto_street"] = face["street"]
        line["winter_sun_h"] = face["winter_sun_h"]
        line["seen"] = line["seen"][-20:]
    return atlas


def _latest_unit(events, key):
    for event in reversed(events):
        if event.get("event") != "new":
            continue
        for summary in event.get("units", []):
            unit = summary.get("data") or {}
            if line_key(unit) == key:
                return unit
    return None


@functools.cache
def _atlas_cached(path: str, mtime: float) -> dict:
    return reference_data.read_json(path, {}) or {}


def load_atlas() -> dict:
    try:
        return _atlas_cached(ATLAS_FILE, os.path.getmtime(ATLAS_FILE))
    except OSError:
        return {}


def write_atlas(events: list | None = None) -> dict:
    events = events if events is not None else reference_data.read_json(check_units.EVENTS_FILE, []) or []
    atlas = build_atlas(events)
    reference_data.write_json(ATLAS_FILE, atlas)
    ensure_overrides_file()
    return atlas


def ensure_overrides_file() -> None:
    """Create the notes file, with an example, the first time."""
    if os.path.exists(OVERRIDES_FILE):
        return
    reference_data.write_json(OVERRIDES_FILE, {
        "about": ("Your own notes on which way a line of apartments faces -- after a viewing, or from a "
                  "floorplan. The key is 'BUILDING ADDRESS|LINE LETTER' as the listings write them. facing: "
                  "compass directions of its windows (N, NNE, NE, ... NNW); street: true if it looks onto a "
                  "street. These win over anything worked out. Keys starting with EXAMPLE are ignored."),
        "lines": {"EXAMPLE 17 STUYVESANT OVAL|H": {"facing": ["SSW"], "street": False,
                                                   "note": "copy this without EXAMPLE, then edit"}},
    })


if __name__ == "__main__":
    atlas = write_atlas()
    print(json.dumps({k: {f: v.get(f) for f in ("facing", "facing_from", "oval_views")}
                      for k, v in atlas["lines"].items()}, indent=1))
