"""
Reference data for the map and the dream-unit ratings, fetched where there's
internet (GitHub's runner) and committed under data/:

    data/reference/buildings.json   every Stuy Town and Peter Cooper Village
        building from OpenStreetMap: its outline and addresses, and each
        wall's direction -- does it look at the Oval, at a street, or at
        another building how far away, and how many hours of winter sun it
        gets by floor (stuytown_geo.py's sun model)
    data/floorplans/   the floorplan of every layout seen in a listing: the
        SVG the site shows, a PNG copy, and index.json (which layout, its
        size, the room labels read off the drawing)

The watcher refreshes both after the morning's checks, once the applier has
stopped, so none of this can ever slow an application down; the workflow's
"refresh_map" button does it on demand. Nothing here raises: without
reference data the map and the ratings simply know less.

    python reference_data.py               # fetch what's missing
    python reference_data.py --buildings   # also re-fetch the buildings from OpenStreetMap

Map data (c) OpenStreetMap contributors, available under the Open Database
License (openstreetmap.org/copyright).
"""

import argparse
import base64
import gzip
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import apply_recorder
import check_units
import stuytown_geo as geo

BUILDINGS_FILE = "data/reference/buildings.json"
FLOORPLANS_DIR = "data/floorplans"
FLOORPLAN_HOSTS = [
    "https://affordable-housing.stuytown.com",  # where the site's own page fetches them
    "https://uxp-pd-cms-gmfqdagrdcdrd0gk.a02.azurefd.net",  # its media CDN
    "https://www.stuytown.com",
]
FLOORPLAN_PNG_WIDTH = 1000
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
ATTRIBUTION = "Map data (c) OpenStreetMap contributors, ODbL (openstreetmap.org/copyright)"
BBOX = (40.7262, -73.9852, 40.7395, -73.9698)  # south, west, north, east: both complexes and their streets
USER_AGENT = "personal-housing-watcher/1.0 (reference data, run once in a while)"

# The streets around the complexes, as OpenStreetMap names them.
STREETS = {
    "First Ave": r"^(first|1st) avenue$",
    "14th St": r"^east 14th street$",
    "20th St": r"^east 20th street$",
    "23rd St": r"^east 23rd street$",
    "Avenue C": r"^avenue c$",
    "FDR Drive": r"fdr|franklin d\.? roosevelt|east river drive",
}
# Where those streets are on the grid (stuytown_geo.to_grid) if OpenStreetMap
# doesn't say: u for the avenues, v for the streets.
STREET_POSITIONS = {"First Ave": -335.0, "Avenue C": 340.0, "14th St": -205.0, "20th St": 270.0,
                    "23rd St": 515.0, "FDR Drive": 395.0}
RAY_M = 260  # how far a wall's view is followed
STREET_NEAR_M = 85  # a street closer than this, with nothing in between, is what the window looks at
OVAL_NEAR_M = 230
MIN_WALL_M = 6  # shorter wall pieces are corners and jogs, not where windows are
SUN_FLOORS = (2, 5, 8, 11)  # winter sun on each wall is worked out for these floors


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, one_per_line: str | None = None) -> None:
    """Write atomically. one_per_line: a top-level list written one item per
    line, so a big file stays small and its diffs readable."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if one_per_line and isinstance(data.get(one_per_line), list):
            rest = {k: v for k, v in data.items() if k != one_per_line}
            f.write(json.dumps(rest, indent=1, sort_keys=True, ensure_ascii=False)[:-2] + f',\n "{one_per_line}": [\n')
            f.write(",\n".join(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in data[one_per_line]))
            f.write("\n ]\n}\n")
        else:
            json.dump(data, f, indent=1, sort_keys=True, ensure_ascii=False)
            f.write("\n")
    os.replace(tmp, path)


def normalize_address(text) -> str:
    """'405 East 14th Street' / '330 1st Avenue' -> '405 EAST 14TH STREET' / '330 FIRST AVENUE',
    the way the listings API writes them."""
    text = re.sub(r"\s+", " ", str(text or "").upper()).strip()
    text = re.sub(r"\b1ST AVE(NUE)?\b", "FIRST AVENUE", text)
    text = re.sub(r"\bAVE\b\.?", "AVENUE", text)
    text = re.sub(r"\bST\b\.?$", "STREET", text)
    text = re.sub(r"\bE\.? (?=\d)", "EAST ", text)
    return text


# ------------------------------------------------------- OpenStreetMap


def overpass_query(bbox=BBOX) -> str:
    s, w, n, e = bbox
    box = f"({s},{w},{n},{e})"
    return f"""[out:json][timeout:90];
(
  way["building"]{box};
  relation["building"]{box};
  way["highway"]["name"]{box};
  node["addr:housenumber"]{box};
  nwr["name"~"Stuyvesant|Peter Cooper",i]{box};
);
out tags geom;"""


def fetch_osm(timeout: float = 90) -> dict:
    """The raw Overpass answer for the area, trying each mirror within
    `timeout` seconds in all. Raises on failure (refresh() catches)."""
    body = urllib.parse.urlencode({"data": overpass_query()}).encode()
    deadline = time.monotonic() + timeout
    last = None
    for url in OVERPASS_URLS:
        left = deadline - time.monotonic()
        if left < 5:
            break
        try:
            request = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=left) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("elements"):
                return data
            last = RuntimeError(f"{url} returned no elements")
        except Exception as e:  # try the next mirror
            last = e
    raise RuntimeError(f"OpenStreetMap (Overpass) didn't answer: {last}")


def _ring(points) -> list:
    """[(x, y), ...] metres, without the repeated last point."""
    ring = [geo.to_xy(p["lat"], p["lon"]) for p in points if p]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring.pop()
    return ring


def _area(ring) -> float:
    """Signed area (positive = anticlockwise)."""
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1])) / 2


def _centroid(ring) -> tuple:
    a = _area(ring)
    if abs(a) < 1e-9:
        return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    cx = sum((x1 + x2) * (x1 * y2 - x2 * y1) for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))
    cy = sum((y1 + y2) * (x1 * y2 - x2 * y1) for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))
    return cx / (6 * a), cy / (6 * a)


def _inside(point, ring) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _hull(points) -> list:
    """Convex hull, anticlockwise (monotone chain)."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _outer_rings(element) -> list:
    if element["type"] == "way":
        return [_ring(element.get("geometry") or [])]
    if element["type"] == "relation":
        return [_ring(m.get("geometry") or []) for m in element.get("members", [])
                if m.get("role") == "outer" and m.get("geometry")]
    return []


def _floors(tags, complex_name) -> int:
    for key in ("building:levels", "levels"):
        n = check_units.number(tags.get(key))
        if n:
            return int(n)
    height = check_units.number(str(tags.get("height") or "").replace("m", ""))
    if height:
        return max(1, round(height / geo.FLOOR_HEIGHT_M))
    return 15 if complex_name == "Peter Cooper Village" else geo.DEFAULT_BUILDING_FLOORS


def _to_grid_xy(x, y) -> tuple:
    lat, lon = geo.from_xy(x, y)
    return geo.to_grid(lat, lon)


def _latlon(point, digits=6) -> list:
    lat, lon = geo.from_xy(*point)
    return [round(lat, digits), round(lon, digits)]


def parse_osm(data: dict) -> dict:
    """Footprints, address points, streets, the Oval and the complexes' outlines, in metres."""
    footprints, addresses, streets, oval_roads, areas = [], [], {}, [], {}
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name = str(tags.get("name") or "")
        if el["type"] == "node" and tags.get("addr:housenumber"):
            addresses.append({"address": normalize_address(f"{tags['addr:housenumber']} {tags.get('addr:street', '')}"),
                              "xy": geo.to_xy(el["lat"], el["lon"])})
            continue
        if "highway" in tags and el["type"] == "way":
            line = [geo.to_xy(p["lat"], p["lon"]) for p in el.get("geometry") or [] if p]
            if re.search(r"stuyvesant oval", name, re.I):
                oval_roads.extend(line)
            for street, pattern in STREETS.items():
                if re.search(pattern, name, re.I):
                    streets.setdefault(street, []).append(line)
            continue
        if re.search(r"stuyvesant town|peter cooper village", name, re.I) and "building" not in tags:
            rings = [r for r in _outer_rings(el) if len(r) >= 3]
            if rings:
                key = "Peter Cooper Village" if "peter" in name.lower() else "Stuy Town"
                areas.setdefault(key, []).extend(rings)
        if re.search(r"\boval\b", name, re.I) and ("leisure" in tags or "landuse" in tags) and el["type"] != "node":
            rings = [r for r in _outer_rings(el) if len(r) >= 3]
            if rings:
                areas.setdefault("Oval", []).extend(rings)
        if "building" in tags and el["type"] in ("way", "relation"):
            for ring in _outer_rings(el):
                if len(ring) < 3:
                    continue
                if _area(ring) < 0:
                    ring.reverse()
                own = (normalize_address(f"{tags['addr:housenumber']} {tags.get('addr:street', '')}")
                       if tags.get("addr:housenumber") else None)
                footprints.append({"id": f"{el['type']}/{el['id']}", "ring": ring, "tags": tags, "address": own})
    return {"footprints": footprints, "addresses": addresses, "streets": streets,
            "oval_roads": oval_roads, "areas": areas}


def _street_positions(streets: dict) -> dict:
    """Each street's grid position (median u for avenues, v for streets), from OSM when it can."""
    positions = dict(STREET_POSITIONS)
    for street, lines in streets.items():
        pts = [_to_grid_xy(*p) for line in lines for p in line]
        if len(pts) < 2:
            continue
        axis = 0 if street in ("First Ave", "Avenue C", "FDR Drive") else 1
        values = sorted(p[axis] for p in pts)
        median = values[len(values) // 2]
        if abs(median - STREET_POSITIONS[street]) < 120:  # a sanity check against a mismatched name
            positions[street] = median
    return positions


def _complex_for(point, parsed, positions) -> str | None:
    """Which complex a building's middle is in, or None."""
    for name in ("Stuy Town", "Peter Cooper Village"):
        rings = parsed["areas"].get(name)
        if rings and any(_inside(point, r) for r in rings):
            return name
    if parsed["areas"].get("Stuy Town") or parsed["areas"].get("Peter Cooper Village"):
        return None
    u, v = _to_grid_xy(*point)
    if not positions["First Ave"] + 15 < u:
        return None
    if positions["14th St"] + 12 < v < positions["20th St"] - 10 and u < positions["Avenue C"] - 12:
        return "Stuy Town"
    if positions["20th St"] + 10 < v < positions["23rd St"] - 12 and u < positions["FDR Drive"] - 15:
        return "Peter Cooper Village"
    return None


def _oval_ring(parsed, building_centers=(), oval_building_centers=()) -> tuple:
    """(ring in metres, where it came from). An outline from OpenStreetMap is
    used only if it's the Oval's size (about four acres) and no building
    stands inside it; otherwise a circle that stops short of the buildings
    with Oval addresses."""
    candidates = [(r, "OpenStreetMap: the Oval's lawn") for r in parsed["areas"].get("Oval") or []]
    if len(parsed["oval_roads"]) >= 6:
        candidates.append((_hull(parsed["oval_roads"]), "OpenStreetMap: the Stuyvesant Oval loop road"))
    for ring, source in sorted(candidates, key=lambda c: -abs(_area(c[0]))):
        if 6_000 < abs(_area(ring)) < 50_000 and not any(_inside(c, ring) for c in building_centers):
            return ring, source
    cx, cy = geo.to_xy(*geo.OVAL_CENTER)
    radius = geo.OVAL_RADIUS_M
    if oval_building_centers:
        radius = max(45.0, min(95.0, 0.8 * min(math.dist((cx, cy), c) for c in oval_building_centers)))
    ring = [(cx + radius * math.cos(a / 16 * math.pi), cy + radius * math.sin(a / 16 * math.pi)) for a in range(32)]
    return ring, f"estimated: a {radius:.0f} m circle round the Oval's geocoded middle"


def _segments(ring) -> list:
    return [(x1, y1, x2, y2) for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1])]


def _bbox(points) -> tuple:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def ray_hit(origin, direction, segments, max_m=RAY_M) -> float | None:
    """Distance along the ray to the nearest segment it crosses, or None."""
    ox, oy = origin
    dx, dy = direction
    best = None
    for x1, y1, x2, y2 in segments:
        ex, ey = x2 - x1, y2 - y1
        denom = dx * ey - dy * ex
        if abs(denom) < 1e-12:
            continue
        wx, wy = x1 - ox, y1 - oy
        t = (wx * ey - wy * ex) / denom
        s = (wx * dy - wy * dx) / denom
        if 0 <= s <= 1 and 1e-6 < t <= max_m and (best is None or t < best):
            best = t
    return best


def _walls(ring) -> list:
    """The footprint's walls, with neighbouring pieces facing the same way
    joined: [((x1, y1), (x2, y2), facing), ...] for an anticlockwise ring."""
    pieces = []
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 0.5:
            continue
        facing = math.degrees(math.atan2(y2 - y1, -(x2 - x1))) % 360  # outward normal (dy, -dx) as a bearing
        if pieces and geo.angle_between(pieces[-1][2], facing) < 6:
            start = pieces[-1][0]
            pieces[-1] = (start, (x2, y2), math.degrees(math.atan2(y2 - start[1], -(x2 - start[0]))) % 360)
        else:
            pieces.append(((x1, y1), (x2, y2), facing))
    if len(pieces) > 1 and geo.angle_between(pieces[0][2], pieces[-1][2]) < 6:
        start, end = pieces[-1][0], pieces[0][1]
        pieces[0] = (start, end, math.degrees(math.atan2(end[1] - start[1], -(end[0] - start[0]))) % 360)
        pieces.pop()
    return pieces


def _wall_view(a, b, facing, own_id, obstacles, oval_segments, street_lines):
    """What a wall looks out on, from rays at a quarter, half and three quarters along it."""
    nx, ny = math.sin(math.radians(facing)), math.cos(math.radians(facing))
    views = []
    for f in (0.25, 0.5, 0.75):
        origin = (a[0] + (b[0] - a[0]) * f + nx * 0.3, a[1] + (b[1] - a[1]) * f + ny * 0.3)
        end = (origin[0] + nx * RAY_M, origin[1] + ny * RAY_M)
        box = (min(origin[0], end[0]), min(origin[1], end[1]), max(origin[0], end[0]), max(origin[1], end[1]))
        building, floors_across = None, None
        for other in obstacles:
            if other["id"] == own_id:
                continue
            ob = other["bbox"]
            if ob[2] < box[0] or ob[0] > box[2] or ob[3] < box[1] or ob[1] > box[3]:
                continue
            hit = ray_hit(origin, (nx, ny), other["segments"])
            if hit is not None and (building is None or hit < building):
                building, floors_across = hit, other["floors"]
        oval = ray_hit(origin, (nx, ny), oval_segments, OVAL_NEAR_M)
        street, street_m = None, None
        for name, segments in street_lines.items():
            hit = ray_hit(origin, (nx, ny), segments, STREET_NEAR_M)
            if hit is not None and (street_m is None or hit < street_m):
                street, street_m = name, hit
        views.append({
            "building": building, "floors_across": floors_across,
            "oval": oval is not None and (building is None or oval < building),
            "street": street if street_m is not None and (building is None or street_m < building) else None,
        })
    return views


def analyze(data: dict) -> dict:
    """buildings.json from an Overpass answer."""
    parsed = parse_osm(data)
    positions = _street_positions(parsed["streets"])
    street_lines = {name: [(x1, y1, x2, y2) for line in lines for (x1, y1), (x2, y2) in zip(line, line[1:])]
                    for name, lines in parsed["streets"].items()}

    obstacles, ours = [], []
    for fp in parsed["footprints"]:
        ring = fp["ring"]
        area = abs(_area(ring))
        center = _centroid(ring)
        complex_name = _complex_for(center, parsed, positions)
        floors = _floors(fp["tags"], complex_name)
        obstacles.append({"id": fp["id"], "segments": _segments(ring), "bbox": _bbox(ring), "floors": floors})
        kind = str(fp["tags"].get("building")).lower()
        if (complex_name and area >= 250 and floors > 3  # not the shops and garages
                and kind not in ("garage", "garages", "parking", "roof", "shed", "kiosk", "retail", "commercial")):
            points = [{"address": fp["address"], "xy": center}] if fp["address"] else []
            points += [a for a in parsed["addresses"] if _inside(a["xy"], ring) and a["address"] != fp["address"]]
            ours.append((fp, complex_name, floors, center, area, points))

    oval_ring, oval_from = _oval_ring(parsed, [o[3] for o in ours],
                                      [p["xy"] for o in ours for p in o[5] if "OVAL" in (p["address"] or "")])
    oval_segments = _segments(oval_ring)
    oval_center = geo.from_xy(*_centroid(oval_ring))

    footprints = []
    for fp, complex_name, floors, center, area, points in ours:
        ring = fp["ring"]
        walls = []
        for a, b, facing in _walls(ring):
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            if length < MIN_WALL_M:
                continue
            views = _wall_view(a, b, facing, fp["id"], obstacles, oval_segments, street_lines)
            middle = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            nearest = min(points, key=lambda p: math.dist(p["xy"], middle))["address"] if points else None
            blocked = sorted(v["building"] for v in views if v["building"] is not None)
            open_m = blocked[len(blocked) // 2] if len(blocked) >= 2 else None
            across = [v["floors_across"] for v in views if v["floors_across"]]
            streets = [v["street"] for v in views if v["street"]]
            wall = {
                "facing": round(facing, 1), "compass": geo.compass(facing), "length_m": round(length, 1),
                "line": [_latlon(a), _latlon(b)], "address": nearest,
                "faces_oval": sum(v["oval"] for v in views) >= 2,
                "street": max(set(streets), key=streets.count) if len(streets) >= 2 else None,
                "open_m": round(open_m) if open_m is not None else None,
                "winter_sun_h": {str(f): geo.sun_profile(facing, f, open_m, max(across) if across else None)["winter"]
                                 for f in SUN_FLOORS} if open_m else
                {str(f): geo.sun_hours(facing) for f in SUN_FLOORS},
            }
            wall["good"] = geo.is_good_facing(facing)
            walls.append(wall)
        lat, lon = geo.from_xy(*center)
        addresses = sorted({p["address"] for p in points if p["address"]})
        footprints.append({
            "id": fp["id"], "complex": complex_name, "floors": floors, "area_m2": round(area),
            "center": [round(lat, 6), round(lon, 6)],
            "outline": [_latlon(p) for p in ring],
            "addresses": addresses,
            "zone": _zone(walls, addresses, lat, lon, complex_name),
            "oval_side": geo.oval_side(lat, lon, oval_center) if complex_name == "Stuy Town" else None,
            "walls": walls,
        })
    footprints.sort(key=lambda f: (f["complex"], f["addresses"][:1] or ["~"], f["center"]))
    return {
        "about": "Stuy Town and Peter Cooper Village buildings, each wall's direction and view. Built by reference_data.py.",
        "source": ATTRIBUTION,
        "fetched_utc": now_utc(),
        "oval": {"outline": [_latlon(p) for p in oval_ring], "center": [round(c, 6) for c in oval_center],
                 "from": oval_from},
        "street_positions": {k: round(v, 1) for k, v in positions.items()},
        "complex_outlines": {name: [[_latlon(p) for p in r] for r in rings]
                             for name, rings in parsed["areas"].items() if name != "Oval"},
        "footprints": footprints,
    }


def _zone(walls, addresses, lat, lon, complex_name) -> str:
    """'Oval' if it looks at the Oval (or has an Oval address), '<street> front'
    for the street most of its street-facing walls look at, else 'interior'."""
    if any(w["faces_oval"] for w in walls) or any("OVAL" in a for a in addresses):
        return "Oval"
    streets = {}
    for w in walls:
        if w["street"]:
            streets[w["street"]] = streets.get(w["street"], 0) + w["length_m"]
    if streets:
        return f"{max(streets, key=streets.get)} front"
    return "interior" if walls else geo.zone(lat, lon, " ".join(addresses), complex_name)


def refresh_buildings(force: bool = False, timeout: float = 90) -> str:
    """Fetch and analyse the buildings if there are none yet (or force)."""
    if os.path.exists(BUILDINGS_FILE) and not force:
        return "buildings: already there"
    data = fetch_osm(timeout)
    result = analyze(data)
    if not result["footprints"]:
        return "buildings: OpenStreetMap answered, but no Stuy Town buildings were found -- kept the old file"
    write_json(BUILDINGS_FILE, result, one_per_line="footprints")
    oval = sum(1 for f in result["footprints"] if any(w["faces_oval"] for w in f["walls"]))
    return (f"buildings: {len(result['footprints'])} from OpenStreetMap, {oval} look at the Oval "
            f"(Oval outline: {result['oval']['from']})")


def load_buildings() -> dict | None:
    return read_json(BUILDINGS_FILE)


# ------------------------------------------------------------ floorplans


def layouts_seen(events: list) -> dict:
    """{svg path: {"layout": "108", "bedrooms": 1, ...}} for every floorplan in a listing."""
    layouts = {}
    for event in events:
        if event.get("event") != "new":
            continue
        for unit in event.get("units", []):
            data = unit.get("data") or {}
            path = (data.get("floorplan") or {}).get("floorplanSvg")
            if not path:
                continue
            style = str((data.get("style") or {}).get("name") or Path(path).stem)
            entry = layouts.setdefault(path, {"layout": style, "bedrooms": data.get("bedrooms"),
                                              "bathrooms": data.get("bathrooms"), "sqft": data.get("sqft"),
                                              "seen_in": []})
            label = f"{unit.get('apartment')}, {unit.get('address')}"
            if label not in entry["seen_in"]:
                entry["seen_in"].append(label)
    return layouts


def floorplan_name(layout: str, path: str) -> str:
    """'108' for /media/x/108.svg, '126_st2bedclassic' when the file isn't named after its layout."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(urllib.parse.urlparse(path).path).stem)[:40] or "plan"
    layout = re.sub(r"[^A-Za-z0-9_-]+", "-", str(layout))[:20] or "layout"
    return layout if stem == layout else f"{layout}_{stem}"


def fetch_floorplan(path: str, timeout: float = 12) -> bytes:
    """The SVG at a listing's floorplanSvg path, from the first host that has it."""
    last = None
    urls = [path] if path.startswith("http") else [host + path for host in FLOORPLAN_HOSTS]
    for url in urls:
        try:
            headers = {**check_units.REQUEST_HEADERS, "Accept": "image/svg+xml,image/*,*/*"}
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
            if b"<svg" in body[:5000].lower():
                return body
            last = RuntimeError(f"{url} didn't return an SVG")
        except Exception as e:
            last = e
    raise RuntimeError(str(last))


def svg_labels(svg: str) -> list:
    """The words on a floorplan drawing, in order: ['Living Room', '12’0', '”', 'x 18’8”', ...]."""
    labels = []
    for match in re.finditer(r"<text\b[^>]*>(.*?)</text>", svg, re.S | re.I):
        text = html.unescape(re.sub(r"<[^>]+>", " ", match[1]))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            labels.append(text)
    return labels[:80]


def rooms(labels: list) -> dict:
    """{'Living Room': '12’1” x 18’8”', 'Closet': '', ...}: each room name with
    the size written under it (the drawings split sizes into pieces)."""
    found, current = {}, None
    for text in labels:
        if re.search(r"[A-Za-z]{3,}", text):
            name, n = text.strip(), 2
            while name in found:  # a second closet
                name, n = f"{text.strip()} {n}", n + 1
            current = name
            found[current] = ""
        elif current and re.search(r"[\dx”\"]", text):
            found[current] += text
    for name, size in found.items():
        size = re.sub(r"\s*x\s*", " x ", size).strip()
        size = re.sub(r"(\d)(?= x|$)", r"\1”", size)  # a size missing its inch mark
        found[name] = size
    return found


def render_pngs(jobs: list, width: int = FLOORPLAN_PNG_WIDTH) -> str:
    """Save a PNG of each (svg_file, png_file), drawn by Chromium on white."""
    if not jobs:
        return ""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "PNGs skipped: Playwright isn't installed"
    done = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": width, "height": 800})
            for svg_file, png_file in jobs:
                data = base64.b64encode(Path(svg_file).read_bytes()).decode()
                page.set_content(f'<html><body style="margin:0;background:#fff">'
                                 f'<img id="plan" style="display:block;width:{width}px;height:auto" '
                                 f'src="data:image/svg+xml;base64,{data}"></body></html>')
                page.wait_for_function("document.getElementById('plan').complete", timeout=10_000)
                page.locator("#plan").screenshot(path=png_file)
                done += 1
            browser.close()
    except Exception as e:
        return f"PNGs: {done} saved, then {type(e).__name__}: {str(e)[:200]}"
    return f"PNGs: {done} saved"


def refresh_floorplans(events: list, deadline: float) -> str:
    """Download the floorplans not saved yet, and PNG copies of any without one."""
    index_file = f"{FLOORPLANS_DIR}/index.json"
    index = read_json(index_file, {}) or {}
    new, failed, pngs = 0, [], []
    for path, seen in sorted(layouts_seen(events).items()):
        name = floorplan_name(seen["layout"], path)
        entry = index.setdefault(name, {})
        entry.update({"layout": seen["layout"], "bedrooms": seen["bedrooms"], "bathrooms": seen["bathrooms"],
                      "sqft": seen["sqft"], "source": path, "seen_in": seen["seen_in"][-12:]})
        svg_file = f"{FLOORPLANS_DIR}/{name}.svg"
        if not os.path.exists(svg_file):
            if time.monotonic() > deadline:
                continue
            try:
                body = fetch_floorplan(path)
            except Exception as e:
                failed.append(name)
                entry["last_error"] = f"{now_utc()}: {str(e)[:200]}"
                continue
            text = apply_recorder.scrub_keys(body.decode("utf-8", "replace"))
            os.makedirs(FLOORPLANS_DIR, exist_ok=True)
            Path(svg_file).write_text(text, encoding="utf-8")
            entry.pop("last_error", None)
            entry.update({"svg": f"{name}.svg", "fetched_utc": now_utc()})
            new += 1
        if os.path.exists(svg_file):  # re-read each time, so a better reader updates old ones too
            entry["labels"] = svg_labels(Path(svg_file).read_text(encoding="utf-8"))
            entry["rooms"] = rooms(entry["labels"])
        png_file = f"{FLOORPLANS_DIR}/{name}.png"
        if os.path.exists(svg_file) and not os.path.exists(png_file):
            pngs.append((svg_file, png_file))
    png_note = render_pngs(pngs) if pngs and time.monotonic() < deadline else ""
    for name, entry in index.items():
        if os.path.exists(f"{FLOORPLANS_DIR}/{name}.png"):
            entry["png"] = f"{name}.png"
    if index:
        write_json(index_file, index)
    parts = [f"floorplans: {len(index)} layouts, {new} new"]
    if failed:
        parts.append(f"couldn't fetch {', '.join(failed)}")
    if png_note:
        parts.append(png_note)
    return "; ".join(parts)


# ---------------------------------------------------------------- refresh


def refresh(events: list | None = None, budget_seconds: float = 100, buildings: bool = True,
            force_buildings: bool = False) -> list:
    """Fetch whatever reference data is missing, within the time budget.
    Returns what happened, one line per part. Never raises."""
    deadline = time.monotonic() + budget_seconds
    notes = []
    if events is None:
        events = read_json(check_units.EVENTS_FILE, []) or []
    try:
        notes.append(refresh_floorplans(events, deadline))
    except Exception as e:
        notes.append(f"floorplans failed: {type(e).__name__}: {e}")
    if buildings:
        try:
            left = deadline - time.monotonic()
            if left > 20 or force_buildings:
                notes.append(refresh_buildings(force_buildings, timeout=max(20, min(90, left))))
            else:
                notes.append("buildings: skipped, out of time")
        except Exception as e:
            notes.append(f"buildings failed: {type(e).__name__}: {str(e)[:300]}")
    return notes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fetch the map's buildings and the layouts' floorplans.")
    parser.add_argument("--buildings", action="store_true", help="re-fetch the buildings from OpenStreetMap")
    parser.add_argument("--from-file", help="analyse a saved Overpass answer instead of fetching one")
    args = parser.parse_args(argv)
    if args.from_file:
        result = analyze(read_json(args.from_file, {}))
        write_json(BUILDINGS_FILE, result)
        print(f"buildings: {len(result['footprints'])} written to {BUILDINGS_FILE}")
        return 0
    for line in refresh(budget_seconds=240, force_buildings=args.buildings):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
