"""
Automated checks for the summary page, the "needs a look" alert, and which
recordings keep their pages -- no network, no browser.

Run from the repo folder:
    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

import apply_recorder  # noqa: E402
import dream_units  # noqa: E402
import fake_osm  # noqa: E402
import reference_data  # noqa: E402
import run_stats  # noqa: E402
import summary  # noqa: E402

FAKE_UNITS = json.loads((REPO_ROOT / "tests" / "fixtures" / "fake_units.json").read_text(encoding="utf-8"))
UNIT = FAKE_UNITS["5A"]


def new_event(stamp, apartment, rent, spk):
    return {"event": "new", "detected_at_utc": stamp, "units": [
        {"unit_id": spk, "apartment": apartment, "address": "287 Avenue C", "bedrooms": 1, "price": rent,
         "income_requirement": 60000,
         "data": {**UNIT, "unitSpk": spk, "name": apartment, "unitFloor": apartment[:2], "bedrooms": 1, "price": rent,
                  "style": {"id": 1, "name": "108"}}}]}


def removed_event(stamp, spk, first, listed_for):
    return {"event": "removed", "detected_at_utc": stamp, "units": [
        {"unit_id": spk, "apartment": "x", "first_seen_utc": first, "listed_for_seconds": listed_for}]}


EVENTS = [
    new_event("2026-10-12T11:15:00Z", "08-B", 2787.04, "P~A"),
    removed_event("2026-10-12T11:16:42Z", "P~A", "2026-10-12T11:15:00Z", 40.0),
    new_event("2026-10-12T11:30:00Z", "06-B", 3067.19, "P~B"),
    removed_event("2026-10-12T11:32:32Z", "P~B", "2026-10-12T11:30:00Z", 90.0),
]
APPLICATIONS = {"P~A": {"attempts": [{"at_utc": "2026-10-12T11:15:31Z", "status": "submitted",
                                      "timeline": {"form_via": "direct (API)"}}]}}


def morning(**overrides):
    run = {
        "run": {"started_utc": "2026-10-12T10:59:01.661Z"},
        "listings_api": {"requests": 5400, "statuses": {"200": 5400}, "failures": {},
                         "response_ms": {"median": 45.0, "p99": 257.9}},
        "units": {"P~A": {"apartment": "08-B", "address": "287 Avenue C", "first_seen_utc": "2026-10-12T11:15:00.044Z",
                          "qualifies_for_auto_apply": True,
                          "posted_vs_quarter_hour_their_clock_s": [-0.21, 0.04],
                          "auto_apply": {"status": "submitted", "detail": "accepted", "via": "direct (API)",
                                         "submit_seconds_after_first_seen": 0.07,
                                         "site_received_seconds_after_first_seen": 0.31}},
                  "P~B": {"apartment": "06-B", "address": "287 Avenue C", "first_seen_utc": "2026-10-12T11:30:00.090Z",
                          "qualifies_for_auto_apply": False}},
        "applier": {"browser_ready_seconds": [1.1, 1.1], "recordings": [], "profile_warnings": [],
                    "direct_submit": {"ready": True, "why_not": None}},
    }
    run.update(overrides)
    return run


class SummaryPageTests(unittest.TestCase):
    def setUp(self):
        # Not the real reference data: the page must look the same whatever's been fetched.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        for target, name, value in ((reference_data, "BUILDINGS_FILE", f"{tmp.name}/buildings.json"),
                                    (reference_data, "FLOORPLANS_DIR", f"{tmp.name}/floorplans"),
                                    (dream_units, "OVERRIDES_FILE", f"{tmp.name}/line_overrides.json")):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_page_has_every_morning_unit_and_pattern(self):
        page = summary.render(EVENTS, [morning()], APPLICATIONS, max_rent=3000,
                              now=datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc))
        self.assertIn("Auto-apply rent limits: **any <= $3,000**", page)
        self.assertIn("Direct submit: **ready**.", page)
        self.assertIn("Nothing: the latest morning went as it should.", page)
        self.assertIn("| Mon Oct 12 | 7:15:00, 7:30:00 | 1 | 1 submitted | 5,400 | 45 / 258 ms |", page)
        self.assertIn("| Oct 12 7:15:00 AM | 08-B, 287 Avenue C | interior | 8 | 1 | 766 | $2,787 | $3.64 | $60,000 | "
                      "Classic | 108 | Marble bath |  |  | 40.0 s | yes | submitted, sent 0.07 s after it appeared, direct |",
                      page)
        self.assertIn("| Oct 12 7:30:00 AM | 06-B, 287 Avenue C | interior | 6 | 1 | 766 | $3,067 | $4.00 | $60,000 | "
                      "Classic | 108 | Marble bath |  |  | 90.0 s | no |  |", page)
        self.assertIn("**Posting times** (ET, all days): 7:15 ×1, 7:30 ×1", page)
        self.assertIn("-0.21 to +0.04", page)
        self.assertIn("1 sent (1 direct); median 0.1 s from the unit appearing to sending, 0.3 s to the site's", page)
        self.assertIn("| 108 | 1 | 766 | 2 | $2,787-$3,067 | not downloaded yet |", page)
        self.assertIn("| ISMB | Marble bath | 2 |", page)
        self.assertIn("[units.geojson](units.geojson)", page)

    def test_per_bedroom_limits_decide_what_qualified(self):
        events = [new_event("2026-10-12T11:15:00Z", "08-B", 2600, "P~A")]
        events[0]["units"][0]["bedrooms"] = 2
        page = summary.render(events, [], {}, rules={1: 2200.0, 2: 2700.0},
                              now=datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc))
        self.assertIn("Auto-apply rent limits: **1 bed <= $2,200, 2 bed <= $2,700**", page)
        self.assertIn("| Mon Oct 12 | 7:15:00 | 1 |", page)

    def test_dream_units_and_floorplans_are_on_the_page(self):
        os.makedirs(f"{self.tmp}/floorplans")
        Path(f"{self.tmp}/floorplans/108.png").write_bytes(b"png")
        Path(reference_data.BUILDINGS_FILE).write_text(json.dumps(reference_data.analyze(fake_osm.answer())))
        dream = fake_osm.unit_at("1", "Stuyvesant Oval", "06-A", price=2150, codes=("VBOV",))
        events = [{"event": "new", "detected_at_utc": "2026-10-12T11:15:00Z", "units": [
            {"unit_id": dream["unitSpk"], "apartment": "06-A", "address": "1 Stuyvesant Oval", "bedrooms": 1,
             "price": 2150, "income_requirement": 70000, "data": dream}]}]
        page = summary.render(events, [], {}, rules={1: 2200.0}, now=datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc))
        self.assertIn("| Oct 12 7:15 AM | 06-A, 1 Stuyvesant Oval | $2,150 | 1 | 6 | SSW | dream | Oval views, faces SSW", page)
        self.assertIn("| Oval | 6 | 1 | 760 | $2,150 |", page)
        self.assertIn("| [108](floorplans/108.png) | Oval views | SSW | **dream** |", page)
        self.assertIn('<img src="floorplans/108.png" width="140" alt="layout 108">', page)
        self.assertIn("**Buildings whose Oval windows get sun** (OpenStreetMap; gold on the map): "
                      "1 Stuyvesant Oval, 5 Stuyvesant Oval, 8 Stuyvesant Oval", page)
        self.assertIn("| 1 Stuyvesant Oval | Oval | windows face SSW | 5.2 min | 1 | $2,150 |  | 1 |", page)

    def test_it_is_built_from_the_real_data_folder_without_errors(self):
        events, stats, applications = summary.load_all()
        self.assertIn("## Patterns", summary.render(events, stats, applications, max_rent=3000))


class NeedsALookTests(unittest.TestCase):
    def test_a_good_morning_needs_nothing(self):
        self.assertEqual(summary.issues(morning()), [])

    def test_what_counts_as_something_broken(self):
        run = morning()
        run["units"]["P~A"]["auto_apply"] = {"status": "incomplete", "detail": "no field found for zip"}
        run["units"]["P~C"] = {"apartment": "1A", "address": "x", "qualifies_for_auto_apply": True}
        run["applier"]["recordings"] = [{"apartment": "06-B", "status": "failed", "detail": "no APPLY NOW"}]
        run["applier"]["direct_submit"] = {"ready": False, "why_not": "the site's code changed since (v1)"}
        run["applier"]["profile_warnings"] = ["first_name is longer than the form allows (20 characters)"]
        run["listings_api"]["statuses"] = {"200": 5390, "429": 10}
        found = summary.issues(run)
        self.assertEqual(len(found), 6, found)
        self.assertIn("Apt 08-B, 287 Avenue C: auto-apply incomplete (no field found for zip)", found)
        self.assertIn("Apt 1A, x qualified, but auto-apply didn't try it", found)
        self.assertTrue(any("asked us to slow down 10 times" in line for line in found))

    def test_the_sites_own_refusals_are_not_breakage(self):
        run = morning()
        run["units"]["P~A"]["auto_apply"] = {
            "status": "rejected", "detail": "the site says you already have an open application for this apartment"}
        self.assertEqual(summary.issues(run), [])


class RecordingPagesTests(unittest.TestCase):
    PAGE = "<html><script src='/sb/aHUnit-js.js.v555'></script><input name='x' value='4B'></html>"

    def write(self, folder, status, notes=None):
        recording = apply_recorder.Recording(folder, apply_recorder.Redactor(None),
                                             {"started_utc": "t", "outcome": {"status": status}, "notes": notes or {}},
                                             [], {"1_unit_page": self.PAGE}, {"2_form_empty": b"\xff\xd8"})
        recording.write()
        return json.loads((Path(folder) / "report.json").read_text())

    def test_pages_are_kept_once_per_site_version_and_when_something_goes_wrong(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(apply_recorder, "SITE_VERSIONS_FILE", f"{tmp}/site_versions.json"):
            first = self.write(f"{tmp}/a", "recorded")
            routine = self.write(f"{tmp}/b", "submitted")
            broken = self.write(f"{tmp}/c", "failed")
            self.assertEqual(first["pages_kept"], "kept: the first time the site's code v555 was seen")
            self.assertTrue(routine["pages_kept"].startswith("not kept"))
            self.assertEqual(sorted(p.name for p in Path(f"{tmp}/b").iterdir()), ["network.json", "report.json"])
            self.assertEqual(broken["pages_kept"], "kept: the outcome was failed")
            self.assertIn("1_unit_page.html", broken["files"])
            self.assertIn("555", json.loads(Path(f"{tmp}/site_versions.json").read_text()))


class ClockTests(unittest.TestCase):
    def test_boundaries_between_their_seconds_pin_their_clock_down(self):
        stats = run_stats.RunStats()
        ours = 1_760_000_000.0  # their clock is 0.3 s ahead of ours
        responses = []
        for i in range(40):  # every 0.25 s, 50 ms round trips
            sent = ours + i * 0.25
            theirs = int(sent + 0.025 + 0.3)
            date = format_datetime(datetime.fromtimestamp(theirs, timezone.utc), usegmt=True)
            responses.append({"ms": 50, "status": 200, "bytes": 2, "body": b"{}", "sent_epoch": sent,
                              "received_epoch": sent + 0.05, "headers": {"date": date}})
        stats.observe_responses(responses)
        offset = stats.clock_offset()
        self.assertTrue(offset["consistent"])
        self.assertLessEqual(offset["at_least"], 0.3)
        self.assertGreaterEqual(offset["at_most"], 0.3)
        # As narrow as the checks' spacing plus a round trip (0.25 + 0.05 s): far better than the header's 1 s.
        self.assertLessEqual(offset["at_most"] - offset["at_least"], 0.301)


if __name__ == "__main__":
    unittest.main()
