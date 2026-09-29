"""
Takes a full-page screenshot of the live, rendered affordable housing
listings page and saves it under screenshots/.

This needs a real browser (Playwright) because the page is JavaScript-
rendered -- unlike check_units.py, which only needs the plain JSON API and
never touches a browser. Only invoked by the workflow when check_units.py
has just found a new unit, so the ~30-60 seconds it takes to install a
browser only gets paid on the rare run that actually matters.

check_units.py decides the exact filename (so it can reference it in the
matching events.json entry) and passes it via the SCREENSHOT_FILENAME
env var. Running this script standalone -- e.g. to test it -- falls back
to a fresh timestamp instead.

Local setup (one-time):
    pip install playwright
    playwright install chromium

Local test (no faking needed -- it just screenshots whatever is live now):
    python screenshot.py
"""

import os
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

PAGE_URL = "https://affordable-housing.stuytown.com/apartments/"
OUTPUT_DIR = Path("screenshots")


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    filename = os.environ.get("SCREENSHOT_FILENAME") or (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ") + ".png"
    )
    out_path = OUTPUT_DIR / filename

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(PAGE_URL, wait_until="networkidle", timeout=30000)
        # Give any last client-side rendering a moment to settle after the
        # network goes quiet.
        page.wait_for_timeout(2000)
        page.screenshot(path=str(out_path), full_page=True)
        browser.close()

    print(f"Saved screenshot to {out_path}")


if __name__ == "__main__":
    main()
