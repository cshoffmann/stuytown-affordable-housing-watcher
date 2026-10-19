"""
A small fake Overpass (OpenStreetMap) answer for the tests: a round Oval
with buildings on each side of it, two on 14th St (one on the street, one
behind it), one in Peter Cooper Village, and one across First Ave that
isn't part of the complex. Laid out on the Manhattan grid (u toward Avenue
C, v uptown), like the real buildings.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stuytown_geo as geo  # noqa: E402

OVAL_U, OVAL_V = geo.to_grid(*geo.OVAL_CENTER)
OVAL_RADIUS = 60

# address -> (u, v of the middle, width along u, depth along v), relative to the Oval's middle
BUILDINGS = {
    ("1", "Stuyvesant Oval"): (0, 80, 60, 20),      # north of the Oval: its SSW wall looks at it
    ("10", "Stuyvesant Oval"): (0, -80, 60, 20),    # south: its NNE wall looks at it
    ("5", "Stuyvesant Oval"): (-80, 0, 20, 60),     # west: ESE
    ("8", "Stuyvesant Oval"): (-55, -55, 18, 18),   # south-west corner: ESE and NNE both look at it
}
STREET_BUILDINGS = {  # absolute grid positions
    ("405", "East 14th Street"): (-200, -165, 60, 20),   # on 14th St
    ("451", "East 14th Street"): (-200, -100, 60, 20),   # behind it
    ("441", "East 20th Street"): (0, 330, 60, 20),       # Peter Cooper Village
    ("300", "First Avenue"): (-380, 0, 20, 60),          # across First Ave: not ours
}


def node(u, v):
    lat, lon = geo.from_grid(u, v)
    return {"lat": round(lat, 7), "lon": round(lon, 7)}


def rectangle(u, v, width, depth):
    corners = [(u - width / 2, v - depth / 2), (u + width / 2, v - depth / 2),
               (u + width / 2, v + depth / 2), (u - width / 2, v + depth / 2)]
    ring = [node(*c) for c in corners]
    return ring + ring[:1]


def answer() -> dict:
    elements, next_id = [], iter(range(1000, 100000))
    for (number, street), (du, dv, w, d) in BUILDINGS.items():
        elements.append({"type": "way", "id": next(next_id), "tags": {"building": "apartments", "building:levels": "13"},
                         "geometry": rectangle(OVAL_U + du, OVAL_V + dv, w, d)})
        elements.append({"type": "node", "id": next(next_id), **node(OVAL_U + du, OVAL_V + dv),
                         "tags": {"addr:housenumber": number, "addr:street": street}})
    for (number, street), (u, v, w, d) in STREET_BUILDINGS.items():
        elements.append({"type": "way", "id": next(next_id),
                         "tags": {"building": "apartments", "addr:housenumber": number, "addr:street": street},
                         "geometry": rectangle(u, v, w, d)})
    loop = [node(OVAL_U + OVAL_RADIUS * math.cos(a / 24 * 2 * math.pi), OVAL_V + OVAL_RADIUS * math.sin(a / 24 * 2 * math.pi))
            for a in range(25)]
    elements.append({"type": "way", "id": next(next_id), "tags": {"highway": "service", "name": "Stuyvesant Oval"},
                     "geometry": loop})
    for name, start, end in (("East 14th Street", (-420, -205), (420, -205)),
                             ("East 20th Street", (-420, 270), (420, 270)),
                             ("First Avenue", (-335, -260), (-335, 560))):
        elements.append({"type": "way", "id": next(next_id), "tags": {"highway": "primary", "name": name},
                         "geometry": [node(*start), node(*end)]})
    return {"elements": elements}


def unit_at(number, street, apartment="06-A", bedrooms=1, price=2150.0, codes=("IBSS",), complex_name="Stuyvesant Town"):
    """A listing in one of the fake buildings, shaped like the real API's."""
    if (number, street) in BUILDINGS:
        du, dv, _, _ = BUILDINGS[(number, street)]
        lat, lon = geo.from_grid(OVAL_U + du, OVAL_V + dv)
    else:
        u, v, _, _ = STREET_BUILDINGS[(number, street)]
        lat, lon = geo.from_grid(u, v)
    return {
        "unitSpk": f"P~TEST~B~{number}~U~{apartment}", "name": apartment, "unitFloor": apartment.split("-")[0],
        "bedrooms": bedrooms, "bathrooms": 1, "price": price, "sqft": 760, "incomeRequirement": 70000,
        "finish": "Blank", "style": {"id": 1, "name": "108"}, "floorplan": {"floorplanSvg": "/media/abc/108.svg"},
        "amenities": [{"code": c, "friendlyDescription": c} for c in codes],
        "property": {"name": complex_name},
        "building": {"address": f"{number} {street}".upper(), "latitude": f"{lat:.6f}", "longitude": f"{lon:.6f}"},
    }
