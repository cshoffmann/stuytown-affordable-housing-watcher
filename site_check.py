"""
Is the watcher still looking where StuyTown's own page looks?

Opens the affordable-housing listings page in Chromium, records every API
call the page makes and what each one returned, and compares that with the
watcher's own request. Prints a JSON report and exits 1 if they disagree:
the page calls a different listings address, or it sees units we don't.

    python site_check.py
"""

import json
import re
import sys
from datetime import datetime, timezone

import apply_recorder
import check_units

AVAILABILITY_TEXT = re.compile(r"no (apartments|units|results)|not available|waitlist|wait list|lottery|"
                               r"closed|paused|coming soon|check back|currently|available", re.I)


def our_api() -> dict:
    client = check_units.ListingsClient()
    try:
        units = client.fetch_all()
        last = client.responses[-1]
        return {"url": check_units.BASE_URL, "units": len(units), "status": last["status"], "bytes": last["bytes"],
                "body_start": last["body"][:200].decode("utf-8", "replace")}
    except Exception as e:
        return {"url": check_units.BASE_URL, "error": f"{type(e).__name__}: {e}"}
    finally:
        client.close()


def page_view(timeout_s: float = 45) -> dict:
    from playwright.sync_api import sync_playwright

    calls = []

    def on_response(response):
        request = response.request
        if request.resource_type not in ("xhr", "fetch"):
            return
        entry = {"method": request.method, "url": apply_recorder.scrub_keys(request.url), "status": response.status}
        try:
            body = response.body()
            entry["bytes"] = len(body)
            data = json.loads(body)
            if isinstance(data, dict):
                for key in ("unitModels", "units", "items", "results", "data"):
                    if isinstance(data.get(key), list):
                        entry[f"{key}_count"] = len(data[key])
                if "totalCount" in data:
                    entry["totalCount"] = data["totalCount"]
            elif isinstance(data, list):
                entry["list_count"] = len(data)
        except Exception:
            pass
        calls.append(entry)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("response", on_response)
        page.goto(check_units.LISTINGS_URL, wait_until="networkidle", timeout=timeout_s * 1000)
        page.wait_for_timeout(5000)  # late calls after the first render
        html = page.content()
        text = page.inner_text("body")
        title = page.title()
        browser.close()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return {
        "title": title,
        "site_version": next(iter(apply_recorder.SITE_VERSION.findall(html)), None),
        "api_calls": calls,
        "availability_text": [line[:200] for line in lines if AVAILABILITY_TEXT.search(line)][:15],
        "apartment_mentions": len(re.findall(r"\bApt\.?\s|\bApartment\s+\w+-\w+", text)),
    }


def verdict(ours: dict, page: dict) -> tuple:
    """(ok, one-line explanation)."""
    if "error" in ours:
        return False, f"our request failed: {ours['error']}"
    listing_calls = [c for c in page["api_calls"] if "unitModels_count" in c]
    if not listing_calls:
        return False, "the page didn't call a listings API we recognise: the site may have changed"
    base = check_units.BASE_URL.split("//", 1)[1]
    other = [c for c in listing_calls if base not in c["url"]]
    if other:
        return False, f"the page now gets its listings from {other[0]['url']}"
    seen = max(c["unitModels_count"] for c in listing_calls)
    if seen != ours["units"]:
        return False, f"the page sees {seen} units, the watcher sees {ours['units']}"
    return True, f"OK: the page uses the same API as the watcher and both see {seen} units"


def main() -> int:
    report = {"checked_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "our_api": our_api()}
    try:
        report["page"] = page_view()
        ok, report["verdict"] = verdict(report["our_api"], report["page"])
    except Exception as e:
        ok, report["verdict"] = False, f"couldn't load the page: {type(e).__name__}: {str(e)[:300]}"
    print(json.dumps(report, indent=1))
    print(report["verdict"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
