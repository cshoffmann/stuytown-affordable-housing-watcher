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

    report.json    steps + timings, the form's fields and buttons, the outcome
    network.json   requests and responses (bodies for page/API calls)
    *.html         the page at each stage
    *.jpg          screenshots without your details
"""

import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = "data/apply_runs"
MAX_HTML_BYTES = 1_500_000
MAX_BODY_CHARS = 50_000
MAX_REQUESTS = 400
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
    (nothing slows down the application) and writes it all in finish()."""

    def __init__(self, folder: str | Path | None, redact: Redactor):
        self.folder = Path(folder) if folder else None
        self.redact = redact
        self.started = time.monotonic()
        self.started_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.steps, self.console, self.notes = [], [], {}
        self.pages, self.images = {}, {}
        self._requests, self._responses = [], {}

    # ---------------------------------------------------------------- collect

    def watch(self, context) -> None:
        """Start recording the browser's network traffic and console."""
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfailed", self._on_failed)
        context.on("page", self._watch_page)
        for page in context.pages:
            self._watch_page(page)

    def _watch_page(self, page) -> None:
        page.on("console", lambda msg: self._on_console(msg.type, msg.text))
        page.on("pageerror", lambda error: self._on_console("pageerror", str(error)))

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

    # ------------------------------------------------------------------ write

    def finish(self, outcome: dict) -> Path | None:
        """Write everything (redacted). Call while the browser is still open:
        response bodies are read here. Never raises."""
        if self.folder is None:
            return None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            report = {
                "started_utc": self.started_utc,
                "seconds": self._now(),
                "outcome": outcome,
                "steps": self.steps,
                "notes": self.notes,
                "console": self.console,
                "files": sorted([f"{n}.html" for n in self.pages] + [f"{n}.jpg" for n in self.images]
                                + ["network.json"]),
            }
            self._write("report.json", json.dumps(self.redact.deep(report), indent=2))
            self._write("network.json", json.dumps(self._network(), indent=2))
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

    def _network(self) -> list:
        entries = []
        for entry in self._requests:
            request = entry["request"]
            item = {
                "t": entry["t"],
                "method": request.method,
                "type": request.resource_type,
                "url": self._redact_url(request.url),
            }
            if entry.get("failure"):
                item["failed"] = entry["failure"]  # incl. what auto-apply itself blocks (images etc.)
            if request.method != "GET" or request.resource_type in BODY_TYPES:
                item["request_headers"] = self._headers(request.headers)
                body = _safe(lambda: request.post_data)
                if body:
                    item["request_body"] = self._redact_body(body, request.headers.get("content-type", ""))
            answered = self._responses.get(id(request))
            if answered:
                t, response = answered
                item.update({"answered_t": t, "status": response.status})
                if request.method != "GET" or request.resource_type in BODY_TYPES:
                    item["response_headers"] = self._headers(response.headers)
                    body = _safe(lambda: response.text())
                    if body is not None:
                        item["response_body"] = self.redact(body)[:MAX_BODY_CHARS]
            entries.append(item)
        return entries

    def _headers(self, headers: dict) -> dict:
        return {k: self.redact(v) for k, v in headers.items() if k.lower() not in DROPPED_HEADERS}

    def _redact_url(self, url: str) -> str:
        return self.redact(urllib.parse.unquote_plus(url))

    def _redact_body(self, body: str, content_type: str) -> str:
        if "x-www-form-urlencoded" in content_type:
            body = urllib.parse.unquote_plus(body)
        return self.redact(body)[:MAX_BODY_CHARS]


def _safe(read):
    try:
        return read()
    except Exception:
        return None


def run_folder(apartment: str, kind: str, root: str | None = None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", apartment)[:40]
    return Path(root or RUNS_DIR) / f"{stamp}_{cleaned}_{kind}"
