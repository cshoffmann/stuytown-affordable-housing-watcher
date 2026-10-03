"""
Takes a full-page screenshot of the affordable housing listings page, as a
real (headless) browser renders it -- so you can see what a new unit looked
like when it was posted. watch_loop.py calls take() right after the alert
has gone out; the alert never waits on this.

Needs Playwright, because the page is drawn by JavaScript (unlike
check_units.py, which only needs the plain JSON API):
    pip install -r requirements.txt
    playwright install chromium

Try it -- screenshots whatever is live right now into screenshots/:
    python screenshot.py
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PAGE_URL = "https://affordable-housing.stuytown.com/apartments/"
OUTPUT_DIR = "screenshots"
# The page's own request for listings (units.stuytown.com/api/ah-units).
LISTINGS_API = re.compile(r"/api/ah-units")
# Cookie banner (Ketch) and analytics: blocked so the banner can't cover the
# listings and the page settles faster. The units render fine without them.
BLOCKED = re.compile(r"ketchcdn\.com|ketchjs\.com|googletagmanager\.com|google-analytics\.com|doubleclick\.net")


def take(out_path: str, fake_api_payload: dict | None = None) -> str:
    """Screenshot the listings page to out_path. With fake_api_payload, the
    page's request for listings gets that payload instead of the live API's
    answer -- the tests use this to see fake units exactly as the real site
    would show them."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.route(BLOCKED, lambda route: route.abort())
            if fake_api_payload is not None:
                body = json.dumps(fake_api_payload)
                page.route(LISTINGS_API, lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=body,
                    headers={"Access-Control-Allow-Origin": "*"},
                ))
            page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass  # something is still trickling in; the listings have rendered by now
            page.wait_for_timeout(1500)  # let any last client-side rendering settle
            page.screenshot(path=out_path, full_page=True)
        finally:
            browser.close()
    return out_path


def main() -> None:
    filename = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ") + ".png"
    print(f"Saved screenshot to {take(f'{OUTPUT_DIR}/{filename}')}")


if __name__ == "__main__":
    main()
