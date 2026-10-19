"""
Automated checks for reference_data.py: reading OpenStreetMap's buildings
(which way each wall faces and what it looks at) and saving the layouts'
floorplans -- against a fake map and a local fake web server, never the
real sites.

Run from the repo folder:
    python -m unittest discover -s tests -v
"""

import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import fake_osm  # noqa: E402
import reference_data  # noqa: E402

FLOORPLAN_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100" width="200" height="100">'
                 '<rect x="5" y="5" width="190" height="90" fill="none" stroke="#000"/>'
                 '<text x="20" y="40">LIVING ROOM</text><text x="20" y="60"><tspan>18\'2" x 11\'6"</tspan></text>'
                 '<!-- "googleMapAPIKey": "AIzaSyA1234567890123456789012345678901" --></svg>')


class BuildingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = reference_data.analyze(fake_osm.answer())
        cls.by_address = {a: fp for fp in cls.result["footprints"] for a in fp["addresses"]}

    def walls(self, address, **where):
        return [w for w in self.by_address[address]["walls"] if all(w.get(k) == v for k, v in where.items())]

    def test_only_the_complexes_buildings_are_kept(self):
        self.assertEqual(sorted(self.by_address), [
            "1 STUYVESANT OVAL", "10 STUYVESANT OVAL", "405 EAST 14TH STREET", "441 EAST 20TH STREET",
            "451 EAST 14TH STREET", "5 STUYVESANT OVAL", "8 STUYVESANT OVAL"])
        self.assertEqual(self.by_address["441 EAST 20TH STREET"]["complex"], "Peter Cooper Village")
        self.assertEqual(self.result["oval"]["from"], "OpenStreetMap: the Stuyvesant Oval loop road")

    def test_each_wall_knows_whether_it_looks_at_the_oval(self):
        self.assertEqual([w["compass"] for w in self.walls("1 STUYVESANT OVAL", faces_oval=True)], ["SSW"])
        self.assertEqual([w["compass"] for w in self.walls("10 STUYVESANT OVAL", faces_oval=True)], ["NNE"])
        self.assertEqual([w["compass"] for w in self.walls("5 STUYVESANT OVAL", faces_oval=True)], ["ESE"])
        self.assertEqual(sorted(w["compass"] for w in self.walls("8 STUYVESANT OVAL", faces_oval=True)), ["ESE", "NNE"])
        self.assertTrue(self.walls("1 STUYVESANT OVAL", faces_oval=True)[0]["good"])
        self.assertEqual(self.by_address["1 STUYVESANT OVAL"]["oval_side"]["side"], "NNE")

    def test_streets_and_the_building_in_front(self):
        on_street = self.walls("405 EAST 14TH STREET", compass="SSW")[0]
        self.assertEqual(on_street["street"], "14th St")
        self.assertEqual(self.by_address["405 EAST 14TH STREET"]["zone"], "14th St front")
        behind = self.walls("451 EAST 14TH STREET", compass="SSW")[0]
        self.assertIsNone(behind["street"])
        self.assertEqual(behind["open_m"], 45)
        self.assertEqual(behind["winter_sun_h"]["2"], 0)  # shaded by the building in front
        self.assertGreater(behind["winter_sun_h"]["11"], 6)
        self.assertEqual(self.by_address["451 EAST 14TH STREET"]["zone"], "interior")
        self.assertEqual(self.walls("441 EAST 20TH STREET", compass="SSW")[0]["street"], "20th St")

    def test_an_outline_that_isnt_the_oval_is_not_used(self):
        answer = fake_osm.answer()
        loop = next(e for e in answer["elements"] if (e.get("tags") or {}).get("name") == "Stuyvesant Oval")
        loop["geometry"].append(fake_osm.node(0, -300))  # a driveway out to 14th St: the outline swallows buildings
        result = reference_data.analyze(answer)
        self.assertTrue(result["oval"]["from"].startswith("estimated"), result["oval"]["from"])
        oval = {a for fp in result["footprints"] for a in fp["addresses"] if any(w["faces_oval"] for w in fp["walls"])}
        self.assertIn("1 STUYVESANT OVAL", oval)
        self.assertNotIn("405 EAST 14TH STREET", oval)

    def test_addresses_are_written_the_listings_way(self):
        self.assertEqual(reference_data.normalize_address("330 1st Avenue"), "330 FIRST AVENUE")
        self.assertEqual(reference_data.normalize_address("405  East 14th Street"), "405 EAST 14TH STREET")
        self.assertEqual(reference_data.normalize_address("277 Ave C"), "277 AVENUE C")


class FakeSite(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/media/abc/108.svg":
            body = FLOORPLAN_SVG.encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


def chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


class FloorplanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for name, value in (("FLOORPLANS_DIR", f"{tmp.name}/floorplans"),
                            ("FLOORPLAN_HOSTS", ["http://127.0.0.1:9", f"http://127.0.0.1:{server.server_port}"]),
                            ("BUILDINGS_FILE", f"{tmp.name}/buildings.json"),
                            ("OVERPASS_URLS", ["http://127.0.0.1:9/api/interpreter"])):
            patcher = mock.patch.object(reference_data, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        unit = fake_osm.unit_at("1", "Stuyvesant Oval")
        missing = {**unit, "style": {"name": "999"}, "floorplan": {"floorplanSvg": "/media/zzz/999.svg"}}
        self.events = [{"event": "new", "units": [
            {"unit_id": "a", "apartment": "06-A", "address": "1 Stuyvesant Oval", "data": unit},
            {"unit_id": "b", "apartment": "07-A", "address": "1 Stuyvesant Oval", "data": missing}]}]

    def test_each_layouts_floorplan_is_saved_once_with_its_labels_and_no_keys(self):
        with mock.patch.object(reference_data, "render_pngs", return_value="PNGs: 0 saved"):
            note = reference_data.refresh_floorplans(self.events, time.monotonic() + 30)
        self.assertIn("2 layouts, 1 new", note)
        self.assertIn("couldn't fetch 999", note)
        saved = Path(self.tmp, "floorplans", "108.svg").read_text()
        self.assertIn("LIVING ROOM", saved)
        self.assertNotIn("AIza", saved)
        index = json.loads(Path(self.tmp, "floorplans", "index.json").read_text())
        self.assertEqual(index["108"]["labels"], ["LIVING ROOM", "18'2\" x 11'6\""])
        self.assertEqual(index["108"]["seen_in"], ["06-A, 1 Stuyvesant Oval"])
        self.assertIn("last_error", index["999"])
        with mock.patch.object(reference_data, "fetch_floorplan", side_effect=AssertionError("fetched twice")), \
                mock.patch.object(reference_data, "render_pngs", return_value=""):
            reference_data.refresh_floorplans(self.events[:1], time.monotonic() + 30)

    def test_a_layout_not_named_after_its_file_keeps_both_names(self):
        self.assertEqual(reference_data.floorplan_name("126", "/media/3y5j1ncp/st2bedclassic.svg"), "126_st2bedclassic")
        self.assertEqual(reference_data.floorplan_name("108", "/media/kiwdfaqb/108.svg"), "108")

    @unittest.skipUnless(chromium_available(), "needs Playwright's Chromium")
    def test_a_png_copy_is_drawn(self):
        reference_data.refresh_floorplans(self.events, time.monotonic() + 60)
        png = Path(self.tmp, "floorplans", "108.png").read_bytes()
        self.assertTrue(png.startswith(b"\x89PNG"))
        index = json.loads(Path(self.tmp, "floorplans", "index.json").read_text())
        self.assertEqual(index["108"]["png"], "108.png")

    def test_refresh_never_raises_and_keeps_going(self):
        with mock.patch.object(reference_data, "render_pngs", return_value=""):
            notes = reference_data.refresh(self.events, budget_seconds=40)
        self.assertTrue(notes[0].startswith("floorplans: 2 layouts"), notes)
        self.assertTrue(notes[1].startswith("buildings failed: RuntimeError: OpenStreetMap (Overpass) didn't answer"),
                        notes)
        self.assertFalse(os.path.exists(reference_data.BUILDINGS_FILE))

    def test_buildings_are_fetched_once(self):
        with mock.patch.object(reference_data, "fetch_osm", return_value=fake_osm.answer()) as fetch:
            self.assertIn("7 from OpenStreetMap, 4 look at the Oval", reference_data.refresh_buildings())
            self.assertEqual(reference_data.refresh_buildings(), "buildings: already there")
            self.assertEqual(fetch.call_count, 1)
        with mock.patch.object(reference_data, "fetch_osm", return_value={"elements": []}):
            self.assertIn("kept the old file", reference_data.refresh_buildings(force=True))
        self.assertTrue(os.path.exists(reference_data.BUILDINGS_FILE))


if __name__ == "__main__":
    unittest.main()
