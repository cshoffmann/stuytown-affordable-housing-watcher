"""
data/units.geojson: a map of Stuy Town and Peter Cooper Village with every
unit the watcher has seen on it. GitHub draws .geojson files as an
interactive map: open the file on github.com, zoom in, and tap a building,
wall or unit for its details. geojson.io draws it too (the link is on the
summary page).

    Buildings   from data/reference/buildings.json (OpenStreetMap), coloured by
                what their windows can get:
                  gold    looks at the Oval with sunny windows (E round to SW)
                  blue    looks at the Oval, but its Oval windows face W/N
                  green   has sunny walls that don't face a street
                  grey    the rest
    Walls       orange: an Oval wall with sun; blue: an Oval wall without;
                green: a sunny wall away from the streets
    Units       one dot per unit, at its building, coloured by its dream rating
                (dream_units.py): gold star = dream, orange star = possible
                dream, purple = almost, green = great light, blue = price
                match, grey = the rest
    L train     the First Ave station's entrances

Without buildings.json (before the first refresh) it still shows each
building seen as a point, and the units.

    python units_map.py    # rebuild data/units.geojson from data/
"""

import json
import math
import os
import sys
from datetime import datetime

import check_units
import dream_units
import reference_data
import stuytown_geo as geo

MAP_FILE = "data/units.geojson"
REPO_URL = "https://github.com/cshoffmann/stuytown-affordable-housing-watcher"
TIER_STYLE = {
    "dream": ("#d4a017", "star", "large"),
    "possible dream": ("#f08c00", "star", "medium"),
    "almost": ("#8e44ad", "circle", "medium"),
    "great light": ("#2e9e44", "circle", "medium"),
    "price match": ("#1f6fd1", "circle", "small"),
    None: ("#8a8f98", "circle", "small"),
}
BUILDING_FILL = {"sunny Oval": "#f2c94c", "shady Oval": "#9cc3f0", "sunny, quiet": "#a8dcae", "other": "#d5dbe0"}
UNIT_SPREAD_M = 7  # units in one building are drawn on a small circle round it


def _lonlat(lat, lon) -> list:
    return [round(float(lon), 6), round(float(lat), 6)]


def _polygon(outline) -> dict:
    ring = [_lonlat(lat, lon) for lat, lon in outline]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _feature(geometry, properties) -> dict:
    return {"type": "Feature", "geometry": geometry,
            "properties": {k: v for k, v in properties.items() if v not in (None, "", [])}}


def _title(address) -> str:
    return " ".join(w.capitalize() for w in str(address).split())


def _money(value) -> str:
    return f"${value:,.0f}" if value is not None else ""


def building_kind(footprint) -> str:
    walls = footprint.get("walls", [])
    oval = [w for w in walls if w.get("faces_oval")]
    if any(dream_units.sunny(w) for w in oval):
        return "sunny Oval"
    if oval:
        return "shady Oval"
    if any(dream_units.sunny(w) and not w.get("street") for w in walls):
        return "sunny, quiet"
    return "other"


def _walls_text(walls) -> str:
    """'SSW 8.8 h, ESE 6.2 h' -- winter sun at floor 8."""
    by_direction = {}
    for w in walls:
        hours = (w.get("winter_sun_h") or {}).get("8")
        if hours is not None:
            by_direction[w["compass"]] = max(hours, by_direction.get(w["compass"], 0))
    return ", ".join(f"{d} {h:g} h" for d, h in sorted(by_direction.items(), key=lambda x: -x[1]))


def building_features(buildings, units_by_address) -> list:
    features = []
    for fp in buildings.get("footprints", []):
        kind = building_kind(fp)
        walls = fp.get("walls", [])
        seen = [u for a in fp.get("addresses", []) for u in units_by_address.get(a, [])]
        lat, lon = fp["center"]
        minutes, entrance = geo.walk_to_l(lat, lon)
        side = fp.get("oval_side") or {}
        properties = {
            "title": ", ".join(_title(a) for a in fp.get("addresses", [])) or f"{fp['complex']} building",
            "Complex": fp["complex"],
            "Zone": fp.get("zone"),
            "Kind": {"sunny Oval": "looks at the Oval, sunny side",
                     "shady Oval": "looks at the Oval, shady side",
                     "sunny, quiet": "sunny walls away from the streets",
                     "other": "no sunny quiet walls"}[kind],
            "Oval walls": ", ".join(sorted({w["compass"] for w in walls if w.get("faces_oval")})),
            "Side of the Oval": side.get("side") if kind in ("sunny Oval", "shady Oval") else None,
            "Street walls": ", ".join(sorted({f"{w['compass']} ({w['street']})" for w in walls if w.get("street")})),
            "Winter sun, floor 8": _walls_text(walls),
            "Floors": fp.get("floors"),
            "Walk to the L": f"{minutes:g} min ({entrance} entrance)",
            "Units seen": len(seen) or None,
            "fill": BUILDING_FILL[kind], "fill-opacity": 0.55 if kind != "other" else 0.35,
            "stroke": "#5f6b76", "stroke-width": 1, "stroke-opacity": 0.8,
        }
        features.append(_feature(_polygon(fp["outline"]), properties))
        for w in walls:
            if w.get("faces_oval"):
                color, width, what = ("#f08c00", 4, "Oval wall, sunny") if dream_units.sunny(w) else ("#1f6fd1", 4, "Oval wall, shady")
            elif dream_units.sunny(w) and not w.get("street"):
                color, width, what = "#2e9e44", 2, "sunny wall, away from the streets"
            else:
                continue
            hours = w.get("winter_sun_h") or {}
            features.append(_feature(
                {"type": "LineString", "coordinates": [_lonlat(*p) for p in w["line"]]},
                {"title": f"{what}: faces {w['compass']}",
                 "Building": properties["title"],
                 "Faces": f"{w['compass']} ({w['facing']:.0f} deg)",
                 "Next building": f"{w['open_m']} m away" if w.get("open_m") else "none close",
                 "Winter sun by floor": ", ".join(f"floor {f}: {h:g} h" for f, h in hours.items()),
                 "stroke": color, "stroke-width": width, "stroke-opacity": 0.95}))
    oval = buildings.get("oval") or {}
    if oval.get("outline"):
        features.insert(0, _feature(_polygon(oval["outline"]), {
            "title": "The Oval", "Outline": oval.get("from"),
            "fill": "#3b8f3b", "fill-opacity": 0.25, "stroke": "#2b6e2b", "stroke-width": 1}))
    return features


def latest_units(events: list) -> list:
    """(the newest sighting's summary, when it was first seen, listed for) for every unit seen."""
    first_seen, listed_for, latest = {}, {}, {}
    for event in events:
        for summary in event.get("units", []):
            uid = summary.get("unit_id")
            if event.get("event") == "new" and summary.get("data"):
                latest[uid] = summary
                first_seen[uid] = event.get("detected_at_utc")
            elif event.get("event") == "removed":
                listed_for[uid] = summary.get("listed_for_seconds")
    return [(latest[u], first_seen.get(u), listed_for.get(u)) for u in latest]


def _when(stamp) -> str:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).astimezone(geo.NEW_YORK)
    except ValueError:
        return str(stamp or "")
    return f"{when:%b} {when.day} {when:%I:%M %p}".replace(" 0", " ")


def unit_features(events, buildings, atlas, overrides) -> tuple:
    """(unit features, {address: [unit, ...]})."""
    features, by_address, slot = [], {}, {}
    for summary, first, listed in latest_units(events):
        unit = summary["data"]
        where = dream_units.position(unit)
        address = dream_units.building_address(unit)
        by_address.setdefault(address, []).append(unit)
        if not where:
            continue
        n = slot[address] = slot.get(address, -1) + 1
        angle = n * 2.4  # golden-angle steps, so many units still spread out
        x, y = geo.to_xy(*where)
        radius = UNIT_SPREAD_M * math.sqrt(n + 1)
        lat, lon = geo.from_xy(x + radius * math.cos(angle), y + radius * math.sin(angle))
        rating = dream_units.rate(unit, buildings, atlas, overrides)
        color, symbol, size = TIER_STYLE.get(rating["tier"], TIER_STYLE[None])
        rent = check_units.unit_rent(unit)
        sqft = check_units.number(unit.get("sqft"))
        layout = str((unit.get("style") or {}).get("name") or "")
        face = rating["facing"]
        features.append(_feature({"type": "Point", "coordinates": _lonlat(lat, lon)}, {
            "title": f"{unit.get('name')}, {_title(address)}",
            "Rating": (rating["tier"] or "-") + (f": {rating['why']}" if rating["why"] else ""),
            "Rent": f"{_money(rent)}/mo" + (f" (${rent / sqft:.2f}/sq ft)" if rent and sqft else ""),
            "Size": f"{'studio' if unit.get('bedrooms') == 0 else str(unit.get('bedrooms')) + ' bed'}"
                    f" / {unit.get('bathrooms')} bath, {unit.get('sqft')} sq ft",
            "Floor": rating["floor"],
            "Layout": layout,
            "Finish": dream_units.finish_name(unit),
            "Features": ", ".join(dream_units.features(unit)),
            "Faces": f"{'/'.join(face['compass'])} -- {face['source']}" if face["compass"] else face["source"],
            "First seen": _when(first),
            "Listed for": f"{listed:.0f} s" if isinstance(listed, (int, float)) else "",
            "Listing": check_units.unit_url(unit),
            "Floorplan": f"{REPO_URL}/blob/main/{reference_data.FLOORPLANS_DIR}/"
                         f"{reference_data.floorplan_name(layout, (unit.get('floorplan') or {}).get('floorplanSvg') or layout)}.png"
                         if layout else "",
            "marker-color": color, "marker-symbol": symbol, "marker-size": size,
        }))
    tier_order = {t: i for i, t in enumerate(dream_units.TIERS)}
    features.sort(key=lambda f: -tier_order.get(f["properties"]["Rating"].split(":")[0], 99))  # best drawn last, on top
    return features, by_address


def building_points(by_address, events) -> list:
    """Without buildings.json: one point per building seen."""
    points, done = [], set()
    for summary, _, _ in latest_units(events):
        unit = summary["data"]
        address = dream_units.building_address(unit)
        where = dream_units.position(unit)
        if address in done or not where:
            continue
        done.add(address)
        info = dream_units.building_info(unit, None)
        side = info.get("oval_side") or {}
        points.append(_feature({"type": "Point", "coordinates": _lonlat(*where)}, {
            "title": _title(address), "Zone": info.get("zone"),
            "Looks at the Oval from": side.get("side") if info.get("zone") == "Oval" else None,
            "Walk to the L": f"{info['walk_to_l'][0]:g} min" if info.get("walk_to_l") else None,
            "Units seen": len(by_address.get(address, [])),
            "marker-color": "#5f6b76", "marker-symbol": "building", "marker-size": "small",
        }))
    return points


def build(events: list | None = None) -> dict:
    events = events if events is not None else reference_data.read_json(check_units.EVENTS_FILE, []) or []
    buildings = dream_units.load_buildings()
    atlas = dream_units.load_atlas()
    overrides = dream_units.load_overrides()
    units, by_address = unit_features(events, buildings, atlas, overrides)
    features = building_features(buildings, by_address) if buildings else building_points(by_address, events)
    for name, (lat, lon) in geo.L_ENTRANCES.items():
        features.append(_feature({"type": "Point", "coordinates": _lonlat(lat, lon)}, {
            "title": f"L train, First Ave station ({name} entrance, approximate)",
            "marker-color": "#6b6b6b", "marker-symbol": "rail-metro", "marker-size": "small"}))
    return {"type": "FeatureCollection", "features": features + units}


def write(events: list | None = None, path: str | None = None) -> str:
    """Write the map; returns a one-line description."""
    data = build(events)
    path = path or MAP_FILE
    with open(f"{path}.tmp", "w", encoding="utf-8") as f:  # one feature per line: readable diffs
        f.write('{"type": "FeatureCollection", "features": [\n')
        f.write(",\n".join(json.dumps(feature, ensure_ascii=False) for feature in data["features"]))
        f.write("\n]}\n")
    os.replace(f"{path}.tmp", path)
    units = sum(1 for f in data["features"] if f["geometry"]["type"] == "Point" and "Rating" in f["properties"])
    shapes = sum(1 for f in data["features"] if f["geometry"]["type"] == "Polygon")
    return f"map: {units} units, {shapes} building outlines"


def refresh_all(budget_seconds: float = 100, force_buildings: bool = False) -> list:
    """Fetch missing floorplans and buildings (reference_data.py), then
    rebuild the apartment atlas and the map. One line per part; never raises."""
    notes = reference_data.refresh(budget_seconds=budget_seconds, force_buildings=force_buildings)
    try:
        atlas = dream_units.write_atlas()
        notes.append(f"atlas: {len(atlas['lines'])} apartment lines")
    except Exception as e:
        notes.append(f"atlas failed: {type(e).__name__}: {e}")
    try:
        notes.append(write())
    except Exception as e:
        notes.append(f"map failed: {type(e).__name__}: {e}")
    return notes


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Rebuild data/units.geojson (and the atlas).")
    parser.add_argument("--refresh", action="store_true", help="first fetch missing floorplans and buildings")
    parser.add_argument("--buildings", action="store_true", help="with --refresh: re-fetch the buildings too")
    args = parser.parse_args(argv)
    if args.refresh:
        for line in refresh_all(budget_seconds=240, force_buildings=args.buildings):
            print(line)
    else:
        dream_units.write_atlas()
        print(write())
    return 0


if __name__ == "__main__":
    sys.exit(main())
