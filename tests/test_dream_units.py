"""
Automated checks for the dream-unit ratings, the apartment atlas, the map,
and the sun and street-grid math under them -- no network, no browser.

Run from the repo folder:
    python -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import check_units  # noqa: E402
import dream_units  # noqa: E402
import fake_osm  # noqa: E402
import reference_data  # noqa: E402
import stuytown_geo as geo  # noqa: E402
import units_map  # noqa: E402


class GeoTests(unittest.TestCase):
    def test_south_facing_windows_get_the_most_winter_sun(self):
        winter = {name: geo.sun_hours(bearing) for name, bearing in
                  (("SSW", 209), ("ESE", 119), ("WNW", 299), ("NNE", 29))}
        self.assertGreater(winter["SSW"], 8)
        self.assertGreater(winter["SSW"], winter["ESE"])
        self.assertGreater(winter["ESE"], winter["WNW"])
        self.assertEqual(winter["NNE"], 0)

    def test_a_building_across_the_way_shades_the_low_floors(self):
        low = geo.sun_profile(209, floor=2, open_m=40)["winter"]
        high = geo.sun_profile(209, floor=11, open_m=40)["winter"]
        self.assertLess(low, 1)
        self.assertGreater(high, 6)

    def test_the_noon_sun_matches_the_almanac(self):
        from datetime import datetime, timezone
        azimuth, elevation = geo.sun_position(datetime(2026, 12, 21, 16, 56, tzinfo=timezone.utc))
        self.assertAlmostEqual(azimuth, 180, delta=2)
        self.assertAlmostEqual(elevation, 25.8, delta=0.5)  # 90 - 40.73 - 23.44

    def test_compass_and_which_ways_are_sunny(self):
        self.assertEqual([geo.compass(b) for b in (29, 119, 209, 299)], ["NNE", "ESE", "SSW", "WNW"])
        self.assertTrue(all(geo.is_good_facing(b) for b in (80, 119, 131.7, 164, 209, 236)))  # E ... SW
        self.assertFalse(any(geo.is_good_facing(b) for b in (74, 254, 299, 29)))  # ENE, WSW, WNW, NNE

    def test_zones_and_the_walk_to_the_l(self):
        on_14th = (40.731373, -73.98167)  # 405 E 14th St, as the listings place it
        self.assertEqual(geo.zone(*on_14th, "405 EAST 14TH STREET"), "14th St front")
        self.assertEqual(geo.zone(40.73165, -73.98032, "451 EAST 14TH STREET"), "interior")
        self.assertEqual(geo.zone(40.73296, -73.9794, "17 STUYVESANT OVAL"), "Oval")
        minutes, entrance = geo.walk_to_l(*on_14th)
        self.assertLess(minutes, 2)
        self.assertEqual(entrance, "First Ave")

    def test_floors_and_lines(self):
        self.assertEqual(geo.floor_number({"unitFloor": "06", "name": "06-E"}), 6)
        self.assertEqual(geo.floor_number({"unitFloor": "0M"}), 1)
        self.assertEqual(geo.line_letter({"name": "06-E"}), "E")
        self.assertIsNone(geo.line_letter({"name": "5A"}))


class SettingsTests(unittest.TestCase):
    def test_floors_are_read_like_people_write_them(self):
        self.assertEqual(dream_units.parse_floors("5-10"), (set(range(5, 11)), []))
        self.assertEqual(dream_units.parse_floors("5 - 7, 12")[0], {5, 6, 7, 12})
        self.assertEqual(dream_units.parse_floors("5 to 6, top")[1], ["top"])
        self.assertEqual(dream_units.floors_text({5, 6, 7, 12}), "5-7, 12")

    def test_the_defaults(self):
        self.assertEqual(dream_units.DREAM_RENT, {1: 2200.0, 2: 2700.0})
        self.assertEqual(dream_units.DREAM_FLOORS, set(range(5, 11)))
        self.assertEqual(dream_units.settings_text(), "1 bed <= $2,200, 2 bed <= $2,700; floors 5-10")


class WithReferenceData(unittest.TestCase):
    """Each test gets its own data folder with the fake buildings in it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        for target, name, value in ((reference_data, "BUILDINGS_FILE", f"{tmp.name}/buildings.json"),
                                    (reference_data, "FLOORPLANS_DIR", f"{tmp.name}/floorplans"),
                                    (dream_units, "ATLAS_FILE", f"{tmp.name}/atlas.json"),
                                    (dream_units, "OVERRIDES_FILE", f"{tmp.name}/line_overrides.json"),
                                    (units_map, "MAP_FILE", f"{tmp.name}/units.geojson"),
                                    (check_units, "EVENTS_FILE", f"{tmp.name}/events.json")):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        reference_data.write_json(reference_data.BUILDINGS_FILE, reference_data.analyze(fake_osm.answer()))

    def rate(self, *args, **kwargs):
        return dream_units.rate(fake_osm.unit_at(*args, **kwargs))


class RatingTests(WithReferenceData):
    def test_oval_views_from_the_north_side_on_a_good_floor_at_the_right_rent_is_a_dream(self):
        rating = self.rate("1", "Stuyvesant Oval", "06-A", price=2150, codes=("VBOV",))
        self.assertEqual(rating["tier"], "dream", rating)
        self.assertEqual(rating["facing"]["compass"], ["SSW"])
        self.assertIn("faces SSW", rating["why"])
        two_bed = self.rate("5", "Stuyvesant Oval", "08-C", bedrooms=2, price=2700, codes=("VBOV",))
        self.assertEqual(two_bed["tier"], "dream")
        self.assertEqual(two_bed["facing"]["compass"], ["ESE"])

    def test_one_thing_missing_is_almost_and_says_what(self):
        cases = {
            ("10", "Stuyvesant Oval", "06-A", 1, 2150): "its windows face NNE",    # the Oval's shady side
            ("1", "Stuyvesant Oval", "03-A", 1, 2150): "floor 3 isn't 5-10",
            ("1", "Stuyvesant Oval", "06-A", 2, 2900): "rent $2,900 is over $2,700",
        }
        for (number, street, apartment, beds, price), why in cases.items():
            rating = self.rate(number, street, apartment, bedrooms=beds, price=price, codes=("VBOV",))
            self.assertEqual((rating["tier"], rating["why"]), ("almost", why))

    def test_a_building_looking_at_the_oval_two_ways_is_a_possible_dream(self):
        rating = self.rate("8", "Stuyvesant Oval", "07-B", price=2100, codes=("VBOV",))
        self.assertEqual(rating["tier"], "possible dream")
        self.assertEqual(sorted(rating["facing"]["compass"]), ["ESE", "NNE"])

    def test_without_oval_views_the_direction_is_unknown(self):
        rating = self.rate("1", "Stuyvesant Oval", "06-A", price=2150)
        self.assertEqual(rating["tier"], "price match")
        self.assertIn("which way it faces isn't known", rating["misses"])
        self.assertIsNone(self.rate("1", "Stuyvesant Oval", "06-A", bedrooms=3, price=2150)["tier"])
        self.assertIsNone(self.rate("1", "Stuyvesant Oval", "06-A", price=4400, codes=("VBOV",))["tier"])

    def test_your_notes_make_great_light_possible_away_from_the_oval(self):
        reference_data.write_json(dream_units.OVERRIDES_FILE, {"lines": {
            "441 EAST 20TH STREET|C": {"facing": ["SSW"], "street": False},
            "EXAMPLE 441 EAST 20TH STREET|D": {"facing": ["SSW"], "street": False}}})
        pcv = self.rate("441", "East 20th Street", "07-C", price=2100, complex_name="Peter Cooper Village")
        self.assertEqual(pcv["tier"], "great light")
        self.assertEqual(pcv["facing"]["source"], "your notes (line_overrides.json)")
        self.assertEqual(self.rate("441", "East 20th Street", "07-D", price=2100)["tier"], "price match")

    def test_without_buildings_the_oval_side_comes_from_the_position(self):
        Path(reference_data.BUILDINGS_FILE).unlink()
        rating = self.rate("5", "Stuyvesant Oval", "06-C", price=2150, codes=("VBOV",))
        self.assertEqual(rating["facing"]["compass"], ["ESE"])
        self.assertIn("estimated", rating["facing"]["source"])
        self.assertEqual(rating["tier"], "dream")

    def test_oval_views_on_one_floor_count_for_the_whole_line(self):
        seen = fake_osm.unit_at("1", "Stuyvesant Oval", "09-A", price=2600, codes=("VBOV",))
        events = [{"event": "new", "detected_at_utc": "2026-10-12T11:15:00Z",
                   "units": [{"unit_id": seen["unitSpk"], "data": seen}]}]
        atlas = dream_units.build_atlas(events)
        line = atlas["lines"]["1 STUYVESANT OVAL|A"]
        self.assertEqual(line["facing"], ["SSW"])
        self.assertEqual(line["oval_views"], {"yes": 1, "no": 0})
        later = fake_osm.unit_at("1", "Stuyvesant Oval", "06-A", price=2150)  # listed without Oval views
        self.assertEqual(dream_units.rate(later, atlas=atlas)["facing"]["compass"], ["SSW"])

    def test_the_alert_says_when_it_is_a_dream(self):
        dream = fake_osm.unit_at("1", "Stuyvesant Oval", "06-A", price=2150, codes=("VBOV",))
        plain = fake_osm.unit_at("1", "Stuyvesant Oval", "06-B", price=2150)
        alert = check_units.qualifying_alert([dream, plain], auto_applying=False, note=dream_units.alert_note)
        self.assertIn("* Dream unit: Oval views, faces SSW", alert["message"])
        self.assertEqual(alert["message"].count("Dream unit"), 1)
        shady = fake_osm.unit_at("10", "Stuyvesant Oval", "06-A", price=2150, codes=("VBOV",))
        self.assertEqual(dream_units.alert_note(shady), "* Almost a dream unit: its windows face NNE")
        broken = check_units.qualifying_alert([dream], auto_applying=False, note=lambda u: 1 / 0)
        self.assertIn("Apt 06-A", broken["message"])  # a failing note never stops the alert


class MapTests(WithReferenceData):
    def events(self):
        units = [fake_osm.unit_at("1", "Stuyvesant Oval", "06-A", price=2150, codes=("VBOV",)),
                 fake_osm.unit_at("1", "Stuyvesant Oval", "03-B", price=2150),
                 fake_osm.unit_at("405", "East 14th Street", "05-D", price=3100)]
        return [{"event": "new", "detected_at_utc": "2026-10-12T11:15:00Z",
                 "units": [{"unit_id": u["unitSpk"], "data": u} for u in units]},
                {"event": "removed", "detected_at_utc": "2026-10-12T11:16:00Z",
                 "units": [{"unit_id": units[0]["unitSpk"], "listed_for_seconds": 41.0}]}]

    def test_the_map_has_the_buildings_their_walls_and_every_unit(self):
        line = units_map.write(self.events())
        self.assertEqual(line, "map: 3 units, 7 building outlines")
        data = json.loads(Path(units_map.MAP_FILE).read_text())
        self.assertEqual(data["type"], "FeatureCollection")
        by_title = {f["properties"].get("title"): f for f in data["features"]}
        north = by_title["1 Stuyvesant Oval"]
        self.assertEqual(north["properties"]["fill"], units_map.BUILDING_FILL["sunny Oval"])
        self.assertEqual(by_title["10 Stuyvesant Oval"]["properties"]["fill"], units_map.BUILDING_FILL["shady Oval"])
        ring = north["geometry"]["coordinates"][0]
        self.assertEqual(ring[0], ring[-1])
        self.assertTrue(-74.0 < ring[0][0] < -73.9 and 40.7 < ring[0][1] < 40.8)  # [lon, lat]
        dream = by_title["06-A, 1 Stuyvesant Oval"]["properties"]
        self.assertTrue(dream["Rating"].startswith("dream: Oval views"))
        self.assertEqual((dream["marker-symbol"], dream["Listed for"]), ("star", "41 s"))
        self.assertIn("The Oval", by_title)
        self.assertIn("Oval wall, sunny: faces SSW", by_title)
        self.assertEqual(sum(1 for f in data["features"] if f["properties"].get("marker-symbol") == "rail-metro"), 2)
        self.assertEqual(data["features"][-1]["properties"]["title"], "06-A, 1 Stuyvesant Oval")  # drawn on top

    def test_without_buildings_it_still_maps_the_units(self):
        Path(reference_data.BUILDINGS_FILE).unlink()
        self.assertEqual(units_map.write(self.events()), "map: 3 units, 0 building outlines")
        data = json.loads(Path(units_map.MAP_FILE).read_text())
        self.assertEqual(sum(1 for f in data["features"] if f["properties"].get("marker-symbol") == "building"), 2)

    def test_refresh_all_writes_the_atlas_and_map_even_when_nothing_can_be_fetched(self):
        reference_data.write_json(check_units.EVENTS_FILE, self.events())
        with mock.patch.object(reference_data, "fetch_floorplan", side_effect=OSError("offline")):
            notes = units_map.refresh_all(budget_seconds=5)
        self.assertTrue(any("couldn't fetch 108" in n for n in notes), notes)
        self.assertIn("atlas: 3 apartment lines", notes)
        self.assertIn("map: 3 units, 7 building outlines", notes)
        self.assertTrue(Path(dream_units.OVERRIDES_FILE).exists())


if __name__ == "__main__":
    unittest.main()
