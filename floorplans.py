"""
Floorplans: the plan of every layout seen in a listing, saved under
data/floorplans/ -- the SVG the site shows, a PNG copy drawn by Chromium,
and index.json (layout, size, where it was seen, room sizes read off the
drawing). Runs once a morning after the checks (never while applying);
only layouts not saved yet are fetched. Never raises.

    python floorplans.py    # fetch any layout not saved yet
"""

import base64
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import apply_recorder
import check_units

FLOORPLANS_DIR = "data/floorplans"
FLOORPLAN_HOSTS = [
    "https://affordable-housing.stuytown.com",  # where the site's own page fetches them
    "https://uxp-pd-cms-gmfqdagrdcdrd0gk.a02.azurefd.net",  # its media CDN
    "https://www.stuytown.com",
]
FLOORPLAN_PNG_WIDTH = 1000


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


def refresh(events: list | None = None, budget_seconds: float = 60) -> str:
    """Fetch the floorplans not saved yet, within the time budget. Never raises."""
    if events is None:
        events = read_json(check_units.EVENTS_FILE, []) or []
    try:
        return refresh_floorplans(events, time.monotonic() + budget_seconds)
    except Exception as e:
        return f"floorplans failed: {type(e).__name__}: {e}"


if __name__ == "__main__":
    print(refresh(budget_seconds=240))
    sys.exit(0)
