"""
Where things are in Stuy Town and Peter Cooper Village, and how much sun a
window gets -- plain math, no network, no files.

    to_xy / from_xy    lat/lon <-> metres east/north of a point near the Oval
    to_grid            lat/lon -> (u, v) on the Manhattan street grid: u runs
                       along the streets toward Avenue C, v up the avenues
                       toward 23rd St. The grid is turned 29 degrees from
                       true north, so "uptown" is NNE, not N.
    zone               "14th St front", "Oval", "interior", ... from a building's position
    walk_to_l          minutes to the nearest First Ave L entrance
    sun_hours          hours of direct sun on a wall facing a compass bearing,
                       on a given day, optionally with a building across from it
    sun_profile        the same for winter, the equinox and summer

Most buildings are lined up with the street grid, so their windows face one
of four ways: SSW (downtown, the most sun), ESE (toward Avenue C, morning
sun), WNW (toward First Ave, late-day sun) or NNE (uptown, almost none). The
ones round the Oval are turned to face it. The sun model is NOAA's, the one
suncalc.org uses.
"""

import functools
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LAT0, LON0 = 40.7315, -73.978  # near the middle of Stuy Town
M_PER_DEG_LAT = 111_049.0
M_PER_DEG_LON = 84_490.0  # at 40.73 N
GRID_BEARING = 29.0  # Manhattan's avenues run this many degrees east of true north

# The Oval's middle (geocoded), used until reference_data.py has its real outline.
OVAL_CENTER = (40.732177, -73.977585)
OVAL_RADIUS_M = 75  # it's about four acres; for the fallback outline only

# First Avenue L station: its First Ave and Avenue A entrances (estimated).
L_ENTRANCES = {
    "First Ave": (40.730953, -73.981628),
    "Avenue A": (40.730160, -73.979860),
}
WALK_M_PER_MIN = 80
TO_WALKING = 1.15  # paths through the complex aren't straight lines

# The windows you want: E, ESE, SE, SSE, S, SSW or SW (16-point compass sectors).
GOOD_FACING = (78.75, 236.25)

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# The four ways a grid-aligned window faces, in plain words.
GRID_FACINGS = {
    "NNE": "toward 20th/23rd St (uptown)",
    "ESE": "toward Avenue C / the river",
    "SSW": "toward 14th/20th St (downtown)",
    "WNW": "toward First Ave",
}

SEASONS = {"winter": date(2026, 12, 21), "equinox": date(2026, 3, 20), "summer": date(2026, 6, 21)}
FLOOR_HEIGHT_M = 2.9
DEFAULT_BUILDING_FLOORS = 13  # Stuy Town; Peter Cooper Village is 15

NEW_YORK = ZoneInfo("America/New_York")


# ------------------------------------------------------------ positions


def to_xy(lat, lon) -> tuple:
    """Metres (east, north) from (LAT0, LON0)."""
    return (float(lon) - LON0) * M_PER_DEG_LON, (float(lat) - LAT0) * M_PER_DEG_LAT


def from_xy(x, y) -> tuple:
    return LAT0 + y / M_PER_DEG_LAT, LON0 + x / M_PER_DEG_LON


def to_grid(lat, lon) -> tuple:
    """(u, v) metres: u toward Avenue C along the streets, v uptown along the avenues."""
    x, y = to_xy(lat, lon)
    a = math.radians(GRID_BEARING)
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def from_grid(u, v) -> tuple:
    """(lat, lon) of a grid position."""
    a = math.radians(GRID_BEARING)
    return from_xy(u * math.cos(a) + v * math.sin(a), -u * math.sin(a) + v * math.cos(a))


def bearing(lat1, lon1, lat2, lon2) -> float:
    """Compass bearing (0 = north, clockwise) from point 1 to point 2."""
    x1, y1 = to_xy(lat1, lon1)
    x2, y2 = to_xy(lat2, lon2)
    return math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360


def distance_m(lat1, lon1, lat2, lon2) -> float:
    x1, y1 = to_xy(lat1, lon1)
    x2, y2 = to_xy(lat2, lon2)
    return math.hypot(x2 - x1, y2 - y1)


def compass(degrees) -> str:
    """16-point compass name: 209 -> 'SSW'."""
    return COMPASS[int(((float(degrees) % 360) + 11.25) // 22.5) % 16]


def is_good_facing(degrees) -> bool:
    low, high = GOOD_FACING
    return low <= float(degrees) % 360 < high


def angle_between(a, b) -> float:
    """Smallest angle between two bearings, 0-180."""
    return abs((float(a) - float(b) + 180) % 360 - 180)


def walk_to_l(lat, lon) -> tuple:
    """(minutes, entrance name) to the nearest First Ave L entrance."""
    best = None
    for name, (elat, elon) in L_ENTRANCES.items():
        u1, v1 = to_grid(lat, lon)
        u2, v2 = to_grid(elat, elon)
        metres = (abs(u2 - u1) + abs(v2 - v1)) * 0.85 * TO_WALKING  # paths cut corners
        if best is None or metres < best[0]:
            best = (metres, name)
    return round(best[0] / WALK_M_PER_MIN, 1), best[1]


def complex_of(unit_or_property) -> str:
    """'Stuy Town' or 'Peter Cooper Village', from a unit's property (or the name itself)."""
    prop = unit_or_property
    if isinstance(prop, dict):
        prop = (prop.get("property") or {}).get("name") if "property" in prop else prop.get("name")
    name = str(prop or "").lower()
    return "Peter Cooper Village" if "peter cooper" in name or name.startswith("pcv") else "Stuy Town"


# Building middles closer than this to the complex's edge are on that street
# (from the buildings seen so far: 405/445/545/605 E 14th are on 14th St at
# v -163..-175, while 451/455 E 14th sit inside at v -113/-80).
EDGES = {
    "Stuy Town": {"14th St": ("v", -145, -1), "20th St": ("v", 220, 1),
                  "First Ave": ("u", -280, -1), "Avenue C": ("u", 285, 1)},
    "Peter Cooper Village": {"20th St": ("v", 300, -1), "23rd St": ("v", 470, 1),
                             "First Ave": ("u", -280, -1), "FDR Drive": ("u", 250, 1)},
}


def zone(lat, lon, address: str = "", complex_name: str = "Stuy Town") -> str:
    """'14th St front', 'First Ave front', 'Oval' or 'interior' for a building."""
    if "OVAL" in str(address).upper():
        return "Oval"
    if lat in (None, "") or lon in (None, ""):
        return "unknown"
    u, v = to_grid(lat, lon)
    position = {"u": u, "v": v}
    fronts = []
    for street, (axis, edge, side) in EDGES.get(complex_name, EDGES["Stuy Town"]).items():
        beyond = (position[axis] - edge) * side
        if beyond > 0:
            fronts.append((beyond, street))
    return f"{max(fronts)[1]} front" if fronts else "interior"


def oval_side(lat, lon, center=OVAL_CENTER) -> dict:
    """Which side of the Oval a building is on, and which way it looks at it:
    {"side": "NW", "faces": 131.7, "faces_compass": "SE", "north_side": True}.
    "North side" means its Oval windows face E round to SW, so they get sun."""
    faces = bearing(lat, lon, *center)
    return {"side": compass((faces + 180) % 360), "faces": round(faces, 1), "faces_compass": compass(faces),
            "north_side": is_good_facing(faces), "distance_m": round(distance_m(lat, lon, *center))}


# ------------------------------------------------------------------ sun


def sun_position(when: datetime, lat: float = LAT0, lon: float = LON0) -> tuple:
    """(azimuth, elevation) in degrees for a UTC datetime (NOAA's formulas)."""
    when = when.astimezone(timezone.utc)
    jd = when.timestamp() / 86400 + 2440587.5
    jc = (jd - 2451545) / 36525
    mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    m = math.radians(mean_anom)
    centre = (math.sin(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
              + math.sin(2 * m) * (0.019993 - 0.000101 * jc) + math.sin(3 * m) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = mean_long + centre - 0.00569 - 0.00478 * math.sin(omega)
    obliq = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    obliq += 0.00256 * math.cos(omega)
    decl = math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(app_long)))
    y = math.tan(math.radians(obliq / 2)) ** 2
    l0 = math.radians(mean_long)
    eq_time = 4 * math.degrees(y * math.sin(2 * l0) - 2 * ecc * math.sin(m) + 4 * ecc * y * math.sin(m) * math.cos(2 * l0)
                               - 0.5 * y * y * math.sin(4 * l0) - 1.25 * ecc * ecc * math.sin(2 * m))
    minutes = when.hour * 60 + when.minute + when.second / 60
    solar_time = (minutes + eq_time + 4 * lon) % 1440
    hour_angle = solar_time / 4 + 180 if solar_time < 0 else solar_time / 4 - 180
    phi = math.radians(lat)
    cos_zenith = (math.sin(phi) * math.sin(decl)
                  + math.cos(phi) * math.cos(decl) * math.cos(math.radians(hour_angle)))
    zenith = math.acos(max(-1.0, min(1.0, cos_zenith)))
    denominator = math.cos(phi) * math.sin(zenith)
    if abs(denominator) < 1e-9:
        azimuth = 180.0
    else:
        a = math.degrees(math.acos(max(-1.0, min(1.0, (math.sin(phi) * math.cos(zenith) - math.sin(decl)) / denominator))))
        azimuth = (a + 180) % 360 if hour_angle > 0 else (540 - a) % 360
    return azimuth, 90 - math.degrees(zenith)


@functools.cache
def _sun_track(day: date, step_minutes: int = 5) -> tuple:
    """((azimuth, elevation), ...) every step through one New York day, sun up only."""
    start = datetime(day.year, day.month, day.day, tzinfo=NEW_YORK).astimezone(timezone.utc)
    track = []
    for i in range(24 * 60 // step_minutes):
        az, el = sun_position(start + timedelta(minutes=i * step_minutes))
        if el > 0:
            track.append((az, el))
    return tuple(track)


def sun_hours(facing, day: date | None = None, open_m: float | None = None,
              blocked_above_m: float = 0.0, step_minutes: int = 5) -> float:
    """Hours of direct sun on a wall facing `facing` (compass degrees).

    open_m / blocked_above_m: a building across from the wall, open_m away,
    standing blocked_above_m higher than the window (treated as a long wall
    parallel to it). Without one, only the horizon counts."""
    day = day or SEASONS["winter"]
    steps = 0
    for az, el in _sun_track(day, step_minutes):
        off = angle_between(az, facing)
        if off >= 85:  # sun skimming along the wall, or behind it
            continue
        if open_m and blocked_above_m > 0:
            if el < math.degrees(math.atan2(blocked_above_m * math.cos(math.radians(off)), open_m)):
                continue
        steps += 1
    return round(steps * step_minutes / 60, 1)


def window_height_m(floor) -> float:
    """Height of a window's middle above the ground; floor 'M' (mezzanine) or 0 counts as 1."""
    try:
        floor = int(floor)
    except (TypeError, ValueError):
        floor = 1
    return (max(floor, 1) - 1) * FLOOR_HEIGHT_M + 1.5 + 1.0  # the ground floor is raised a little


def sun_profile(facing, floor=None, open_m: float | None = None, across_floors: int | None = None) -> dict:
    """{"winter": 8.3, "equinox": 11.1, "summer": 9.6} hours of direct sun --
    for one floor when a building stands open_m across from it."""
    blocked = 0.0
    if open_m and floor is not None:
        across = (across_floors or DEFAULT_BUILDING_FLOORS) * FLOOR_HEIGHT_M + 1.5
        blocked = max(0.0, across - window_height_m(floor))
    return {season: sun_hours(facing, day, open_m, blocked) for season, day in SEASONS.items()}


def floor_number(unit: dict) -> int | None:
    """6 for unitFloor "06" (or name "06-E"); 1 for the mezzanine "0M"; None if unknown."""
    raw = str(unit.get("unitFloor") or str(unit.get("name") or "").split("-")[0]).strip().upper()
    if raw in ("0M", "M", "MEZ"):
        return 1
    try:
        return int(raw)
    except ValueError:
        return None


def line_letter(unit: dict) -> str | None:
    """'E' for apartment "06-E": apartments with the same letter in a building
    sit on top of each other, so they face the same way."""
    name = str(unit.get("name") or "")
    if "-" in name:
        letter = name.split("-", 1)[1].strip().upper()
        return letter or None
    return None
