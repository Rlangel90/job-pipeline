"""Output assembly: schema validity, stats, ordering, references, previous-blob reuse."""
from __future__ import annotations

import copy
import json
import logging
from datetime import date, datetime, timezone

import pytest

from scanner import build, overrides as ovr
from tests.helpers import ALL_FIXTURES, FIXTURES, load_eml
from tests.test_descriptions import FakeResp, FakeSession

OWNER = "owner@example.com"
NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)
SINCE = date(2026, 10, 1)


def scan(now=NOW, ov=None, previous=None, session=None, names=ALL_FIXTURES):
    raws = [load_eml(n) for n in names]
    return build.run_scan(raws, OWNER, SINCE, ov or ovr.Overrides(), previous, now, session=session or FakeSession({}))


@pytest.fixture(scope="module")
def result():
    return scan()


def test_output_validates_against_the_schema(result):
    assert build.validate(result.data) == []
    build.check(result.data)


def test_output_with_overrides_validates():
    res = scan(ov=ovr.load_overrides(FIXTURES / "overrides_sample.yml"))
    assert build.validate(res.data) == []
    companies = {a["company"] for a in res.data["applications"]}
    assert "Stealth startup" in companies and "Orbitwave" not in companies


def test_top_level_shape(result):
    d = result.data
    assert list(d) == ["version", "generated_at", "scan_window_start", "stages", "applications", "contacts", "messages", "events", "stats"]
    assert d["version"] == 1 and d["generated_at"] == "2026-10-14T18:00:00Z" and d["scan_window_start"] == "2026-10-01"
    assert [s["id"] for s in d["stages"]] == ["applied", "recruiter_screen", "hiring_manager", "technical", "onsite", "offer", "accepted", "rejected", "withdrew", "ghosted"]
    assert [s["kind"] for s in d["stages"]] == ["active"] * 6 + ["terminal"] * 4


def test_counts_are_reported(result):
    c = result.counts
    assert c["messages_fetched"] == len(ALL_FIXTURES)
    assert c["applications"] == 9 and c["contacts"] == 3
    assert c["candidate_threads"] == 14  # 17 messages: two noise threads dropped, the reply shares the take-home thread
    assert (c["descriptions_fetched"], c["descriptions_reused"]) == (0, 0)


def test_sorting(result):
    d = result.data
    assert [a["last_activity_at"] for a in d["applications"]] == sorted((a["last_activity_at"] for a in d["applications"]), reverse=True)
    assert [m["date"] for m in d["messages"]] == sorted(m["date"] for m in d["messages"])
    assert [c["last_seen"] for c in d["contacts"]] == sorted((c["last_seen"] for c in d["contacts"]), reverse=True)


def test_noise_messages_are_not_published(result):
    subjects = [m["subject"] for m in result.data["messages"]]
    assert not any("new jobs" in s or "viewing your profile" in s for s in subjects)
    assert len(subjects) == len(ALL_FIXTURES) - 2


def test_message_fields(result):
    msg = [m for m in result.data["messages"] if m["subject"] == "Offer letter - Senior Data Engineer at Acme Corp"][0]
    assert msg["direction"] == "in" and msg["stage_signal"] == "offer" and msg["labels"] == []
    assert msg["from"] == {"name": "Jane Doe", "email": "jane.doe@acme.example"}
    assert msg["to"] == [{"name": "Alex Morgan", "email": OWNER}]
    assert msg["gmail_url"].startswith("https://mail.google.com/mail/u/0/#all/")
    labelled = [m for m in result.data["messages"] if "Lumenridge" in m["subject"]][0]
    assert labelled["labels"] == ["jobs/rejected"] and labelled["stage_signal"] == "rejected"


def test_stats(result):
    assert result.data["stats"] == {"applications": 9, "active": 6, "offers": 1, "rejected": 2, "ghosted": 0, "response_rate": 0.556}


def test_ghosting_through_the_pipeline():
    later = datetime(2026, 11, 30, 12, 0, tzinfo=timezone.utc)
    res = scan(now=later)
    stats = res.data["stats"]
    assert stats["ghosted"] == 6 and stats["active"] == 0
    acme = [a for a in res.data["applications"] if a["company"] == "Acme Corp"][0]
    assert acme["stage_history"][-1] == {"stage": "ghosted", "at": "2026-11-03T19:00:00Z", "message_id": None, "evidence": "timeout"}
    assert build.validate(res.data) == []


def test_ghost_window_comes_from_overrides():
    ov = ovr.parse_overrides({"ghost_after_days": 5}, FIXTURES)
    res = scan(ov=ov)
    assert res.data["stats"]["ghosted"] >= 4


def test_deterministic(result):
    again = scan()
    assert json.dumps(again.data, sort_keys=True) == json.dumps(result.data, sort_keys=True)


def test_previous_description_is_reused_without_fetching():
    first = scan()
    acme = [a for a in first.data["applications"] if a["company"] == "Acme Corp"][0]
    prev = copy.deepcopy(first.data)
    for a in prev["applications"]:
        if a["id"] == acme["id"]:
            a["description"] = {"url": "https://boards.greenhouse.io/acme/jobs/4012345", "text": "Kept snapshot", "source": "greenhouse", "fetched_at": "2026-10-02T14:05:00Z"}
    session = FakeSession({})
    res = scan(previous=prev, session=session)
    got = [a for a in res.data["applications"] if a["id"] == acme["id"]][0]
    assert got["description"]["text"] == "Kept snapshot"
    assert res.counts["descriptions_reused"] == 1
    assert not [c for c in session.calls if "greenhouse" in c["url"]]


def test_description_is_fetched_from_a_posting_link_in_mail():
    api = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/4012345"
    session = FakeSession({api: FakeResp(payload={"content": "Posting text"})})
    res = scan(session=session)
    acme = [a for a in res.data["applications"] if a["company"] == "Acme Corp"][0]
    assert acme["description"]["source"] == "greenhouse" and acme["description"]["text"] == "Posting text"
    assert res.counts["descriptions_fetched"] == 1


def test_one_unparsable_message_does_not_sink_the_scan(monkeypatch):
    real = build.mail.parse_message

    def flaky(raw, owner):
        if "Offer letter" in str(raw.message["Subject"]):
            raise ValueError("malformed header")
        return real(raw, owner)
    monkeypatch.setattr(build.mail, "parse_message", flaky)
    res = scan()
    assert res.counts["messages_unparsable"] == 1
    assert build.validate(res.data) == []
    assert not any("Offer letter" in m["subject"] for m in res.data["messages"])


def test_run_scan_logs_counts_only(caplog):
    with caplog.at_level(logging.DEBUG):
        scan()
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("Acme", "Brightloom", "Jane", "owner@example.com"):
        assert secret not in text


def test_contact_first_seen_is_carried_forward():
    first = scan()
    prev = copy.deepcopy(first.data)
    prev["contacts"][0]["first_seen"] = "2026-09-01T00:00:00Z"
    res = scan(previous=prev)
    carried = [c for c in res.data["contacts"] if c["id"] == prev["contacts"][0]["id"]][0]
    assert carried["first_seen"] == "2026-09-01T00:00:00Z"


# ---------------------------------------------------------------- stats and validation units

def app_stub(stages_seen, current=None):
    history = [{"stage": s, "at": "2026-10-01T00:00:00Z", "message_id": None, "evidence": "x"} for s in stages_seen]
    current = current or stages_seen[-1]
    return {"stage_history": history, "current_stage": current, "terminal": current in ("accepted", "rejected", "withdrew", "ghosted")}


class TestStats:
    def test_empty(self):
        assert build.compute_stats([])["response_rate"] == 0.0

    def test_response_rate_rules(self):
        apps = [
            app_stub(["applied"]),                       # no response
            app_stub(["applied", "ghosted"]),            # ghosted is not a response
            app_stub(["applied", "rejected"]),           # terminal other than ghosted is a response
            app_stub(["applied", "recruiter_screen"]),   # moved beyond applied
        ]
        stats = build.compute_stats(apps)
        assert stats == {"applications": 4, "active": 2, "offers": 0, "rejected": 1, "ghosted": 1, "response_rate": 0.5}

    def test_offers_count_ever_reaching_offer(self):
        apps = [app_stub(["applied", "offer", "rejected"]), app_stub(["applied", "offer", "accepted"]), app_stub(["applied", "offer"])]
        assert build.compute_stats(apps)["offers"] == 3

    def test_response_rate_is_rounded_to_three_places(self):
        apps = [app_stub(["applied", "rejected"]), app_stub(["applied"]), app_stub(["applied"])]
        assert build.compute_stats(apps)["response_rate"] == 0.333


class TestValidation:
    def test_reports_the_path_not_the_value(self, result):
        bad = copy.deepcopy(result.data)
        bad["applications"][0]["source"] = "SECRET-VALUE"
        paths = build.validate(bad)
        assert paths == ["/applications/0/source"]
        assert "SECRET" not in " ".join(paths)

    def test_dangling_reference_is_reported(self, result):
        bad = copy.deepcopy(result.data)
        bad["applications"][0]["contact_ids"].append("c_missing")
        assert build.validate(bad) == [f"/applications/0/contact_ids/{len(bad['applications'][0]['contact_ids']) - 1}"]

    def test_terminal_must_be_last(self, result):
        bad = copy.deepcopy(result.data)
        app = [a for a in bad["applications"] if a["stage_history"][-1]["stage"] == "rejected"][0]
        app["stage_history"].append({"stage": "applied", "at": "2026-12-01T00:00:00Z", "message_id": None, "evidence": "x"})
        assert build.validate(bad)

    def test_check_raises_with_paths(self, result):
        bad = copy.deepcopy(result.data)
        del bad["stats"]
        with pytest.raises(build.ValidationFailure) as exc:
            build.check(bad)
        assert exc.value.paths == ["/"]
