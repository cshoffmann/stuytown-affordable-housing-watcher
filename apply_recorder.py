"""
Records everything about one trip through StuyTown's application form that
would help fix auto_apply.py if it ever fails: each step with its timing and
URL, every form field's label and HTML attributes, the buttons, the page
HTML at each stage, the browser's network traffic (including the request
SUBMIT sends and the site's answer) and its console errors.

It's saved to data/apply_runs/<time>_<apartment>_<kind>/ and committed, so the
next round of fixes can start from what the real site did. This repo is
public, so every value from your applicant profile is replaced with its key
name -- "Jane" becomes <first_name>, "+1 212 555 0123" becomes <cell_phone>
-- before anything is written. Screenshots of a filled-in form can't be
redacted, so those only ever go to your phone; the ones saved here are of
pages that don't contain your details (the unit page, the empty form).

    report.json    steps + timings, the form's fields and buttons, the outcome,
                   page navigations, live (WebSocket) connections, cookies,
                   and an "analysis" of the trip: the request SUBMIT sent and
                   its answer, which sites the scripts came from, the CDN,
                   and any sign of anti-bot or CAPTCHA services
    network.json   requests and responses (bodies for page/API calls)
    *.html         the page at each stage
    *.jpg          screenshots without your details

Cookies are recorded by name and settings only, never their values.
"""

import json
import re
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = "data/apply_runs"
MAX_HTML_BYTES = 1_500_000
MAX_BODY_CHARS = 50_000
MAX_REQUESTS = 400
MAX_SOCKET_FRAMES_KEPT = 3  # per WebSocket: the first few messages, shortened
# Headers that carry login state; never worth keeping.
DROPPED_HEADERS = {"cookie", "set-cookie", "authorization", "proxy-authorization"}
BODY_TYPES = ("document", "xhr", "fetch")  # whose responses are worth reading
PHONE_KEYS = ("cell_phone", "work_phone")


class Redactor:
    """Replaces every value of the applicant profile in a piece of text with
    <its key>, in the forms a page or a request might show it: any letter
    case, phone numbers with or without +1 and separators, the income as
    95000 / 95,000.00 / 9500000 (cents), and URL-encoded."""

    def __init__(self, profile: dict | None):
        patterns = []
        for key, value in _leaves(profile or {}):
            patterns += [(pattern, f"<{key}>") for pattern in _patterns_for(key, value)]
        # Longest first, so "jane.doe@example.com" goes before "Jane".
        patterns.sort(key=lambda item: -len(item[0]))
        self.patterns = [(re.compile(p, re.I), name) for p, name in patterns]

    def __call__(self, text):
        if not isinstance(text, str) or not text:
            return text
        for pattern, name in self.patterns:
            text = pattern.sub(name, text)
        return text

    def deep(self, value):
        """Redact every string inside a JSON-like structure."""
        if isinstance(value, dict):
            return {self(k) if isinstance(k, str) else k: self.deep(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.deep(v) for v in value]
        return self(value) if isinstance(value, str) else value


def _leaves(value, key=None):
    if isinstance(value, dict):
        for inner_key, inner in value.items():
            if not str(inner_key).startswith("_"):
                yield from _leaves(inner, inner_key if key is None else key)
    elif isinstance(value, list):
        for inner in value:
            yield from _leaves(inner, key)
    elif value is not None and not isinstance(value, bool):
        yield str(key), value


def _patterns_for(key: str, value) -> list:
    text = str(value).strip()
    digits = re.sub(r"\D", "", text)
    if key in PHONE_KEYS or (len(digits) in (10, 11) and re.fullmatch(r"[\d\s()+.-]+", text)):
        national = digits[-10:]
        if len(national) == 10:
            return [r"(?<!\d)(?:\+?\s*1[\s().-]*)?" + r"[\s().-]*".join(national) + r"(?!\d)"]
    if key == "annual_income":
        try:
            amount = float(text.replace("$", "").replace(",", ""))
        except ValueError:
            amount = None
        if amount:
            spellings = {f"{amount:,.2f}", f"{amount:,.0f}", f"{amount:.2f}", f"{amount:.0f}", f"{amount * 100:.0f}"}
            return [r"(?<![\d,.])" + re.escape(s) + r"(?![\d,]|\.\d)" for s in spellings]
    if len(text) < 3:
        return []  # "NY", "1", "4B": not identifying, and would mangle everything else
    start = r"(?<![0-9a-z])" if text[0].isalnum() else ""
    end = r"(?![0-9a-z])" if text[-1].isalnum() else ""
    return [start + re.escape(text) + end]


class RunRecorder:
    """One trip through the form. Collects in memory while the browser works
    (nothing slows down the application). Afterwards, collect() gathers the
    network data from the browser -- quick, and done by the applier, because
    only its thread can talk to its browser -- and the Recording it returns is
    redacted and written by the background logger, never by the applier."""

    def __init__(self, folder: str | Path | None, redact: Redactor):
        self.folder = Path(folder) if folder else None
        self.redact = redact
        self.started = time.monotonic()
        self.started_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.steps, self.console, self.notes = [], [], {}
        self.pages, self.images = {}, {}
        self._requests, self._responses = [], {}
        self.navigations, self.sockets = [], []

    # ---------------------------------------------------------------- collect

    def watch(self, page) -> None:
        """Start recording one page's network traffic and console -- and any
        pop-up it opens. Per page, not per browser: the applier's browser is
        reused all morning, and each trip gets its own page."""
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_failed)
        page.on("console", lambda msg: self._on_console(msg.type, msg.text))
        page.on("pageerror", lambda error: self._on_console("pageerror", str(error)))
        page.on("popup", self.watch)
        page.on("framenavigated", lambda frame: self._on_navigated(page, frame))
        page.on("websocket", self._on_websocket)

    def _on_navigated(self, page, frame) -> None:
        if frame == page.main_frame and len(self.navigations) < 50:
            self.navigations.append({"t": self._now(), "url": frame.url})

    def _on_websocket(self, socket) -> None:
        """A live connection the site holds open (e.g. for instant updates)."""
        if len(self.sockets) >= 20:
            return
        entry = {"t": self._now(), "url": socket.url, "sent": 0, "received": 0, "first_messages": []}
        self.sockets.append(entry)

        def frame(direction, payload):
            entry[direction] += 1
            if len(entry["first_messages"]) < MAX_SOCKET_FRAMES_KEPT:
                text = payload if isinstance(payload, str) else f"<{len(payload)} bytes>"
                entry["first_messages"].append({"t": self._now(), "direction": direction, "text": text[:300]})

        socket.on("framesent", lambda payload: frame("sent", payload))
        socket.on("framereceived", lambda payload: frame("received", payload))
        socket.on("close", lambda *_: entry.update(closed_t=self._now()))

    def _on_console(self, kind: str, text: str) -> None:
        if kind in ("error", "warning", "pageerror") and len(self.console) < 100:
            self.console.append({"t": self._now(), "type": kind, "text": text[:500]})

    def _on_request(self, request) -> None:
        if len(self._requests) < MAX_REQUESTS:
            self._requests.append({"t": self._now(), "request": request})

    def _on_response(self, response) -> None:
        self._responses[id(response.request)] = (self._now(), response)

    def _on_failed(self, request) -> None:
        for entry in self._requests:
            if entry["request"] is request:
                entry["failure"] = request.failure

    def step(self, name: str, **details) -> None:
        self.steps.append({"t": self._now(), "step": name, **details})

    def note(self, key: str, value) -> None:
        self.notes[key] = value

    def snapshot(self, name: str, page_or_frame) -> None:
        """The page's HTML right now (redacted when written)."""
        try:
            self.pages[name] = page_or_frame.content()
        except Exception as e:
            self.pages[name] = f"<!-- couldn't read the page: {e} -->"

    def image(self, name: str, jpeg: bytes | None) -> None:
        """A screenshot WITHOUT your details (an unfilled page) -- committed."""
        if jpeg:
            self.images[name] = jpeg

    def _now(self) -> float:
        return round(time.monotonic() - self.started, 3)

    # ---------------------------------------------------------------- collect

    def collect(self, outcome: dict, page=None) -> "Recording":
        """Everything gathered so far, plus the network data read from the
        browser (request bodies, response bodies) and, given the page, its
        cookies' names and settings. Call while the page is still open.
        Unredacted: hand it to the background logger, which redacts it in
        Recording.write(). Never raises."""
        try:
            network = self._network()
        except Exception as e:
            network = [{"error": f"couldn't read the network data: {e}"}]
        return Recording(self.folder, self.redact, {
            "started_utc": self.started_utc,
            "seconds": self._now(),
            "outcome": outcome,
            "steps": self.steps,
            "notes": self.notes,
            "console": self.console,
            "navigations": self.navigations,
            "websockets": self.sockets,
            "cookies": _cookie_settings(page),
        }, network, dict(self.pages), dict(self.images))

    def finish(self, outcome: dict) -> Path | None:
        """collect() and write() in one go, for the command-line tools."""
        return self.collect(outcome).write()

    def _network(self) -> list:
        entries = []
        for entry in self._requests:
            request = entry["request"]
            item = {
                "t": entry["t"],
                "method": request.method,
                "type": request.resource_type,
                "url": request.url,
            }
            if entry.get("failure"):
                item["failed"] = entry["failure"]  # incl. what auto-apply itself blocks (images etc.)
            if request.method != "GET" or request.resource_type in BODY_TYPES:
                item["request_headers"] = dict(request.headers)
                body = _safe(lambda: request.post_data)
                if body:
                    item["request_body"] = body
            answered = self._responses.get(id(request))
            if answered:
                t, response = answered
                item.update({"answered_t": t, "status": response.status})
                if request.method != "GET" or request.resource_type in BODY_TYPES:
                    item["response_headers"] = dict(response.headers)
                    body = _safe(lambda: response.text())
                    if body is not None:
                        item["response_body"] = body[:MAX_BODY_CHARS * 4]
            entries.append(item)
        return entries


class Recording:
    """A collected trip through the form, ready to be redacted and written --
    by the background logger."""

    def __init__(self, folder, redact: Redactor, report: dict, network: list, pages: dict, images: dict):
        self.folder = Path(folder) if folder else None
        self.redact = redact
        self.report, self.network, self.pages, self.images = report, network, pages, images

    def write(self) -> Path | None:
        """Redact and write everything. Never raises."""
        if self.folder is None:
            return None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            report = {**self.report, "files": sorted([f"{n}.html" for n in self.pages]
                                                     + [f"{n}.jpg" for n in self.images] + ["network.json"])}
            report["analysis"] = self._analysis()
            self._write("report.json", json.dumps(self.redact.deep(report), indent=2))
            self._write("network.json", json.dumps([self._redact_entry(e) for e in self.network], indent=2))
            for name, content in self.pages.items():
                self._write(f"{name}.html", self.redact(content)[:MAX_HTML_BYTES])
            for name, jpeg in self.images.items():
                (self.folder / f"{name}.jpg").write_bytes(jpeg)
            return self.folder
        except Exception as e:
            print(f"   WARNING: couldn't save the form recording: {e}")
            return None

    def _write(self, name: str, text: str) -> None:
        (self.folder / name).write_text(text, encoding="utf-8")

    def _analysis(self) -> dict:
        """What Phase 2 most needs from a trip, worked out from the raw data
        (and redacted like the rest)."""
        try:
            steps = self.report.get("steps") or []
            pressed = next((s["t"] for s in steps if s.get("step") == "pressing SUBMIT"), None)
            requests = [e for e in self.network if "method" in e]
            after = [e for e in requests if pressed is not None and e["t"] >= pressed]
            sent = next((e for e in after if e["method"] != "GET"), None)
            scripts = Counter(urllib.parse.urlsplit(e["url"]).netloc for e in requests if e.get("type") == "script")
            return {
                "submit_request": self._summarize_request(sent) if sent else None,
                "requests_after_submit": [f"{e['method']} {e.get('status', '-')} {self._redact_url(e['url'])[:160]}"
                                          for e in after[:25]],
                "script_hosts": dict(scripts.most_common(20)),
                "anti_bot": self._seen(ANTI_BOT, requests),
                "cdn": self._seen(CDN, requests),
            }
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def _summarize_request(self, entry: dict) -> dict:
        """The request SUBMIT sent: everything a direct (browser-less) submit
        would have to reproduce -- headers by name, the body's field names,
        which of them look like tokens -- and the site's answer."""
        headers = {k.lower(): v for k, v in (entry.get("request_headers") or {}).items()}
        content_type = headers.get("content-type", "")
        keys = _body_keys(entry.get("request_body") or "", content_type)
        return {
            "method": entry["method"],
            "url": self._redact_url(entry["url"]),
            "type": entry.get("type"),
            "seconds_after_pressing": round(entry["t"] - next(
                s["t"] for s in self.report["steps"] if s.get("step") == "pressing SUBMIT"), 3),
            "content_type": content_type,
            "header_names": sorted(headers),
            "body_keys": [self.redact(k) for k in keys],
            "token_like": [self.redact(k) for k in keys + sorted(headers) if TOKEN_LIKE.search(k)],
            "status": entry.get("status"),
            "answered_after_seconds": round(entry["answered_t"] - entry["t"], 3) if "answered_t" in entry else None,
            "response_content_type": {k.lower(): v for k, v in (entry.get("response_headers") or {}).items()}
            .get("content-type"),
            "response_excerpt": self.redact(entry.get("response_body") or "")[:600] or None,
            "failed": entry.get("failed"),
        }

    def _seen(self, vendors: dict, requests: list) -> dict:
        """Which of these services the site uses, and where each was seen
        (request addresses, page HTML, cookie names, response headers)."""
        sources = {
            "requests": "\n".join(e["url"] for e in requests),
            "pages": "\n".join(self.pages.values()),
            "cookies": "\n".join(c.get("name", "") for c in self.report.get("cookies") or []),
            "headers": "\n".join(f"{k}: {v}" for e in requests for k, v in (e.get("response_headers") or {}).items()),
        }
        found = {}
        for vendor, pattern in vendors.items():
            where = [name for name, text in sources.items() if pattern.search(text)]
            if where:
                found[vendor] = where
        return found

    def _redact_entry(self, entry: dict) -> dict:
        item = dict(entry)
        if "url" in item:
            item["url"] = self._redact_url(item["url"])
        for key in ("request_headers", "response_headers"):
            if key in item:
                item[key] = self._headers(item[key])
        if "request_body" in item:
            content_type = (entry.get("request_headers") or {}).get("content-type", "")
            item["request_body"] = self._redact_body(item["request_body"], content_type)
        if "response_body" in item:
            item["response_body"] = self.redact(item["response_body"])[:MAX_BODY_CHARS]
        return item

    def _headers(self, headers: dict) -> dict:
        return {k: self.redact(v) for k, v in headers.items() if k.lower() not in DROPPED_HEADERS}

    def _redact_url(self, url: str) -> str:
        return self.redact(urllib.parse.unquote_plus(url))

    def _redact_body(self, body: str, content_type: str) -> str:
        if "x-www-form-urlencoded" in content_type:
            body = urllib.parse.unquote_plus(body)
        return self.redact(body)[:MAX_BODY_CHARS]


# Anti-bot, CAPTCHA and waiting-room services, by what they leave behind.
ANTI_BOT = {
    "recaptcha": re.compile(r"google\.com/recaptcha|recaptcha\.net|gstatic\.com/recaptcha|grecaptcha", re.I),
    "hcaptcha": re.compile(r"hcaptcha\.com", re.I),
    "cloudflare_turnstile": re.compile(r"challenges\.cloudflare\.com/turnstile|cf-turnstile", re.I),
    "cloudflare_bot_check": re.compile(r"/cdn-cgi/challenge-platform|cf_clearance|cf-chl|__cf_bm", re.I),
    "perimeterx_human": re.compile(r"perimeterx|px-cdn\.net|px-cloud\.net|pxchk\.net|\b_px[23vh]?\b", re.I),
    "datadome": re.compile(r"datadome|captcha-delivery\.com", re.I),
    "akamai_bot_manager": re.compile(r"\b_abck\b|\bbm_sz\b|\bak_bmsc\b", re.I),
    "kasada": re.compile(r"kasada|x-kpsdk|kpsdk", re.I),
    "imperva_incapsula": re.compile(r"incapsula|imperva|incap_ses|visid_incap", re.I),
    "queue_it": re.compile(r"queue-it\.net|queueit", re.I),
    "f5_shape": re.compile(r"shapesecurity|\bTS01[0-9a-f]{6}\b", re.I),
}
# Who serves the site (decides what caching and blocking to expect).
CDN = {
    "cloudflare": re.compile(r"^cf-ray:|^server: cloudflare", re.I | re.M),
    "akamai": re.compile(r"akamai", re.I),
    "fastly": re.compile(r"fastly|^x-served-by: cache-", re.I | re.M),
    "cloudfront": re.compile(r"cloudfront|^x-amz-cf-", re.I | re.M),
    "azure_front_door": re.compile(r"^x-azure-ref:|azurefd\.net", re.I | re.M),
}
# Field and header names that usually carry a per-visit token.
TOKEN_LIKE = re.compile(r"token|csrf|xsrf|nonce|captcha|signature|\bsig\b|verification|antiforgery|session", re.I)


def _body_keys(body: str, content_type: str) -> list:
    """The field names in a request body (JSON, nested as a.b, or a form)."""
    keys = []
    if "json" in content_type or body.lstrip().startswith(("{", "[")):
        try:
            def walk(value, prefix=""):
                if isinstance(value, dict):
                    for k, v in value.items():
                        keys.append(prefix + str(k))
                        walk(v, prefix + str(k) + ".")
                elif isinstance(value, list) and value:
                    walk(value[0], prefix)
            walk(json.loads(body))
            return keys[:80]
        except ValueError:
            pass
    if "multipart" in content_type:
        return re.findall(r'name="([^"]+)"', body)[:80]
    return [urllib.parse.unquote_plus(part.split("=", 1)[0]) for part in body.split("&") if part][:80]


def _cookie_settings(page) -> list:
    """The browser's cookies -- name, site, and settings. Never their values."""
    if page is None:
        return []
    def days_left(cookie):
        expires = cookie.get("expires", -1)
        return round((expires - time.time()) / 86400, 1) if expires > 0 else None  # None: ends with the visit

    try:
        return [{"name": c.get("name"), "domain": c.get("domain"), "path": c.get("path"),
                 "http_only": c.get("httpOnly"), "secure": c.get("secure"), "same_site": c.get("sameSite"),
                 "expires_in_days": days_left(c), "value_length": len(c.get("value") or "")}
                for c in page.context.cookies()][:60]
    except Exception as e:
        return [{"error": f"couldn't read the cookies: {type(e).__name__}"}]


class SiteWatch:
    """What the listings page does on its own while an applier browser sits
    on it all morning: the requests it makes after loading (does the site
    poll the listings API itself, and how often? is there a cheaper
    "anything new?" endpoint?) and the live connections it holds open.

    Read from the page's own resource timings, by the worker that owns the
    page, before each reload and at the end -- so nothing is listened to in
    between and the browser isn't kept any busier."""

    LATE_AFTER_SECONDS = 10  # requests made later than this after a load are the page's own doing

    def __init__(self):
        self.loads = 0
        self.watched_seconds = 0.0
        self.late = Counter()
        self.sockets = Counter()
        self.errors = Counter()
        self._loaded_at = None

    def attach(self, page) -> None:
        page.on("websocket", lambda socket: self.sockets.update([socket.url.split("?")[0][:160]]))

    def loaded(self, page) -> None:
        self.loads += 1
        self._loaded_at = time.monotonic()
        try:
            page.evaluate("() => performance.setResourceTimingBufferSize(5000)")
        except Exception as e:
            self.errors[type(e).__name__] += 1

    def harvest(self, page) -> None:
        if self._loaded_at is None:
            return
        try:
            entries = page.evaluate(f"""() => performance.getEntriesByType('resource')
                .filter(r => r.startTime > {self.LATE_AFTER_SECONDS * 1000})
                .map(r => [r.initiatorType, r.name.split('?')[0].slice(0, 200)])""")
            self.late.update(f"{kind} {url}" for kind, url in entries)
            self.watched_seconds += max(0.0, time.monotonic() - self._loaded_at - self.LATE_AFTER_SECONDS)
        except Exception as e:
            self.errors[type(e).__name__] += 1
        self._loaded_at = None

    def summary(self) -> dict:
        minutes = self.watched_seconds / 60
        return {
            "loads": self.loads,
            "watched_minutes": round(minutes, 1),
            "requests_on_its_own": [{"what": what, "count": n, "per_minute": round(n / minutes, 2) if minutes else None}
                                    for what, n in self.late.most_common(25)],
            "websockets": dict(self.sockets),
            "errors": dict(self.errors),
        }


def _safe(read):
    try:
        return read()
    except Exception:
        return None


def run_folder(apartment: str, kind: str, root: str | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", apartment)[:40]
    return Path(root or RUNS_DIR) / f"{stamp}_{cleaned}_{kind}"
