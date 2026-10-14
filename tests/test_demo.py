"""The demo generator: fake but believable, schema valid, reproducible."""
from __future__ import annotations

import json
import re
from collections import Counter

import pytest

from scanner import build, demo


@pytest.fixture(scope="module")
def data():
    return demo.generate(7)


def test_validates_against_the_schema(data):
    assert build.validate(data) == []


def test_shape_and_window(data):
    assert data["generated_at"] == "2026-10-14T18:00:00Z"
    assert data["scan_window_start"] == "2026-10-01"
    assert len(data["applications"]) == 28
    assert [s["id"] for s in data["stages"]][0] == "applied"


def test_stage_mix(data):
    current = Counter(a["current_stage"] for a in data["applications"])
    assert current["accepted"] == 1 and current["offer"] == 1 and current["withdrew"] == 1
    assert current["onsite"] == 1 and current["ghosted"] >= 3
    assert current["rejected"] >= 12
    active = [a for a in data["applications"] if not a["terminal"]]
    assert 5 <= len(active) <= 8
    onsite_reached = [a for a in data["applications"] if any(h["stage"] == "onsite" for h in a["stage_history"])]
    assert len(onsite_reached) >= 2
    technical_reached = [a for a in data["applications"] if any(h["stage"] == "technical" for h in a["stage_history"])]
    assert len(technical_reached) >= 3


def test_stats_match_applications(data):
    assert data["stats"] == build.compute_stats(data["applications"])
    assert data["stats"]["applications"] == 28


def test_contacts_have_titles(data):
    assert 14 <= len(data["contacts"]) <= 18
    assert all(c["title"] and c["company"] and c["application_ids"] for c in data["contacts"])


def test_messages_per_application(data):
    counts = [len(a["message_ids"]) for a in data["applications"]]
    assert min(counts) >= 2 and max(counts) <= 7
    assert 3 <= sum(counts) / len(counts) <= 4.5


def test_events_and_descriptions(data):
    assert 3 <= len(data["events"]) <= 6
    with_desc = [a for a in data["applications"] if a["description"]]
    assert 8 <= len(with_desc) <= 11
    for a in with_desc:
        paragraphs = [p for p in a["description"]["text"].split("\n\n") if p.strip()]
        assert 3 <= len(paragraphs) <= 6


def test_dates_stay_inside_the_window(data):
    for m in data["messages"]:
        assert "2026-10-01" <= m["date"][:10] <= "2026-10-14"
    assert all(m["date"] <= data["generated_at"] for m in data["messages"])


def test_only_fake_addresses(data):
    emails = {m["from"]["email"] for m in data["messages"]} | {p["email"] for m in data["messages"] for p in m["to"]} | {c["email"] for c in data["contacts"]}
    assert emails
    assert all(re.search(r"@([\w-]+\.)*example(\.com)?$", e) for e in emails), emails


def test_messages_are_sorted_and_urls_are_placeholders(data):
    dates = [m["date"] for m in data["messages"]]
    assert dates == sorted(dates)
    assert all(m["gmail_url"] == "https://mail.google.com/mail/u/0/#all/demo" for m in data["messages"])


def test_application_ids_are_unique(data):
    ids = [a["id"] for a in data["applications"]]
    assert len(ids) == len(set(ids))


def test_same_seed_same_bytes(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    demo.write_demo(a, 7)
    demo.write_demo(b, 7)
    assert a.read_bytes() == b.read_bytes()


def test_different_seed_differs_but_still_validates(tmp_path):
    other = demo.generate(8)
    assert build.validate(other) == []
    assert json.dumps(other, sort_keys=True) != json.dumps(demo.generate(7), sort_keys=True)


def test_written_file_is_valid_json_with_trailing_newline(tmp_path):
    p = tmp_path / "docs" / "data" / "demo.json"
    demo.write_demo(p, 7)
    raw = p.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert build.validate(json.loads(raw)) == []
