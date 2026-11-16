"""
Automated checks for unit_sun.py (which way an apartment faces, how much sun)
and the sun map's data -- no network.

Run from the repo folder:
    python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import unit_sun  # noqa: E402

# One made-up building: line A is sure (one spot), line B is either of two spots.
# per floor 1-3: [morning h, afternoon h, Dec 21 h, Dec 21 morning h, sun score]
SUN_SSW = [[1.0, 3.0, 2.0, 0.5, 40], [2.0, 5.0, 6.0, 2.0, 70], [3.5, 5.5, 7.0, 2.5, 90]]
SUN_NNE = [[0.5, 0.0, 0.0, 0.0, 2], [0.6, 0.0, 0.0, 0.0, 4], [0.7, 0.0, 0.0, 0.0, 6]]
FAKE = {"addresses": {"1 TEST OVAL": {
    "complex": "Stuy Town", "building": "way/1", "top_floor": 3, "top_floor_known": True,
    "spots": [{"faces": ["ESE", "SSW"], "oval": True, "street": [], "xy": [0, 0], "sun": SUN_SSW},
              {"faces": ["NNE"], "oval": False, "street": ["20th St"], "xy": [0, 9], "sun": SUN_NNE}],
    "lines": {
        "A": {"spots": [[0, 1.0]], "confidence": "sure", "p_faces": {"ESE": 1.0, "SSW": 1.0}, "p_oval": 1.0,
              "p_street": 0.0, "how": "plan", "source": "test plan", "score": [40, 70, 90], "pct": [80, 96, 99]},
        "B": {"spots": [[0, 0.5], [1, 0.5]], "confidence": "either way", "p_faces": {"ESE": 0.5, "SSW": 0.5, "NNE": 0.5},
              "p_oval": 0.5, "p_street": 0.5, "how": "slots", "source": "test slots", "score": [21, 37, 48],
              "pct": [45, 52, 60]}}}}}


class FakeFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = f"{tmp.name}/unit_lines.json"
        Path(self.path).write_text(json.dumps(FAKE), encoding="utf-8")


class Apartments(unittest.TestCase):
    def test_apartment_numbers(self):
        self.assertEqual(unit_sun.parse_apartment("07-F"), (7, "F"))
        self.assertEqual(unit_sun.parse_apartment("0M-D"), (0, "D"))
        self.assertEqual(unit_sun.parse_apartment("0T-A"), (-1, "A"))
        self.assertEqual(unit_sun.parse_apartment(""), (None, None))
        self.assertEqual(unit_sun.normalize_address(" 287  Avenue c "), "287 AVENUE C")


class Lookup(FakeFile):
    def test_a_sure_line(self):
        info = unit_sun.lookup("1 Test Oval", "02-A", path=self.path)
        self.assertEqual(info["faces"], ["ESE", "SSW"])
        self.assertEqual(info["confidence"], "sure")
        self.assertEqual((info["morning_h"], info["afternoon_h"], info["dec21_h"]), (2.0, 5.0, 6.0))
        self.assertEqual((info["sun_score"], info["top_percent"], info["rank_in_building"]), (70, 4, 1))
        text = unit_sun.describe("1 TEST OVAL", "02-A", path=self.path)
        self.assertTrue(text.startswith("Sun 70/100, top 4% on floor 2, #1 of 2 here · faces ESE + SSW (sure)"), text)
        self.assertIn("Oval view", text)
        self.assertIn("mornings: some (2 h), afternoons 5 h, Dec 21 6 h", text)
        self.assertEqual(unit_sun.short("1 TEST OVAL", "02-A", path=self.path), "70 top 4% · ESE+SSW Oval · 2/5 h")

    def test_a_unit_from_the_api(self):
        unit = {"name": "03-A", "building": {"address": "1 TEST OVAL"}}
        self.assertEqual(unit_sun.lookup(unit, path=self.path)["floor"], 3)

    def test_an_either_way_line_gives_both_sides(self):
        info = unit_sun.lookup("1 TEST OVAL", "02-B", path=self.path)
        self.assertEqual([o["p"] for o in info["options"]], [0.5, 0.5])
        self.assertEqual(info["morning_h"], 1.3)  # (2.0 + 0.6) / 2
        self.assertEqual((info["sun_score"], info["worst_score"], info["best_score"]), (37, 4, 70))
        self.assertEqual(info["rank_in_building"], 2)
        text = unit_sun.describe("1 TEST OVAL", "02-B", path=self.path)
        self.assertTrue(text.startswith("Sun 37/100, top 48% on floor 2, #2 of 2 here (could be 4-70)"), text)
        self.assertIn("faces ESE + SSW (50%) or NNE (50%)", text)
        self.assertIn("maybe an Oval view (50%)", text)
        self.assertIn("maybe onto 20th St (50%)", text)
        self.assertEqual(unit_sun.short("1 TEST OVAL", "02-B", path=self.path), "37 top 48% · ESE+SSW? (50%) Oval? · 1.3/2.5 h")
        # floor 3: a 50% chance of 3+ hours of morning sun
        self.assertEqual(unit_sun.lookup("1 TEST OVAL", "03-B", path=self.path)["p_good_morning"], 0.5)
        self.assertIn("50% chance of 3 h+", unit_sun.describe("1 TEST OVAL", "03-B", path=self.path))

    def test_low_ranks_say_bottom(self):
        self.assertEqual(unit_sun.rank_text(3), "bottom 3%")
        self.assertEqual(unit_sun.rank_text(96), "top 4%")
        self.assertEqual(unit_sun.rank_text(100), "top 1%")

    def test_floors_outside_the_building_use_the_nearest(self):
        self.assertEqual(unit_sun.lookup("1 TEST OVAL", "0M-A", path=self.path)["morning_h"], 1.0)
        top = unit_sun.lookup("1 TEST OVAL", "09-A", path=self.path)
        self.assertEqual(top["morning_h"], 3.5)
        self.assertIn("top floor", top["notes"])

    def test_unknown_things_say_nothing(self):
        self.assertIsNone(unit_sun.lookup("9 NOWHERE STREET", "02-A", path=self.path))
        self.assertIsNone(unit_sun.lookup("1 TEST OVAL", "02-Z", path=self.path))
        self.assertEqual(unit_sun.describe("1 TEST OVAL", "", path=self.path), "")
        self.assertEqual(unit_sun.short({}, path=self.path), "")
        self.assertEqual(unit_sun.describe("1 TEST OVAL", "02-A", path="/nonexistent/x.json"), "")


class MarketEvidence(unittest.TestCase):
    UNIT = {"unitSpk": "P~NYST31~B~001~U~02-F", "unitNumber": "02-F", "bedrooms": 2, "sqft": 932, "price": 6000,
            "property": {"name": "Stuyvesant Town"}, "floorplan": {"floorplanSvg": "/media/x/124.svg"},
            "amenities": [{"code": "VBOV"}, {"code": "FS02"}],
            "building": {"address": "1 STUYVESANT OVAL", "latitude": "40.7307", "longitude": "-73.9783"}}

    def test_new_listings_are_added_and_old_ones_kept(self):
        other = dict(self.UNIT, property={"name": "Parker Towers"}, unitSpk="P~X")
        data, new = unit_sun.merge_market({"units": [{"unitSpk": "OLD", "address": "2 STUYVESANT OVAL", "apt": "01-A"}]},
                                          [self.UNIT, other], now="2026-11-16T15:00:00Z")
        self.assertEqual(new, 1)
        self.assertEqual([r["unitSpk"] for r in data["units"]], ["P~NYST31~B~001~U~02-F", "OLD"])
        rec = data["units"][0]
        self.assertEqual((rec["layout"], rec["codes"], rec["apt"]), ("124", ["FS02", "VBOV"], "02-F"))
        again, new = unit_sun.merge_market(data, [self.UNIT], now="2026-11-17T15:00:00Z")
        self.assertEqual(new, 0)
        self.assertEqual(again["units"][0]["first_seen_utc"], "2026-11-16T15:00:00Z")
        self.assertEqual(again["units"][0]["last_seen_utc"], "2026-11-17T15:00:00Z")


class RealSunMap(unittest.TestCase):
    """The committed data/sun/unit_lines.json: there, complete, and right where the CBRE plans say."""

    def setUp(self):
        self.data = unit_sun.load()
        if not self.data:
            self.skipTest("data/sun/unit_lines.json not built")

    def test_every_listed_address_is_covered(self):
        market = json.loads((REPO_ROOT / "data" / "sun" / "market_seen.json").read_text(encoding="utf-8"))
        missing = {r["address"] for r in market["units"]} - set(self.data["addresses"])
        self.assertEqual(missing, set())

    def test_planned_lines(self):
        # From the CBRE floor plans: 330 First Avenue G is the ESE + SSW corner; 444 East 20th C is a one-sided NNE flat.
        self.assertEqual(unit_sun.lookup("330 FIRST AVENUE", "07-G")["faces"], ["ESE", "SSW"])
        self.assertEqual(unit_sun.lookup("330 FIRST AVENUE", "07-G")["confidence"], "sure")
        self.assertEqual(unit_sun.lookup("444 EAST 20TH STREET", "05-C")["faces"], ["NNE"])

    def test_higher_floors_get_at_least_as_much_sun(self):
        low, high = unit_sun.lookup("444 EAST 20TH STREET", "02-H"), unit_sun.lookup("444 EAST 20TH STREET", "09-H")
        self.assertGreater(high["dec21_h"], low["dec21_h"])
        self.assertGreater(high["sun_score"], low["sun_score"])

    def test_scores_rank_sunny_corners_over_dark_flats(self):
        corner = unit_sun.lookup("330 FIRST AVENUE", "07-G")   # ESE + SSW
        dark = unit_sun.lookup("444 EAST 20TH STREET", "07-C")  # NNE only
        self.assertGreaterEqual(corner["sun_score"], 75)
        self.assertLessEqual(corner["top_percent"], 5)
        self.assertLessEqual(dark["sun_score"], 5)
        self.assertEqual(dark["rank_text"], "bottom 1%")

    def test_lines_have_probabilities_that_add_up(self):
        for address, building in self.data["addresses"].items():
            for letter, line in building["lines"].items():
                self.assertAlmostEqual(sum(w for _, w in line["spots"]), 1.0, places=2, msg=f"{address} {letter}")
                self.assertEqual(len(line["score"]), building["top_floor"])
                self.assertTrue(all(0 <= v <= 100 for v in line["score"] + line["pct"]))
                for index, _ in line["spots"]:
                    self.assertEqual(len(building["spots"][index]["sun"]), building["top_floor"])


if __name__ == "__main__":
    unittest.main()
