"""
Sun map builder: which way every StuyTown / Peter Cooper Village apartment
line faces, and how much direct sun it gets on each floor.

    pip install -r tools/sun_map/requirements.txt
    python tools/sun_map/build.py

Reads (all in the repo):
    tools/sun_map/inputs/osm_buildings.json   building outlines and walls (OpenStreetMap)
    tools/sun_map/inputs/cbre_plans.json      lettered floor plans (CBRE 2006 offering memorandum)
    tools/sun_map/inputs/market_listings.json market-rate listings seen (view tags, layouts, sizes)
    data/events.json                          affordable listings the watcher has seen
Writes:
    data/sun/unit_lines.json   the lookup unit_sun.py reads: per address, the apartment
                               spots (windows, sun by floor) and which spot each line is
    data/sun/sun_map.html      the interactive map (self-contained page)
    data/sun/units_by_floor.csv  every line x floor, for a spreadsheet

How a line gets its direction, best first:
  1. "sure": the building's own CBRE floor plan, laid onto its outline.
  2. "sure"/"likely": a building of the same design (outline matches after
     turning or mirroring), the right way round picked by listing sizes and
     view tags (Oval, East River, Empire State).
  3. Designs with no plan: the 8 apartments of each floor are placed around
     the address's core from the outline (two per wing, lettered clockwise,
     as in every plan we have), and the listings decide where A starts. The
     answer is a probability over spots, so a line can be "faces the Oval
     (ENE), one of two neighbouring apartments".
Sun: NOAA sun positions every 10 minutes on the 21st of each month; a window
is in sun when the sun is in front of it, over 2 degrees up, and no building
(its own wings included) is in the way. Trees aren't modelled.
"""
import csv
import itertools
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import geo  # noqa: E402  (plain math: positions, sun, compass)
import numpy as np  # noqa: E402
from shapely import affinity  # noqa: E402
from shapely.geometry import LineString, Point, Polygon  # noqa: E402
from shapely.prepared import prep  # noqa: E402

INPUTS = os.path.join(HERE, "inputs")
OUT = os.path.join(REPO, "data", "sun")
LETTERS = "ABCDEFGH"


def log(*a):
    print(*a, flush=True)


# ================================================================== geometry
B = json.load(open(os.path.join(INPUTS, "osm_buildings.json")))
FPS = B["footprints"]
FLOOR_H = {"Stuy Town": 2.9, "Peter Cooper Village": 3.0}
MAIN_Z = 2.5  # window middle on the Main level, metres above grade; floor n adds n storeys

# OpenStreetMap's name -> the name the listings use
ALIASES = {"651 14TH STREET LOOP": "651 EAST 14TH STREET"}
for fp in FPS:
    fp["addresses"] = [ALIASES.get(a, a) for a in fp["addresses"]]
    for w in fp["walls"]:
        w["address"] = ALIASES.get(w.get("address"), w.get("address"))
for fp in FPS:
    fp["poly"] = Polygon([geo.to_xy(a, b) for a, b in fp["outline"]]).buffer(0)
    fp["height"] = fp["floors"] * FLOOR_H[fp["complex"]] + 1.0
    fp["prep"] = prep(fp["poly"])
FP_BY_ID = {f["id"]: f for f in FPS}


def unit_vec(bearing):
    r = math.radians(bearing)
    return np.array([math.sin(r), math.cos(r)])


def bearing_of(vec):
    return math.degrees(math.atan2(vec[0], vec[1])) % 360


GU = unit_vec(geo.GRID_BEARING + 90)  # along the streets, toward Avenue C
GV = unit_vec(geo.GRID_BEARING)       # up the avenues, uptown

SEGS = []  # wall pieces of at most 12 m
for fp in FPS:
    for w in fp["walls"]:
        (la1, lo1), (la2, lo2) = w["line"]
        p1, p2 = np.array(geo.to_xy(la1, lo1)), np.array(geo.to_xy(la2, lo2))
        n = max(1, math.ceil(np.linalg.norm(p2 - p1) / 12))
        for i in range(n):
            a, b = p1 + (p2 - p1) * i / n, p1 + (p2 - p1) * (i + 1) / n
            SEGS.append({"fp": fp["id"], "address": w.get("address"), "facing": w["facing"],
                         "street": w.get("street"), "oval": bool(w.get("faces_oval")),
                         "a": a, "b": b, "mid": (a + b) / 2})
SEGS_BY_FP = {}
for s in SEGS:
    SEGS_BY_FP.setdefault(s["fp"], []).append(s)
log("walls", len(SEGS))

# ================================================================== sun
DAYS = [date(2026, m, 21) for m in range(1, 13)]
STEP = 10
_sun = []
for di, d in enumerate(DAYS):
    start = datetime(d.year, d.month, d.day, tzinfo=geo.NEW_YORK)
    for i in range(24 * 60 // STEP):
        t = start + timedelta(minutes=i * STEP + STEP / 2)
        az, el = geo.sun_position(t.astimezone(timezone.utc))
        if el > 2:
            _sun.append((di, t.hour + t.minute / 60, az, el))
SUN_DAY = np.array([s[0] for s in _sun])
SUN_HR = np.array([s[1] for s in _sun])
SUN_AZ = np.array([s[2] for s in _sun])
SUN_EL = np.array([s[3] for s in _sun])
AZ_BIN = 3.0
SUN_BIN = (np.round(SUN_AZ / AZ_BIN).astype(int)) % int(360 / AZ_BIN)
MORNING = SUN_HR < 12.0
DEC = DAYS.index(date(2026, 12, 21))
STEP_H = STEP / 60


def ray_hits(seg, az):
    start = seg["mid"] + unit_vec(seg["facing"]) * 0.4
    ray = LineString([tuple(start), tuple(start + unit_vec(az) * 300)])
    hits = []
    for fp in FPS:
        if fp["prep"].intersects(ray):
            inter = fp["poly"].intersection(ray)
            if not inter.is_empty:
                hits.append((max(Point(tuple(start)).distance(inter), 0.3), fp["height"]))
    return hits


log("shading ...")
for s in SEGS:
    s["prof"] = {b: ray_hits(s, b * AZ_BIN) for b in range(int(360 / AZ_BIN))
                 if geo.angle_between(b * AZ_BIN, s["facing"]) < 80}

_lit_cache = {}


def seg_lit(seg, z):
    key = (id(seg), round(z, 1))
    if key not in _lit_cache:
        out = np.zeros(len(SUN_AZ), bool)
        for b, hits in seg["prof"].items():
            m = SUN_BIN == b
            if m.any():
                block = max([math.degrees(math.atan2(h - z, d)) for d, h in hits if h > z] or [-90])
                out |= m & (SUN_EL > block)
        _lit_cache[key] = out
    return _lit_cache[key]


def sun_stats(mask):
    """[morning h, afternoon h, Dec 21 h, Dec 21 morning h]: daily averages over the 12 days, and the shortest day."""
    n = len(DAYS)
    return [round(float(mask[MORNING].sum()) * STEP_H / n, 1),
            round(float(mask[~MORNING].sum()) * STEP_H / n, 1),
            round(float(mask[SUN_DAY == DEC].sum()) * STEP_H, 1),
            round(float(mask[(SUN_DAY == DEC) & MORNING].sum()) * STEP_H, 1)]


def z_of(complex_name, floor):
    return MAIN_Z + floor * FLOOR_H[complex_name]


# ================================================================== listings (evidence)
def norm(addr):
    return re.sub(r"\s+", " ", str(addr or "").upper().strip())


def layout_base(name):
    """'224' / '124' / '407z' / '2-bed-412' -> '24' / '24' / '07' / '12': the floorplan's shape, whatever the bedroom setup."""
    m = re.search(r"(\d)(\d\d)[a-z]?(?:-[a-z])?$", str(name or ""))
    return m.group(2) if m else None


LISTINGS = []
for u in json.load(open(os.path.join(REPO, "data", "sun", "market_seen.json")))["units"]:
    LISTINGS.append({"address": norm(u["address"]), "apt": u["apt"], "sqft": u.get("sqft"), "beds": u.get("beds"),
                     "rent": u.get("price"), "codes": u.get("codes") or [], "layout": u.get("layout"),
                     "point": (u["lat"], u["lon"]) if u.get("lat") else None, "source": "market"})
_seen = set()
for e in json.load(open(os.path.join(REPO, "data", "events.json"))):
    if e.get("event") != "new":
        continue
    for x in e.get("units", []):
        d = x.get("data")
        if not d:
            continue
        key = (norm(d["building"]["address"]), d["name"])
        if key in _seen:
            continue
        _seen.add(key)
        b = d["building"]
        LISTINGS.append({"address": key[0], "apt": d["name"], "sqft": d.get("sqft"), "beds": d.get("bedrooms"),
                         "rent": x.get("price"), "codes": [a["code"] for a in d.get("amenities") or []],
                         "layout": str((d.get("style") or {}).get("name") or ""),
                         "point": (float(b["latitude"]), float(b["longitude"])) if b.get("latitude") else None,
                         "source": "affordable"})
for L in LISTINGS:
    fl, _, letter = L["apt"].partition("-")
    L["line"] = letter.strip().upper()
    L["floor"] = int(fl) if fl.isdigit() else 0
    L["base"] = layout_base(L.get("layout"))
# Lines you've confirmed (a viewing, the leasing office): strong evidence. "ADDRESS|LETTER": {"faces": [...], "oval": bool}
NOTES = {k.upper(): v for k, v in json.load(open(os.path.join(INPUTS, "line_notes.json")))["lines"].items()
         if not k.upper().startswith("EXAMPLE")}
log("confirmed lines (line_notes.json):", len(NOTES))


def note_score(addr, letter, segs):
    """+5 when a confirmed line's windows match these walls, -5 when they don't, 0 with no note."""
    note = NOTES.get(f"{addr}|{letter}")
    if not note:
        return 0.0, 0
    faces = {geo.compass(s["facing"]) for s in segs}
    ok = True
    if note.get("faces"):
        ok &= all(any(geo.angle_between(geo.COMPASS.index(f) * 22.5, s["facing"]) <= 34 for s in segs) for f in note["faces"])
    if "oval" in note:
        ok &= bool(note["oval"]) == any(s["oval"] or view_ok("VBOV", s) for s in segs)
    return (5.0 if ok else -5.0), 1


LISTING_BY = {}
for L in LISTINGS:
    LISTING_BY.setdefault((L["address"], L["line"]), []).append(L)
ADDRESS_POINTS = {L["address"]: np.array(geo.to_xy(*L["point"])) for L in LISTINGS if L.get("point")}
TOPS = {}
for L in LISTINGS:
    if "FSTOP" in L["codes"]:
        TOPS[L["address"]] = max(TOPS.get(L["address"], 0), L["floor"])
log("listings", len(LISTINGS), "| top floors known for", len(TOPS), "addresses")

OVAL_C = np.array(geo.to_xy(*B["oval"]["center"]))
ESB = np.array(geo.to_xy(40.748440, -73.985664))


def view_ok(code, seg):
    f = seg["facing"]
    if code == "VBOV":
        return seg["oval"] or geo.angle_between(f, bearing_of(OVAL_C - seg["mid"])) < 50
    if code == "VBRV":
        return geo.angle_between(f, 75) < 75
    if code == "VBESV":
        return geo.angle_between(f, bearing_of(ESB - seg["mid"])) < 75
    return True


# ================================================================== CBRE plans
CB = json.load(open(os.path.join(INPUTS, "cbre_plans.json")))["pages"]
LABELS = {(int(pg), lab): tuple(xy) for pg, d in CB.items() for lab, xy in d["labels"].items()}
RSF = {lab: v for d in CB.values() for lab, v in d["sizes"].items()}
PCV_RSF = {"A": 949, "B": 1221, "C": 1220, "D": 942, "E": 955, "F": 1220, "G": 1221, "H": 941}

# Each apartment's windows, as directions on the page (U up, D down, L left, R right),
# read off the plans by eye (see the plan images in the PR/issue for checking).
EXPO = {}


def _put(prefixes, table):
    for p in prefixes:
        for k, v in table.items():
            EXPO[f"{p}-{k}"] = v


_put(["444", "446"], {"F": "L", "G": "R", "E": "LU", "D": "LD", "H": "RU", "A": "RD", "C": "L", "B": "R"})
_put(["442"], {"F": "L", "G": "R", "E": "LU", "D": "LD", "H": "RU", "A": "RD", "C": "DL", "B": "DR"})
_put(["448"], {"F": "UL", "G": "UR", "E": "U", "D": "D", "H": "RU", "A": "RD", "C": "L", "B": "R"})
_put(["450"], {"A": "UL", "B": "UR", "C": "U", "D": "D", "E": "D", "H": "LU", "G": "DL", "F": "DR"})
_put(["445"], {"F": "UL", "G": "UR", "E": "LU", "D": "LD", "H": "RU", "A": "RD", "C": "L", "B": "R"})
_put(["435"], {"F": "L", "G": "R", "E": "LU", "D": "LD", "H": "RU", "A": "RD", "C": "DL", "B": "DR"})
_put(["278"], {"G": "UL", "H": "UR", "F": "UL", "E": "DL", "D": "D", "A": "RU", "C": "L", "B": "R"})
_put(["276", "274"], {"F": "L", "G": "R", "E": "LU", "D": "LD", "H": "RU", "A": "RD", "C": "L", "B": "R"})
_put(["272"], {"F": "L", "G": "R", "D": "UL", "E": "U", "H": "RD", "C": "DL", "B": "DL", "A": "DR"})
_put(["280"], {"B": "UL", "C": "UR", "A": "LU", "H": "LD", "D": "RU", "E": "RD", "G": "DL", "F": "DR"})
_put(["400"], {"B": "UL", "C": "UR", "A": "LU", "H": "LD", "D": "U", "E": "D", "G": "L", "F": "R"})
_put(["410"], {"F": "U", "E": "D", "G": "UL", "H": "UR", "D": "D", "A": "RU", "C": "DL", "B": "DR"})
_put(["330"], {"C": "L", "D": "R", "B": "LU", "A": "LD", "E": "R", "F": "UR", "G": "DR", "H": "DL"})
PCV_EXPO = {"A": "LU", "B": "UL", "C": "UR", "D": "RU", "E": "RD", "F": "DR", "G": "DL", "H": "LD"}

# name, page, outline box on the page (PDF points), an address of the building drawn, page turned (top = ESE)?
PLANS = [
    ("442-450 E 20th", 39, (130.9, 65.5, 496.1, 756.7), "442 EAST 20TH STREET", True),
    ("435-445 E 14th", 36, (314.2, 86.4, 577.3, 710.2), "435 EAST 14TH STREET", True),
    ("330 First / 400-410 E 20th", 37, (36.0, 140.1, 575.3, 680.1), "330 FIRST AVENUE", False),
    ("272-278 First", 38, (317.5, 64.1, 579.3, 767.1), "272 FIRST AVENUE", False),
    ("280 First", 38, (45.8, 255.3, 362.0, 571.4), "280 FIRST AVENUE", False),
]
PCV_BOX = (28.1, 102.8, 266.4, 710.8)


def fp_of(addr):
    return next(f for f in FPS if addr in f["addresses"])


def plan_to_xy(plan, x, y):
    _, _, (x0, y0, x1, y1), ref, turned = plan
    pts = np.array(fp_of(ref)["poly"].exterior.coords)
    us, vs = pts @ GU, pts @ GV
    uc, vc = (us.min() + us.max()) / 2, (vs.min() + vs.max()) / 2
    xc, yc = (x0 + x1) / 2, (y0 + y1) / 2
    if turned:
        s = ((us.max() - us.min()) / (y1 - y0) + (vs.max() - vs.min()) / (x1 - x0)) / 2
        u, v = uc - s * (y - yc), vc - s * (x - xc)
        dirs = {"U": GU, "D": -GU, "R": -GV, "L": GV}
    else:
        s = ((us.max() - us.min()) / (x1 - x0) + (vs.max() - vs.min()) / (y1 - y0)) / 2
        u, v = uc + s * (x - xc), vc - s * (y - yc)
        dirs = {"U": GV, "D": -GV, "R": GU, "L": -GU}
    return u * GU + v * GV, dirs


def windows_in(seg_list, p, dir_vecs, limit=20):
    """For each window direction: the wall piece facing that way, in front of p and nearest to it."""
    chosen = []
    for d in dir_vecs:
        dbear = bearing_of(d)
        best = None
        for s in seg_list:
            if geo.angle_between(s["facing"], dbear) > 35:
                continue
            along = (np.dot(s["a"] - p, d) + np.dot(s["b"] - p, d)) / 2
            if along < 0.5:
                continue
            perp = np.array([d[1], -d[0]])
            la, lb = sorted((np.dot(s["a"] - p, perp), np.dot(s["b"] - p, perp)))
            lateral = 0 if la <= 0 <= lb else min(abs(la), abs(lb))
            sc = along + 2 * lateral
            if best is None or sc < best[0]:
                best = (sc, s)
        if best and best[0] < limit:
            chosen.append(best[1])
    return chosen


def transforms(ref_poly, tgt_poly, min_iou):
    """Turns (90-degree steps of the street grid) and mirrorings that lay one outline on the other."""
    rc = np.array([ref_poly.centroid.x, ref_poly.centroid.y])
    tc = np.array([tgt_poly.centroid.x, tgt_poly.centroid.y])
    out = []
    for mirror in (False, True):
        for rot in (0, 90, 180, 270):
            def T(pt, rot=rot, mirror=mirror):
                q = np.asarray(pt) - rc
                u, v = q @ GU, q @ GV
                if mirror:
                    u = -u
                r = math.radians(rot)
                u, v = u * math.cos(r) - v * math.sin(r), u * math.sin(r) + v * math.cos(r)
                return tc + u * GU + v * GV

            def Tdir(vec, T=T):
                return T(rc + vec) - T(rc)
            moved = Polygon([T(np.array(c)) for c in ref_poly.exterior.coords])
            iou = moved.intersection(tgt_poly).area / moved.union(tgt_poly).area
            if iou >= min_iou:
                out.append({"rot": rot, "mirror": mirror, "iou": iou, "T": T, "Tdir": Tdir})
    return out


def address_points(fp):
    pts = {}
    for w in fp["walls"]:
        (la1, lo1), (la2, lo2) = w["line"]
        pts.setdefault(w.get("address"), []).append(np.array(geo.to_xy((la1 + la2) / 2, (lo1 + lo2) / 2)))
    return {a: np.mean(v, axis=0) for a, v in pts.items() if a}


def evidence_score(units):
    sc, n = 0.0, 0
    for u in units:
        ns, nn = note_score(u["address"], u["line"], u["segs"])
        sc, n = sc + ns, n + nn
        for L in LISTING_BY.get((u["address"], u["line"]), []):
            if L.get("sqft") and u.get("rsf"):
                diff = abs(L["sqft"] - u["rsf"]) / u["rsf"]
                sc += 1.0 if diff < 0.04 else (0.3 if diff < 0.1 else -1.0)
                n += 1
            for code in ("VBOV", "VBRV", "VBESV"):
                if code in L["codes"]:
                    sc += 2.0 if any(view_ok(code, s) for s in u["segs"]) else -2.0
                    n += 1
    return sc, n


CANDS = []  # {address, line, fp, p, segs, weight, how ("plan"/"twin"/"slots"), label, source}

PROTO = {  # same CBRE design, outline drawn a bit differently by OpenStreetMap: footprint -> its real addresses
    "435-445 E 14th": {"510 EAST 20TH STREET": ["510 EAST 20TH STREET", "520 EAST 20TH STREET"],
                       "530 EAST 20TH STREET": ["530 EAST 20TH STREET", "540 EAST 20TH STREET"],
                       "610 EAST 20TH STREET": ["610 EAST 20TH STREET", "620 EAST 20TH STREET"]},
    "330 First / 400-410 E 20th": {"240 FIRST AVENUE": ["240 FIRST AVENUE", "250 FIRST AVENUE", "405 EAST 14TH STREET"],
                                   "245 AVENUE C": ["245 AVENUE C", "635 EAST 14TH STREET", "645 EAST 14TH STREET"]},
}
ALLOWED = {fp_of(k)["id"]: v for d in PROTO.values() for k, v in d.items()}
ALLOWED[fp_of("330 FIRST AVENUE")["id"]] = ["330 FIRST AVENUE", "400 EAST 20TH STREET", "410 EAST 20TH STREET"]


def plan_units(plan, tgt, tr):
    name, page = plan[0], plan[1]
    ref = fp_of(plan[3])
    apts = address_points(tgt)
    if tgt["id"] in ALLOWED:
        apts = {a: p for a, p in apts.items() if a in ALLOWED[tgt["id"]]}
    units = []
    for (pg, lab), (x, y) in LABELS.items():
        if pg != page or lab not in EXPO:
            continue
        p_ref, dirs = plan_to_xy(plan, x, y)
        if not ref["poly"].buffer(3).contains(Point(tuple(p_ref))):
            continue
        p = tr["T"](p_ref)
        if tgt is ref:
            addr = next(a for a in ref["addresses"] if a.startswith(lab.split("-")[0] + " "))
        else:
            addr = min(apts, key=lambda a: np.linalg.norm(apts[a] - p))
        units.append({"address": addr, "line": lab.split("-")[1], "p": p, "rsf": RSF.get(lab), "plan_unit": lab,
                      "segs": windows_in(SEGS_BY_FP[tgt["id"]], p, [tr["Tdir"](dirs[c]) for c in EXPO[lab]])})
    return units


def add(units, fp_id, weight, how, label, source):
    for u in units:
        CANDS.append({"address": u["address"], "line": u["line"], "fp": fp_id, "p": u["p"], "segs": u["segs"],
                      "weight": weight, "how": how, "label": label, "source": source})


DESIGN_REPORT = []
for plan in PLANS:
    ref = fp_of(plan[3])
    loose = {fp_of(k)["id"] for k in PROTO.get(plan[0], {})}
    for tgt in FPS:
        if tgt["complex"] != "Stuy Town":
            continue
        trs = transforms(ref["poly"], tgt["poly"], 0.55 if tgt["id"] in loose else 0.93)
        if not trs:
            continue
        is_ref = tgt is ref
        if is_ref:
            trs = [t for t in trs if t["rot"] == 0 and not t["mirror"]]
        hyps = []
        for tr in trs:
            us = plan_units(plan, tgt, tr)
            ev, n = evidence_score(us)
            hyps.append((ev, tr, us, n))
        hyps.sort(key=lambda h: (h[0], not h[1]["mirror"], h[1]["iou"]), reverse=True)
        ev, tr, us, n = hyps[0]
        if is_ref:
            label = "likely" if plan[0] == "280 First" else "sure"
            why = "CBRE floor plan of this building" + (" (a symmetrical cross: which wing is which is assumed)" if label == "likely" else "")
            add(us, tgt["id"], 1.0, "plan", label, why)
        else:
            gap = ev - (hyps[1][0] if len(hyps) > 1 else -99)
            top_iou = max(h[1]["iou"] for h in hyps)
            if n >= 3 and gap >= 3 and not tr["mirror"] and tr["iou"] >= 0.9:
                label = "sure"
            elif (n >= 2 and gap >= 1.5) or (not tr["mirror"] and tr["rot"] == 0 and tr["iou"] == top_iou and tgt["id"] not in loose):
                label = "likely"
            else:
                label = "either way"
            why = (f"same design as {plan[0]} ({'mirrored, ' if tr['mirror'] else ''}turned {tr['rot']} deg); "
                   f"{n} listing clue{'s' if n != 1 else ''} agree" + ("; outline only roughly matches" if tr["iou"] < 0.9 else ""))
            if label == "either way" and len(hyps) > 1:
                add(us, tgt["id"], 0.5, "twin", label, why + "; another fit is as good")
                add(hyps[1][2], tgt["id"], 0.5, "twin", label, why + "; another fit is as good")
            else:
                add(us, tgt["id"], 1.0, "twin", label, why)
        DESIGN_REPORT.append((plan[0], ", ".join(tgt["addresses"])[:60], CANDS[-1]["label"]))

# ---------------------------------------------------------------- Peter Cooper Village
for fp in [f for f in FPS if f["complex"] == "Peter Cooper Village"]:
    rect = fp["poly"].minimum_rotated_rectangle
    cs = np.array(rect.exterior.coords)[:4]
    e1, e2 = cs[1] - cs[0], cs[2] - cs[1]
    long_v = e1 if np.linalg.norm(e1) > np.linalg.norm(e2) else e2
    L_len = np.linalg.norm(long_v)
    long_v = long_v / L_len
    c = np.array([fp["poly"].centroid.x, fp["poly"].centroid.y])
    x0, y0, x1, y1 = PCV_BOX
    xc, yc, s = (x0 + x1) / 2, (y0 + y1) / 2, L_len / (y1 - y0)
    hyps = []
    for up in (1, -1):
        U = long_v * up
        R = np.array([U[1], -U[0]])
        dirs = {"U": U, "D": -U, "R": R, "L": -R}
        us = []
        for (pg, lab), (x, y) in LABELS.items():
            if pg != 36 or lab not in PCV_EXPO or not (x0 - 5 <= x <= x1 + 5):
                continue
            p = c + (x - xc) * s * R + (-(y - yc)) * s * U
            segs = windows_in(SEGS_BY_FP[fp["id"]], p, [dirs[k] for k in PCV_EXPO[lab]])
            # one building can have two street numbers (390 First Avenue / 390 East 23rd Street)
            for addr in fp["addresses"]:
                us.append({"address": addr, "line": lab, "p": p, "rsf": PCV_RSF[lab], "plan_unit": "PCV-" + lab, "segs": segs})
        ev, n = evidence_score(us)
        hyps.append((ev, us, n))
    hyps.sort(key=lambda h: h[0], reverse=True)
    views = sum(1 for (a, _), ls in LISTING_BY.items() if a in fp["addresses"] for L in ls if set(L["codes"]) & {"VBRV", "VBESV"})
    if views and hyps[0][0] - hyps[1][0] >= 3:
        add(hyps[0][1], fp["id"], 1.0, "plan", "likely", f"Peter Cooper Village typical plan, turned to fit {views} river / Empire State view tag(s)")
    else:
        why = "Peter Cooper Village typical plan; which end the letters start from isn't known for this building yet"
        add(hyps[0][1], fp["id"], 0.5, "plan", "either way", why)
        add(hyps[1][1], fp["id"], 0.5, "plan", "either way", why)

# ================================================================== slot model (designs with no plan)


def fp_axes(fp):
    acc = np.zeros(2)
    for w in fp["walls"]:
        a = math.radians((w["facing"] % 90) * 4)
        acc += w["length_m"] * np.array([math.cos(a), math.sin(a)])
    base = (math.degrees(math.atan2(acc[1], acc[0])) / 4) % 90
    return [base, base + 90, base + 180, base + 270]


def extent(poly, c, bearing, max_m=90.0):
    d = unit_vec(bearing)
    t = 0.0
    while t < max_m:
        if not poly.contains(Point(*(c + d * (t + 0.5)))):
            return t
        t += 0.5
    return max_m


def refine_core(poly, g, axes, radius=12.0):
    """The listing's point is the entrance, on the building's edge: find the middle of its cross."""
    best, pts = -1, []
    for dx in np.arange(-radius, radius + 0.1, 1.0):
        for dy in np.arange(-radius, radius + 0.1, 1.0):
            q = g + np.array([dx, dy])
            if not poly.contains(Point(*q)):
                continue
            sc = round(min(extent(poly, q, axes[0]) + extent(poly, q, axes[2]),
                           extent(poly, q, axes[1]) + extent(poly, q, axes[3])))
            if sc > best:
                best, pts = sc, [q]
            elif sc == best:
                pts.append(q)
    if not pts:
        return g
    pts = np.array(pts)
    dist = np.linalg.norm(pts - g, axis=1)
    return pts[dist <= dist.min() + 6].mean(axis=0)


def module_slots(poly, c, axes, cores):
    """The apartments round one core: two per wing, in clockwise order."""
    slots = []
    for b in axes:
        d = unit_vec(b)
        L = extent(poly, c, b)
        if L < 5:
            continue
        conn = None
        for c2 in cores:
            v = c2 - c
            along = float(v @ d)
            if along > 3 and abs(v[0] * d[1] - v[1] * d[0]) < 8 and along < L + 8:
                conn = along if conn is None else min(conn, along)
        reach, free = (conn / 2, False) if conn is not None else (L, L <= 26)
        for side in (1, -1):
            sv = np.array([d[1], -d[0]]) * side
            slots.append({"p": c + d * max(5.0, (4 + reach) / 2) + sv * 3.0, "arm": b % 360, "conn": conn is not None,
                          "dirs": [unit_vec((b + 90 * side) % 360)] + ([d] if free else [])})
    slots.sort(key=lambda q: bearing_of(q["p"] - c))
    return slots


covered = {c["fp"] for c in CANDS}
GROUPS = {}
for fp in FPS:
    if fp["complex"] == "Stuy Town" and fp["id"] not in covered:
        # plus listed addresses whose entrance is on this building but which OpenStreetMap doesn't name (7 Stuyvesant Oval)
        extra = [a for a, pt in ADDRESS_POINTS.items() if a not in fp["addresses"] and fp["poly"].buffer(4).contains(Point(*pt))]
        GROUPS[fp["id"]] = {"poly": fp["poly"], "segs": list(SEGS_BY_FP[fp["id"]]),
                            "addresses": list(fp["addresses"]) + extra, "fp": fp}
for k in [k for k, g in GROUPS.items() if g["poly"].area < 600]:  # OpenStreetMap splits 5 Stuyvesant Oval in two
    other = next((k2 for k2, g2 in GROUPS.items() if k2 != k and g2["poly"].distance(GROUPS[k]["poly"]) < 0.5), None)
    if other:
        g2 = GROUPS[other]
        g2["poly"] = g2["poly"].union(GROUPS[k]["poly"]).buffer(0.3).buffer(-0.3)
        g2["segs"] += GROUPS[k]["segs"]
        g2["addresses"] = sorted(set(g2["addresses"]) | set(GROUPS[k]["addresses"]))
        del GROUPS[k]
for fp in FPS:  # an address left without apartments in a planned building (e.g. 251 Avenue C)
    if fp["complex"] == "Stuy Town" and fp["id"] in covered:
        have = {c["address"] for c in CANDS if c["fp"] == fp["id"]}
        missing = [a for a in fp["addresses"] if a not in have and a in ADDRESS_POINTS]
        if missing:
            GROUPS[fp["id"] + "+"] = {"poly": fp["poly"], "segs": list(SEGS_BY_FP[fp["id"]]), "addresses": missing,
                                      "fp": fp, "partial": True}


def cores_for(g):
    axes = fp_axes(g["fp"])
    pts = address_points(g["fp"])
    found = {}
    for a in g["addresses"]:
        if a in ADDRESS_POINTS and g["poly"].buffer(4).contains(Point(*ADDRESS_POINTS[a])):
            found[a] = (refine_core(g["poly"], ADDRESS_POINTS[a], axes), "listing")
        elif a in pts:
            found[a] = (refine_core(g["poly"], pts[a], axes), "map")
    keep = {}
    for a, (c, src) in sorted(found.items(), key=lambda kv: kv[1][1] != "listing"):
        if all(np.linalg.norm(c - c2) > 9 for c2, _ in keep.values()):
            keep[a] = (c, src)
    return axes, keep


def slots_for(g):
    axes, cores = cores_for(g)
    all_c = [c for c, _ in cores.values()]
    out = {}
    for a, (c, src) in cores.items():
        sl = module_slots(g["poly"], c, axes, [c2 for c2 in all_c if c2 is not c])
        for q in sl:
            q["segs"] = windows_in(g["segs"], q["p"], q["dirs"]) or windows_in(g["segs"], q["p"], q["dirs"], limit=40)
        out[a] = (c, sl)
    return out


# layout shape -> one-sided or two-sided apartment, learnt where we're sure
BASE_SIDES = {}
for c in CANDS:
    if c["label"] == "sure" and c["weight"] == 1.0:
        n = min(2, len({round(s["facing"] / 30) for s in c["segs"]}))
        for L in LISTING_BY.get((c["address"], c["line"]), []):
            if L.get("base"):
                BASE_SIDES.setdefault(L["base"], set()).add(n)
BASE_SIDES = {b: next(iter(v)) for b, v in BASE_SIDES.items() if len(v) == 1}


def chain_prior(slots, k):
    """In every planned building that's a chain of crosses, B and C sit on the wing toward one
    neighbouring entrance (or the chain's end), F and G toward the other, A/H and D/E on the
    cross wings. So A is on a cross wing and B on a chain wing. 0 when that holds, -2 when not."""
    chain = {round(q["arm"]) % 180 for q in slots if q.get("conn")}
    n = len(slots)
    if not chain or n != 8:
        return 0.0
    on_chain = [round(q["arm"]) % 180 in chain for q in slots]
    return 0.0 if (not on_chain[k % n] and on_chain[(k + 1) % n]) else -2.0


def slot_evidence(addr, slots, k, mirror):
    """How well 'line A is in spot k' fits this address's listings."""
    n = len(slots)
    sc = 0.0
    for i, letter in enumerate(LETTERS[:n]):
        q = slots[(k - i) % n] if mirror else slots[(k + i) % n]
        sc += note_score(addr, letter, q["segs"])[0]
        for L in LISTING_BY.get((addr, letter), []):
            for code in ("VBOV", "VBRV", "VBESV"):
                if code in L["codes"]:
                    sc += 2.0 if any(view_ok(code, s) for s in q["segs"]) else -2.0
            if "VBOV" not in L["codes"] and q["segs"] and all(s["oval"] for s in q["segs"]):
                sc -= 0.3
            want = BASE_SIDES.get(L.get("base"))
            if want:
                sc += 0.6 if min(2, len({round(s["facing"] / 30) for s in q["segs"]})) == want else -0.6
    return sc


gids = [k for k in GROUPS if not GROUPS[k].get("partial")]
design_of = {}
for gid in gids:
    if gid in design_of:
        continue
    design_of[gid] = gid
    for g2 in gids:
        if g2 not in design_of and transforms(GROUPS[gid]["poly"], GROUPS[g2]["poly"], 0.9):
            design_of[g2] = gid
designs = {}
for gid, rep in design_of.items():
    designs.setdefault(rep, []).append(gid)
for gid in GROUPS:
    if GROUPS[gid].get("partial"):
        designs[gid] = [gid]

def make_emitter(rep, members, per, options, member_score, rep_addrs):
    def emit_member(gid, opt, share, combo, i):
        mirror, amap, _, how = options[gid][opt]
        g = GROUPS[gid]
        for a in rep_addrs:
            if a not in amap:
                continue
            b, delta = amap[a]
            sl = per[gid][b][1]
            n = len(sl)
            cmb = combo[:i] + (opt,) + combo[i + 1:]
            sc = np.array([chain_prior(per[rep][a][1], k) + sum(member_score(m, cmb[j], a, k) for j, m in enumerate(members))
                           for k in range(n)])
            w = np.exp(sc - sc.max())
            w = share * w / w.sum()
            clues = sum(1 for letter in LETTERS for _ in LISTING_BY.get((b, letter), []))
            pooled = sum(1 for m in members for x in per[m] for letter in LETTERS for _ in LISTING_BY.get((x, letter), []))
            why = (f"no floor plan for this design: apartments placed round the core from the building outline, "
                   f"letters clockwise; {clues} listing{'s' if clues != 1 else ''} here and {pooled - clues} in "
                   f"{len(members) - 1} other building(s) of the same design decide where A is" + (f" ({how})" if how else ""))
            for k in range(n):
                if w[k] < 0.01:
                    continue
                start = (delta - k) % n if mirror else (delta + k) % n
                for j, letter in enumerate(LETTERS[:n]):
                    q = sl[(start - j) % n] if mirror else sl[(start + j) % n]
                    CANDS.append({"address": b, "line": letter, "fp": g["fp"]["id"], "p": q["p"], "segs": q["segs"],
                                  "weight": float(w[k]), "how": "slots", "label": None, "source": why})
    return emit_member


for rep, members in designs.items():
    per = {gid: slots_for(GROUPS[gid]) for gid in members}
    rep_addrs = list(per[rep].keys())
    # each member: possible outline transforms, the address each rep address lands on, and how its spots line up
    options = {}
    for gid in members:
        if gid == rep:
            options[gid] = [(False, {a: (a, 0) for a in rep_addrs}, 0.0, "")]
            continue
        opts = []
        for tr in transforms(GROUPS[rep]["poly"], GROUPS[gid]["poly"], 0.9):
            amap = {}
            for a in rep_addrs:
                p = tr["T"](per[rep][a][0])
                near = min(per[gid].items(), key=lambda kv: np.linalg.norm(kv[1][0] - p), default=None)
                if not near or np.linalg.norm(near[1][0] - p) > 10:
                    continue
                b, (cb, slb) = near
                if len(slb) != len(per[rep][a][1]) or not slb:
                    continue
                first = tr["T"](per[rep][a][1][0]["p"])
                delta = min(range(len(slb)), key=lambda j: np.linalg.norm(slb[j]["p"] - first))
                amap[a] = (b, delta)
            opts.append((tr["mirror"], amap, -0.3 if tr["mirror"] else 0.0,
                         f"{'mirrored, ' if tr['mirror'] else ''}turned {tr['rot']} deg from {', '.join(GROUPS[rep]['addresses'][:1])}"))
        options[gid] = opts or [(False, {}, 0.0, "")]

    def member_score(gid, opt, a, k):
        mirror, amap, _, _ = options[gid][opt]
        if a not in amap:
            return 0.0
        b, delta = amap[a]
        sl = per[gid][b][1]
        n = len(sl)
        return slot_evidence(b, sl, (delta - k) % n if mirror else (delta + k) % n, mirror)

    best = None
    for combo in itertools.product(*[range(len(options[g])) for g in members]):
        total, scores = 0.0, {}
        for a in rep_addrs:
            n = len(per[rep][a][1])
            scores[a] = [chain_prior(per[rep][a][1], k) + sum(member_score(g, combo[i], a, k) for i, g in enumerate(members))
                         for k in range(n)]
            total += max(scores[a]) if n else 0
        total += sum(options[g][combo[i]][2] for i, g in enumerate(members))
        if best is None or total > best[0]:
            best = (total, combo, scores)
    best_total, combo, scores = best
    emit_member = make_emitter(rep, members, per, options, member_score, rep_addrs)

    def total_for(cmb):
        t = 0.0
        for a in rep_addrs:
            n = len(per[rep][a][1])
            if n:
                t += max(chain_prior(per[rep][a][1], k) + sum(member_score(g, cmb[i], a, k) for i, g in enumerate(members))
                         for k in range(n))
        return t + sum(options[g][cmb[i]][2] for i, g in enumerate(members))

    for i, gid in enumerate(members):
        # every way of laying the design onto this building that fits as well as the best one:
        # spread the answer over them instead of picking one at random
        tied = [o for o in range(len(options[gid]))
                if total_for(combo[:i] + (o,) + combo[i + 1:]) >= best_total - 0.5]
        for o in tied:
            emit_member(gid, o, 1.0 / len(tied), combo, i)



# ================================================================== per address: spots and lines
ADDR = {}
for c in CANDS:
    A = ADDR.setdefault(c["address"], {"fp": c["fp"], "spots": [], "lines": {}})
    key = (round(float(c["p"][0])), round(float(c["p"][1])), tuple(sorted(id(s) for s in c["segs"])))
    idx = next((i for i, sp in enumerate(A["spots"]) if sp["key"] == key), None)
    if idx is None:
        A["spots"].append({"key": key, "p": c["p"], "segs": c["segs"]})
        idx = len(A["spots"]) - 1
    line = A["lines"].setdefault(c["line"], {"w": {}, "how": c["how"], "label": c["label"], "source": c["source"]})
    line["w"][idx] = line["w"].get(idx, 0) + c["weight"]


def faces_of(segs):
    return sorted({geo.compass(s["facing"]) for s in segs}, key=lambda x: geo.COMPASS.index(x))


def top_floor(addr, fp):
    if fp["complex"] == "Peter Cooper Village":
        return TOPS.get(addr, 14)
    return TOPS.get(addr) or (12 if fp["floors"] >= 14 else 11)


OUT_ADDR = {}
for addr, A in sorted(ADDR.items()):
    fp = FP_BY_ID[A["fp"]]
    top = top_floor(addr, fp)
    used = sorted({i for ln in A["lines"].values() for i in ln["w"]})
    remap = {i: j for j, i in enumerate(used)}
    spots = []
    for i in used:
        sp = A["spots"][i]
        sun = []
        for f in range(1, top + 1):
            mask = np.zeros(len(SUN_AZ), bool)
            for s in sp["segs"]:
                mask |= seg_lit(s, z_of(fp["complex"], f))
            sun.append(sun_stats(mask))
        spots.append({"faces": faces_of(sp["segs"]), "oval": any(s["oval"] for s in sp["segs"]),
                      "street": sorted({s["street"] for s in sp["segs"] if s["street"]}),
                      "xy": [round(float(v), 1) for v in sp["p"]], "sun": sun})
    lines = {}
    for letter, ln in sorted(A["lines"].items()):
        tot = sum(ln["w"].values())
        ws = sorted(((remap[i], w / tot) for i, w in ln["w"].items()), key=lambda t: -t[1])
        # how sure: by direction, not by exact spot (two spots on one wall face the same way)
        by_faces = {}
        for i, w in ws:
            k = tuple(spots[i]["faces"])
            by_faces[k] = by_faces.get(k, 0) + w
        # chance that a window faces each way, that it looks at the Oval, that it's on a street
        p_dir = {}
        for i, w in ws:
            for f in spots[i]["faces"]:
                p_dir[f] = p_dir.get(f, 0) + w
        p_dir = {f: round(v, 2) for f, v in sorted(p_dir.items(), key=lambda kv: -kv[1])}
        p_oval = round(sum(w for i, w in ws if spots[i]["oval"]), 2)
        p_street = round(sum(w for i, w in ws if spots[i]["street"]), 2)
        main = max(p_dir.values()) if p_dir else 0
        label = ln["label"]
        if ln["how"] == "slots":
            label = "likely" if main >= 0.8 else ("either way" if main >= 0.5 else "unknown")
        if f"{addr}|{letter}" in NOTES:
            label = "sure"
        lines[letter] = {"spots": [[i, round(w, 3)] for i, w in ws], "confidence": label,
                         "p_faces": p_dir, "p_oval": p_oval, "p_street": p_street,
                         "how": ln["how"], "source": ln["source"]}
    OUT_ADDR[addr] = {"complex": fp["complex"], "building": A["fp"], "top_floor": top,
                      "top_floor_known": addr in TOPS, "spots": spots, "lines": lines}

# addresses with listings but no model at all (shouldn't happen, but say so)
missing = sorted({L["address"] for L in LISTINGS} - set(OUT_ADDR))
if missing:
    log("no model for:", missing)

# ================================================================== write
os.makedirs(OUT, exist_ok=True)
now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
lookup = {
    "about": ("Which way each StuyTown / Peter Cooper Village apartment line faces and how much direct sun it "
              "gets, by floor. Built by tools/sun_map/build.py; read with unit_sun.py. For each address: "
              "'spots' are apartment positions on a typical floor (windows' compass directions, Oval, street, "
              "and sun per floor 1..top_floor as [morning h, afternoon h, Dec 21 h, Dec 21 morning h], the first "
              "two averaged over the 21st of every month); 'lines' gives, for each letter, which spot(s) it "
              "is, with probabilities. Confidence: sure (the building's own plan, or a same-design building "
              "confirmed by listings), likely, either way (two fits equally good), unknown."),
    "built_utc": now,
    "inputs": {"osm_fetched_utc": B.get("fetched_utc"), "listings": len(LISTINGS)},
    "addresses": OUT_ADDR,
}
with open(os.path.join(OUT, "unit_lines.json"), "w") as f:
    json.dump(lookup, f, separators=(",", ":"), sort_keys=True)

with open(os.path.join(OUT, "units_by_floor.csv"), "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["address", "line", "confidence", "likely_faces", "p_oval", "p_street", "floor", "top_floor",
                "morning_h", "afternoon_h", "dec21_h", "dec21_morning_h"])
    for addr, A in OUT_ADDR.items():
        for letter, ln in A["lines"].items():
            for fl in range(1, A["top_floor"] + 1):
                exp = [sum(wt * A["spots"][i]["sun"][fl - 1][k] for i, wt in ln["spots"]) for k in range(4)]
                faces = " ".join(f"{f}:{p:.0%}" for f, p in ln["p_faces"].items())
                w.writerow([addr, letter, ln["confidence"], faces, ln["p_oval"], ln["p_street"],
                            fl, A["top_floor"]] + [round(x, 1) for x in exp])


def xy(p):
    return [round(float(p[0]), 1), round(float(p[1]), 1)]


seg_out = []
for s in SEGS:
    fp = FP_BY_ID[s["fp"]]
    top = 14 if fp["complex"] == "Peter Cooper Village" else 12
    seg_out.append({"fp": s["fp"], "address": s["address"], "compass": geo.compass(s["facing"]), "facing": round(s["facing"]),
                    "street": s["street"], "oval": s["oval"], "xy": [round(float(v), 1) for v in (*s["a"], *s["b"])],
                    "f": [sun_stats(seg_lit(s, z_of(fp["complex"], f))) for f in range(1, top + 1)]})
page = {
    "built_utc": now,
    "grid_bearing": geo.GRID_BEARING,
    "buildings": [{"id": f["id"], "complex": f["complex"], "addresses": f["addresses"],
                   "ring": [xy(c) for c in f["poly"].exterior.coords]} for f in FPS],
    "oval": [xy(geo.to_xy(a, b)) for a, b in B["oval"]["outline"]],
    "segments": seg_out,
    "addresses": OUT_ADDR,
    "listings": [{k: L.get(k) for k in ("address", "apt", "line", "floor", "sqft", "beds", "rent", "source")} |
                 {"codes": [c for c in L["codes"] if c in ("VBOV", "VBRV", "VBESV", "FSTOP")]} for L in LISTINGS],
}
template = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
fragment = template.replace("__DATA__", json.dumps(page, separators=(",", ":")).replace("</", "<\\/"))
with open(os.path.join(OUT, "sun_map.html"), "w", encoding="utf-8") as f:  # a whole page: open it in any browser
    f.write('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n</head>\n<body>\n'
            + fragment + "\n</body>\n</html>\n")
if os.environ.get("SUN_MAP_FRAGMENT"):  # the same page without <html>/<head>, for hosts that add their own
    with open(os.environ["SUN_MAP_FRAGMENT"], "w", encoding="utf-8") as f:
        f.write(fragment)

conf = {}
for A in OUT_ADDR.values():
    for ln in A["lines"].values():
        conf[ln["confidence"]] = conf.get(ln["confidence"], 0) + 1
log(f"wrote data/sun: {len(OUT_ADDR)} addresses, {sum(conf.values())} lines {conf}")
for r in DESIGN_REPORT:
    log("  ", r)
