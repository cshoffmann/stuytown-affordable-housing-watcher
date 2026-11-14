"""
Automated checks for floorplans.py: saving each layout's floorplan, against a
local fake web server, never the real site.

Run from the repo folder:
    python -m unittest discover -s tests -v
"""

import http.server
import json
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

import floorplans  # noqa: E402

FLOORPLAN_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100" width="200" height="100">'
                 '<rect x="5" y="5" width="190" height="90" fill="none" stroke="#000"/>'
                 '<text x="20" y="40">LIVING ROOM</text><text x="20" y="60"><tspan>18\'2" x 11\'6"</tspan></text>'
                 '<!-- "googleMapAPIKey": "AIzaSyA1234567890123456789012345678901" --></svg>')


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
                            ):
            patcher = mock.patch.object(floorplans, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        unit = {"unitSpk": "P~T~U~06-A", "name": "06-A", "bedrooms": 1, "bathrooms": 1, "sqft": 760,
                "style": {"name": "108"}, "floorplan": {"floorplanSvg": "/media/abc/108.svg"}}
        missing = {**unit, "style": {"name": "999"}, "floorplan": {"floorplanSvg": "/media/zzz/999.svg"}}
        self.events = [{"event": "new", "units": [
            {"unit_id": "a", "apartment": "06-A", "address": "1 Stuyvesant Oval", "data": unit},
            {"unit_id": "b", "apartment": "07-A", "address": "1 Stuyvesant Oval", "data": missing}]}]

    def test_each_layouts_floorplan_is_saved_once_with_its_labels_and_no_keys(self):
        with mock.patch.object(floorplans, "render_pngs", return_value="PNGs: 0 saved"):
            note = floorplans.refresh_floorplans(self.events, time.monotonic() + 30)
        self.assertIn("2 layouts, 1 new", note)
        self.assertIn("couldn't fetch 999", note)
        saved = Path(self.tmp, "floorplans", "108.svg").read_text()
        self.assertIn("LIVING ROOM", saved)
        self.assertNotIn("AIza", saved)
        index = json.loads(Path(self.tmp, "floorplans", "index.json").read_text())
        self.assertEqual(index["108"]["labels"], ["LIVING ROOM", "18'2\" x 11'6\""])
        self.assertEqual(index["108"]["rooms"], {"LIVING ROOM": "18'2\" x 11'6\""})
        self.assertEqual(index["108"]["seen_in"], ["06-A, 1 Stuyvesant Oval"])
        self.assertIn("last_error", index["999"])
        with mock.patch.object(floorplans, "fetch_floorplan", side_effect=AssertionError("fetched twice")), \
                mock.patch.object(floorplans, "render_pngs", return_value=""):
            floorplans.refresh_floorplans(self.events[:1], time.monotonic() + 30)

    def test_room_sizes_are_put_back_together(self):
        labels = ["Kitchen", "Living Room", "12’0", "”", "x 18’7”", "Dining Area", "9’6", "x 9’4”", "Closet",
                  "Master Bedroom", "15’0", "x 12’0", "Closet"]
        self.assertEqual(floorplans.rooms(labels), {
            "Kitchen": "", "Living Room": "12’0” x 18’7”", "Dining Area": "9’6” x 9’4”", "Closet": "",
            "Master Bedroom": "15’0” x 12’0”", "Closet 2": ""})

    def test_a_layout_not_named_after_its_file_keeps_both_names(self):
        self.assertEqual(floorplans.floorplan_name("126", "/media/3y5j1ncp/st2bedclassic.svg"), "126_st2bedclassic")
        self.assertEqual(floorplans.floorplan_name("108", "/media/kiwdfaqb/108.svg"), "108")

    @unittest.skipUnless(chromium_available(), "needs Playwright's Chromium")
    def test_a_png_copy_is_drawn(self):
        floorplans.refresh_floorplans(self.events, time.monotonic() + 60)
        png = Path(self.tmp, "floorplans", "108.png").read_bytes()
        self.assertTrue(png.startswith(b"\x89PNG"))
        index = json.loads(Path(self.tmp, "floorplans", "index.json").read_text())
        self.assertEqual(index["108"]["png"], "108.png")

    def test_refresh_never_raises(self):
        with mock.patch.object(floorplans, "refresh_floorplans", side_effect=OSError("disk full")):
            self.assertEqual(floorplans.refresh(self.events), "floorplans failed: OSError: disk full")


if __name__ == "__main__":
    unittest.main()
