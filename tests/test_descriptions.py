"""Job description snapshots: URL recognition, per-board APIs, JSON-LD, reuse, failure handling."""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone

import pytest

from scanner import descriptions as desc
from scanner.model import Application

NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)
LOG = logging.getLogger("test")

GH = "https://boards.greenhouse.io/acme/jobs/4012345"
LEVER = "https://jobs.lever.co/brightloom/6f1c2d3e-aaaa-4bbb-8ccc-0123456789ab"
ASHBY = "https://jobs.ashbyhq.com/fernhill/0d9c8b7a-1111-4222-8333-444455556666"
WORKDAY = "https://quarry.wd5.myworkdayjobs.com/en-US/External/job/Chicago/Data-Platform-Engineer_R-10234"


class FakeResp:
    def __init__(self, status=200, text="", payload=None):
        self.status_code, self.text, self._payload = status, text, payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        r = self.responses.get(url, FakeResp(404))
        if isinstance(r, Exception):
            raise r
        return r


def make_app(app_id="app_aaaaaa", urls=(), description=None) -> Application:
    return Application(id=app_id, company="Acme", role="", source="other", applied_at="2026-10-02",
                       stage_history=[{"stage": "applied", "at": "2026-10-02T12:00:00Z", "message_id": None, "evidence": "inferred"}],
                       last_activity_at=NOW, urls=list(urls), description=description)


# ---------------------------------------------------------------- url recognition

class TestUrlRecognition:
    def test_known_boards(self):
        kinds = {u.kind: u for u in desc.extract_job_urls([GH, LEVER, ASHBY, WORKDAY])}
        assert set(kinds) == {"greenhouse", "lever", "ashby", "workday"}
        assert kinds["greenhouse"].params == {"board": "acme", "id": "4012345"}
        assert kinds["lever"].params == {"co": "brightloom", "id": "6f1c2d3e-aaaa-4bbb-8ccc-0123456789ab"}
        assert kinds["ashby"].params["co"] == "fernhill"

    def test_new_greenhouse_host_and_query_string(self):
        found = desc.extract_job_urls(["https://job-boards.greenhouse.io/acme/jobs/99?gh_src=abc"])
        assert found[0].kind == "greenhouse" and found[0].params["id"] == "99"

    @pytest.mark.parametrize("url", [
        "https://www.linkedin.com/jobs/view/123", "https://www.indeed.com/viewjob?jk=1", "https://calendly.com/jane/30min",
        "https://click.example.com/track?u=1", "https://acme.example/unsubscribe?id=1", "https://acme.example/logo.png",
        "https://list-manage.com/open", "mailto:jane@acme.example", "https://www.greenhouse.io/privacy",
    ])
    def test_skipped(self, url):
        assert desc.extract_job_urls([url]) == []

    def test_generic_career_page_is_kept(self):
        found = desc.extract_job_urls(["https://acme.example/careers/data-engineer"])
        assert [u.kind for u in found] == ["generic"]

    def test_unrelated_page_is_not_fetched(self):
        assert desc.extract_job_urls(["https://acme.example/about-us"]) == []

    def test_specific_boards_come_before_generic_and_duplicates_collapse(self):
        found = desc.extract_job_urls(["https://acme.example/careers/x", GH, GH, WORKDAY])
        assert [u.kind for u in found] == ["greenhouse", "workday", "generic"]


# ---------------------------------------------------------------- fetching

def fetcher(responses):
    session = FakeSession(responses)
    return desc.DescriptionFetcher(session=session, logger=LOG, now=NOW), session


class TestFetchers:
    def test_greenhouse_unescapes_html_content(self):
        content = html.escape("<p>Build <b>pipelines</b>.</p><ul><li>SQL</li></ul>")
        f, session = fetcher({"https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345": FakeResp(payload={"content": content})})
        got = f.fetch(desc.extract_job_urls([GH])[0])
        assert got["source"] == "greenhouse" and got["url"] == GH
        assert "Build pipelines." in got["text"] and "SQL" in got["text"] and "<" not in got["text"]
        assert got["fetched_at"] == "2026-10-14T18:00:00Z"

    def test_lever_joins_plain_text_and_lists(self):
        payload = {"descriptionPlain": "Own the warehouse.", "lists": [{"text": "Requirements", "content": "<li>SQL</li><li>Python</li>"}]}
        f, _ = fetcher({"https://api.lever.co/v0/postings/brightloom/6f1c2d3e-aaaa-4bbb-8ccc-0123456789ab": FakeResp(payload=payload)})
        got = f.fetch(desc.extract_job_urls([LEVER])[0])
        assert got["source"] == "lever"
        assert "Own the warehouse." in got["text"] and "Requirements" in got["text"] and "Python" in got["text"]

    def test_ashby_picks_the_job_with_the_matching_id(self):
        payload = {"jobs": [{"id": "other", "descriptionHtml": "<p>Wrong</p>"},
                            {"id": "0d9c8b7a-1111-4222-8333-444455556666", "descriptionHtml": "<p>Right one</p>"}]}
        f, _ = fetcher({"https://api.ashbyhq.com/posting-api/job-board/fernhill": FakeResp(payload=payload)})
        got = f.fetch(desc.extract_job_urls([ASHBY])[0])
        assert got["source"] == "ashby" and got["text"] == "Right one"

    def test_ashby_missing_job_fails(self):
        f, _ = fetcher({"https://api.ashbyhq.com/posting-api/job-board/fernhill": FakeResp(payload={"jobs": []})})
        assert f.fetch(desc.extract_job_urls([ASHBY])[0]) is None

    def test_workday_reads_json_ld(self):
        page = ('<html><head><script type="application/ld+json">' + json.dumps(
            {"@type": "JobPosting", "description": "<p>Design the <i>platform</i>.</p>"}) + "</script></head></html>")
        f, _ = fetcher({WORKDAY: FakeResp(text=page)})
        got = f.fetch(desc.extract_job_urls([WORKDAY])[0])
        assert got["source"] == "workday" and got["text"] == "Design the platform."

    def test_generic_json_ld_inside_graph(self):
        page = ('<script type="application/ld+json">' + json.dumps(
            {"@graph": [{"@type": "Organization"}, {"@type": ["Thing", "JobPosting"], "description": "Plain description text"}]}) + "</script>")
        f, _ = fetcher({"https://acme.example/careers/de": FakeResp(text=page)})
        got = f.fetch(desc.extract_job_urls(["https://acme.example/careers/de"])[0])
        assert got["source"] == "jsonld" and got["text"] == "Plain description text"

    def test_generic_falls_back_to_main_text_when_long_enough(self):
        body = " ".join(["Responsibilities include building pipelines."] * 30)
        f, _ = fetcher({"https://acme.example/careers/de": FakeResp(text=f"<html><body><nav>menu</nav><main><p>{body}</p></main></body></html>")})
        got = f.fetch(desc.extract_job_urls(["https://acme.example/careers/de"])[0])
        assert got["source"] == "page" and "Responsibilities" in got["text"] and "menu" not in got["text"]

    def test_generic_gives_up_on_short_pages(self):
        f, _ = fetcher({"https://acme.example/careers/de": FakeResp(text="<main>too short</main>")})
        assert f.fetch(desc.extract_job_urls(["https://acme.example/careers/de"])[0]) is None

    def test_text_is_capped(self):
        content = html.escape("<p>" + "x" * 50000 + "</p>")
        f, _ = fetcher({"https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345": FakeResp(payload={"content": content})})
        assert len(f.fetch(desc.extract_job_urls([GH])[0])["text"]) == 30000

    def test_request_uses_browser_user_agent_and_timeout(self):
        f, session = fetcher({"https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345": FakeResp(payload={"content": "x"})})
        f.fetch(desc.extract_job_urls([GH])[0])
        call = session.calls[0]
        assert call["timeout"] == 15
        assert "Mozilla" in call["headers"]["User-Agent"]

    def test_failures_never_raise(self):
        for resp in (FakeResp(500), FakeResp(200, payload=None), RuntimeError("boom")):
            f, _ = fetcher({"https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345": resp})
            assert f.fetch(desc.extract_job_urls([GH])[0]) is None
            assert f.stats["failed"] == 1

    def test_one_fetch_per_url_per_run_even_when_it_fails(self):
        f, session = fetcher({})
        url = desc.extract_job_urls([GH])[0]
        f.fetch(url)
        f.fetch(url)
        assert len(session.calls) == 1


# ---------------------------------------------------------------- collection

GH_API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345"


class TestCollect:
    def test_fills_missing_descriptions(self):
        app = make_app(urls=["https://acme.example/about", GH])
        f, _ = fetcher({GH_API: FakeResp(payload={"content": "Hello"})})
        counts = desc.collect([app], {}, f)
        assert app.description["source"] == "greenhouse" and app.description["text"] == "Hello"
        assert counts == {"fetched": 1, "reused": 0, "failed": 0}

    def test_reuses_previous_snapshot_without_fetching(self):
        app = make_app(urls=[GH])
        previous = {app.id: {"url": GH, "text": "Old text", "source": "greenhouse", "fetched_at": "2026-10-01T00:00:00Z"}}
        f, session = fetcher({})
        counts = desc.collect([app], previous, f)
        assert app.description["text"] == "Old text"
        assert session.calls == [] and counts["reused"] == 1

    def test_previous_without_text_is_not_reused(self):
        app = make_app(urls=[GH])
        previous = {app.id: {"url": GH, "text": "", "source": "greenhouse", "fetched_at": "2026-10-01T00:00:00Z"}}
        f, _ = fetcher({GH_API: FakeResp(payload={"content": "New"})})
        desc.collect([app], previous, f)
        assert app.description["text"] == "New"

    def test_manual_description_is_left_alone(self):
        manual = {"url": None, "text": "mine", "source": "manual", "fetched_at": "2026-10-10T00:00:00Z"}
        app = make_app(urls=[GH], description=manual)
        f, session = fetcher({})
        desc.collect([app], {}, f)
        assert app.description == manual and session.calls == []

    def test_tries_next_candidate_after_failure_and_counts_failures(self):
        app = make_app(urls=[GH, LEVER])
        f, session = fetcher({"https://api.lever.co/v0/postings/brightloom/6f1c2d3e-aaaa-4bbb-8ccc-0123456789ab":
                              FakeResp(payload={"descriptionPlain": "Lever text"})})
        counts = desc.collect([app], {}, f)
        assert app.description["source"] == "lever"
        assert counts == {"fetched": 1, "reused": 0, "failed": 1}

    def test_shared_url_is_fetched_once_for_two_applications(self):
        a, b = make_app("app_aaaaaa", [GH]), make_app("app_bbbbbb", [GH])
        f, session = fetcher({GH_API: FakeResp(payload={"content": "Hello"})})
        desc.collect([a, b], {}, f)
        assert len(session.calls) == 1 and a.description and b.description

    def test_no_urls_means_no_description(self):
        app = make_app()
        f, session = fetcher({})
        counts = desc.collect([app], {}, f)
        assert app.description is None and counts == {"fetched": 0, "reused": 0, "failed": 0}

    def test_at_most_three_candidates_per_application(self):
        urls = [f"https://acme.example/careers/{i}" for i in range(6)]
        app = make_app(urls=urls)
        f, session = fetcher({})
        desc.collect([app], {}, f)
        assert len(session.calls) == 3
