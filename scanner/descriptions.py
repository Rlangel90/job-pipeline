"""Job description snapshots.

Postings get taken down once a role is filled, so the scanner saves the text while it is still
there. Greenhouse, Lever and Ashby expose public JSON; Workday and company career pages are
read from their JSON-LD block. Every failure is swallowed and counted: a missing description
must never fail a scan.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from . import classify
from .model import Application, iso_ts

TIMEOUT = 15
TEXT_CAP = 30000
MAX_CANDIDATES = 3
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

GREENHOUSE_RE = re.compile(r"^https?://(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?P<board>[\w-]+)/jobs/(?P<id>\d+)")  # boards.greenhouse.io/acme/jobs/123
LEVER_RE = re.compile(r"^https?://jobs(?:\.eu)?\.lever\.co/(?P<co>[\w.-]+)/(?P<id>[0-9a-fA-F-]{36})")  # jobs.lever.co/acme/<uuid>
ASHBY_RE = re.compile(r"^https?://jobs\.ashbyhq\.com/(?P<co>[\w.%-]+)/(?P<id>[0-9a-fA-F-]{36})")  # jobs.ashbyhq.com/acme/<uuid>
WORKDAY_RE = re.compile(r"^https?://[\w.-]+\.myworkdayjobs\.com/\S+")  # acme.wd5.myworkdayjobs.com/en-US/x/job/...

_SKIP_DOMAINS = ["linkedin.com", "indeed.com", "calendly.com", "google.com", "w3.org", "schema.org", "list-manage.com",
                 "mailchi.mp", "sendgrid.net", "googleapis.com", "gstatic.com"]
_SKIP_HOST_PARTS = ("click.", "track.", "tracking.", "links.", "email.", "mail.", "trk.", "url.", "r.")
_SKIP_PATH_RE = re.compile(r"unsubscribe|optout|opt-out|/track|/click|\.(?:png|jpe?g|gif|svg|css|js|ico|woff2?)(?:\?|$)", re.I)
_JOB_HINT_RE = re.compile(r"/(?:careers?|jobs?|positions?|openings?|opportunities|join|apply|vacanc\w+)(?:/|$|\?)|gh_jid=|lever-source", re.I)
_BLOCK_TAGS = ["p", "div", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "table"]


@dataclass
class JobUrl:
    kind: str  # greenhouse | lever | ashby | workday | generic
    url: str
    params: dict = field(default_factory=dict)


_KIND_ORDER = {"greenhouse": 0, "lever": 0, "ashby": 0, "workday": 1, "generic": 2}


def classify_url(url: str) -> JobUrl | None:
    if not url.lower().startswith(("http://", "https://")):
        return None
    for kind, rx in (("greenhouse", GREENHOUSE_RE), ("lever", LEVER_RE), ("ashby", ASHBY_RE)):
        m = rx.match(url)
        if m:
            return JobUrl(kind, url, m.groupdict())
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host or _SKIP_PATH_RE.search(url):
        return None
    if classify.domain_in(host, _SKIP_DOMAINS) or any(host.startswith(p) for p in _SKIP_HOST_PARTS):
        return None
    if WORKDAY_RE.match(url):
        return JobUrl("workday", url)
    if classify.is_ats(host) or classify.is_board(host) or classify.domain_in(host, list(classify.GENERIC_MAIL_DOMAINS)):
        return None
    if _JOB_HINT_RE.search(parsed.path + ("?" + parsed.query if parsed.query else "")):
        return JobUrl("generic", url)
    return None


def extract_job_urls(urls: list[str]) -> list[JobUrl]:
    """Recognized posting URLs, specific boards first, duplicates dropped, everything else skipped."""
    seen: dict[str, JobUrl] = {}
    for url in urls:
        found = classify_url(url)
        if found and found.url not in seen:
            seen[found.url] = found
    return sorted(seen.values(), key=lambda j: _KIND_ORDER[j.kind])  # sorted() is stable


# ---------------------------------------------------------------- text helpers

def html_to_plain(markup: str) -> str:
    """Readable text from posting HTML: block tags become line breaks, list items become dashes."""
    if "<" not in markup:
        return htmllib.unescape(markup).strip()
    soup = BeautifulSoup(markup, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, "- ")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.append("\n")
    text = soup.get_text("")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.replace("\r", "").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _walk_json(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json(item)


def jobposting_text(page_html: str) -> str | None:
    soup = BeautifulSoup(page_html, "html.parser")
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        for node in _walk_json(data):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "JobPosting" in kinds and node.get("description"):
                text = html_to_plain(htmllib.unescape(str(node["description"])) if "&lt;" in str(node["description"]) else str(node["description"]))
                if text:
                    return text
    return None


def _page_text(page_html: str) -> str | None:
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in ("main", "article"):
        node = soup.find(tag)
        if node is not None:
            text = html_to_plain(str(node))
            if len(text) > 500:
                return text
    return None


# ---------------------------------------------------------------- fetching

class DescriptionFetcher:
    """Fetches posting text with a per-run cache so no URL is requested twice."""

    def __init__(self, session=None, logger: logging.Logger | None = None, now: datetime | None = None):
        if session is None:
            import requests
            session = requests.Session()
        self.session = session
        self.logger = logger or logging.getLogger(__name__)
        self.now = now
        self.stats = {"fetched": 0, "reused": 0, "failed": 0}
        self._cache: dict[str, object] = {}

    # one GET per URL per run, success or not
    def _get(self, url: str):
        if url in self._cache:
            cached = self._cache[url]
            if cached is None:
                raise LookupError("cached failure")
            return cached
        try:
            resp = self.session.get(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"}, timeout=TIMEOUT)
            if resp.status_code != 200:
                raise LookupError(f"status {resp.status_code}")
        except Exception:  # noqa: BLE001 - any network or protocol problem is just a missed snapshot
            self._cache[url] = None
            raise
        self._cache[url] = resp
        return resp

    def _json(self, url: str):
        return self._get(url).json()

    def _extract(self, job: JobUrl) -> tuple[str, str] | None:
        if job.kind == "greenhouse":
            data = self._json(f"https://boards-api.greenhouse.io/v1/boards/{job.params['board']}/jobs/{job.params['id']}")
            return "greenhouse", html_to_plain(htmllib.unescape(data["content"]))
        if job.kind == "lever":
            data = self._json(f"https://api.lever.co/v0/postings/{job.params['co']}/{job.params['id']}")
            parts = [data.get("descriptionPlain") or ""]
            for block in data.get("lists") or []:
                parts.append(block.get("text") or "")
                parts.append(html_to_plain(block.get("content") or ""))
            parts.append(data.get("additionalPlain") or "")
            return "lever", "\n\n".join(p.strip() for p in parts if p and p.strip())
        if job.kind == "ashby":
            data = self._json(f"https://api.ashbyhq.com/posting-api/job-board/{job.params['co']}")
            for posting in data.get("jobs") or []:
                if str(posting.get("id", "")).lower() == job.params["id"].lower():
                    text = html_to_plain(posting.get("descriptionHtml") or "") or posting.get("descriptionPlain") or ""
                    return "ashby", text
            return None
        page = self._get(job.url).text
        if job.kind == "workday":
            text = jobposting_text(page)
            return ("workday", text) if text else None
        text = jobposting_text(page)
        if text:
            return "jsonld", text
        text = _page_text(page)
        return ("page", text) if text else None

    def fetch(self, job: JobUrl) -> dict | None:
        try:
            found = self._extract(job)
        except Exception:  # noqa: BLE001
            self.stats["failed"] += 1
            return None
        if not found or not found[1].strip():
            self.stats["failed"] += 1
            return None
        source, text = found
        return {"url": job.url, "text": text.strip()[:TEXT_CAP], "source": source,
                "fetched_at": iso_ts(self.now or datetime.now().astimezone())}

    def fetch_text(self, url: str) -> str | None:
        """Best-effort text for an arbitrary URL (used for the `description_url` override)."""
        job = classify_url(url) or JobUrl("generic", url)
        got = self.fetch(job)
        return got["text"] if got else None


def collect(apps: list[Application], previous: dict[str, dict], fetcher: DescriptionFetcher) -> dict:
    """Attach a description to every application that lacks one: reuse the old snapshot, else fetch."""
    for app in apps:
        if app.description is not None:
            continue
        old = previous.get(app.id)
        if old and (old.get("text") or "").strip():
            app.description = dict(old)
            fetcher.stats["reused"] += 1
            continue
        for job in extract_job_urls(app.urls)[:MAX_CANDIDATES]:
            got = fetcher.fetch(job)
            if got:
                app.description = got
                fetcher.stats["fetched"] += 1
                break
    return dict(fetcher.stats)
