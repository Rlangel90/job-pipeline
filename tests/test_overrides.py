"""Manual overrides: rename, ignore, forced stage, manual applications, descriptions, contacts."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from scanner import applications as appmod
from scanner import mail
from scanner import overrides as ovr
from scanner.model import application_id
from tests.helpers import ALL_FIXTURES, FIXTURES, load_eml

OWNER = "owner@example.com"
NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)
SAMPLE = FIXTURES / "overrides_sample.yml"


def fresh():
    parsed = [mail.parse_message(load_eml(n), OWNER) for n in ALL_FIXTURES]
    messages = {m.id: m for m in parsed}
    built = appmod.build_applications(appmod.analyse_threads(parsed, None, OWNER), OWNER)
    contacts = appmod.build_contacts(built, messages, OWNER)
    events = appmod.build_events(built, messages, OWNER)
    return built, contacts, events


def apply(data: dict, tmp_path=None, fetch_text=None):
    built, contacts, events = fresh()
    ov = ovr.parse_overrides(data, tmp_path or FIXTURES)
    return ovr.apply_overrides(built, contacts, events, ov, NOW, fetch_text=fetch_text)


def by_company(result):
    return {a.company: a for a in result.apps}


# ---------------------------------------------------------------- loading

class TestLoading:
    def test_sample_file_loads(self):
        ov = ovr.load_overrides(SAMPLE)
        assert len(ov.applications) == 5
        assert ov.ghost_after_days == 21
        assert ov.ignore_senders == ["newsletter@example.com"]
        assert ov.base_dir == SAMPLE.parent

    def test_no_file_means_defaults(self):
        ov = ovr.load_overrides(None)
        assert ov.applications == [] and ov.ghost_after_days == 21 and ov.rules().ignore_senders == []

    def test_rules_carry_ignore_lists(self):
        rules = ovr.load_overrides(SAMPLE).rules()
        assert rules.ignore_senders == ["newsletter@example.com"]
        assert rules.ignore_subjects[0].search("Weekly digest #4")

    def test_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(ovr.OverridesError):
            ovr.load_overrides(tmp_path / "nope.yml")

    def test_bad_yaml_is_an_error(self, tmp_path):
        p = tmp_path / "o.yml"
        p.write_text("applications: [unclosed", encoding="utf-8")
        with pytest.raises(ovr.OverridesError):
            ovr.load_overrides(p)

    def test_unknown_stage_is_rejected(self):
        with pytest.raises(ovr.OverridesError):
            ovr.parse_overrides({"applications": [{"match": {"company": "X"}, "stage": "hired"}]}, FIXTURES)

    def test_bad_ignore_regex_is_rejected(self):
        with pytest.raises(ovr.OverridesError):
            ovr.parse_overrides({"ignore_subjects_matching": ["("]}, FIXTURES)


# ---------------------------------------------------------------- matching, rename, ignore

class TestRenameAndIgnore:
    def test_rename_rekeys_the_id_and_remaps_references(self):
        built, contacts, events = fresh()
        old_id = {a.company: a for a in built}["Harborlight"].id
        ov = ovr.parse_overrides({"applications": [{"match": {"company": "Harborlight"}, "company": "Harborlight Health", "notes": "Referral."}]}, FIXTURES)
        res = ovr.apply_overrides(built, contacts, events, ov, NOW)
        new = {a.company: a for a in res.apps}["Harborlight Health"]
        assert new.id == application_id("Harborlight Health", "Analytics Engineer") != old_id
        assert new.notes == "Referral."
        assert "Harborlight" not in {a.company for a in res.apps}

    def test_rename_updates_contacts_and_events(self):
        built, contacts, events = fresh()
        ov = ovr.parse_overrides({"applications": [{"match": {"company": "Acme Corp"}, "company": "Acme Holdings"}]}, FIXTURES)
        res = ovr.apply_overrides(built, contacts, events, ov, NOW)
        new = {a.company: a for a in res.apps}["Acme Holdings"]
        assert res.events[0]["application_id"] == new.id
        jane = [c for c in res.contacts if c["email"] == "jane.doe@acme.example"][0]
        assert jane["application_ids"] == [new.id]

    def test_match_by_id(self):
        built, contacts, events = fresh()
        target = {a.company: a for a in built}["Brightloom"]
        ov = ovr.parse_overrides({"applications": [{"match": {"id": target.id}, "notes": "by id"}]}, FIXTURES)
        res = ovr.apply_overrides(built, contacts, events, ov, NOW)
        assert {a.company: a for a in res.apps}["Brightloom"].notes == "by id"

    def test_match_with_role_uses_loose_overlap(self):
        res = apply({"applications": [{"match": {"company": "Acme", "role": "Data Engineer"}, "notes": "hi"}]})
        assert by_company(res)["Acme Corp"].notes == "hi"
        res = apply({"applications": [{"match": {"company": "Acme", "role": "Pastry Chef"}, "notes": "hi"}]})
        assert by_company(res)["Acme Corp"].notes == ""

    def test_ignore_drops_application_and_its_references(self):
        res = apply({"applications": [{"match": {"company": "Acme Corp"}, "ignore": True}]})
        assert "Acme Corp" not in by_company(res)
        assert res.events == []
        assert all(c["email"] != "jane.doe@acme.example" for c in res.contacts)

    def test_rename_collision_merges_applications(self):
        built, contacts, events = fresh()
        n = len(built)
        ov = ovr.parse_overrides({"applications": [{"match": {"company": "Tallpine Data"}, "company": "Quarry Systems", "role": "Data Platform Engineer"}]}, FIXTURES)
        res = ovr.apply_overrides(built, contacts, events, ov, NOW)
        assert len(res.apps) == n - 1
        merged = by_company(res)["Quarry Systems"]
        assert len(merged.message_ids) == 2

    def test_unmatched_entry_is_counted_not_fatal(self):
        res = apply({"applications": [{"match": {"company": "Nobody"}, "notes": "x"}]})
        assert res.stats["unmatched"] == 1


# ---------------------------------------------------------------- forced stage

class TestStageOverride:
    def test_force_terminal_stage_with_date(self):
        res = apply({"applications": [{"match": {"company": "Quarry Systems"}, "stage": "rejected", "stage_at": date(2026, 10, 10), "notes": "Budget froze."}]})
        app = by_company(res)["Quarry Systems"]
        assert app.current_stage == "rejected" and app.terminal
        assert app.stage_history[-1] == {"stage": "rejected", "at": "2026-10-10T00:00:00Z", "message_id": None, "evidence": "override"}
        assert app.notes == "Budget froze."

    def test_force_active_stage_without_date_uses_now(self):
        res = apply({"applications": [{"match": {"company": "Quarry Systems"}, "stage": "onsite"}]})
        app = by_company(res)["Quarry Systems"]
        assert app.current_stage == "onsite" and not app.terminal
        assert app.stage_history[-1]["at"] == "2026-10-14T18:00:00Z"
        assert app.last_activity_at == NOW

    def test_forcing_an_active_stage_removes_a_terminal_entry(self):
        res = apply({"applications": [{"match": {"company": "Brightloom"}, "stage": "technical", "stage_at": "2026-10-13"}]})
        app = by_company(res)["Brightloom"]
        assert [h["stage"] for h in app.stage_history] == ["applied", "technical"] and not app.terminal

    def test_forcing_a_stage_strips_a_ghosted_entry(self):
        built, contacts, events = fresh()
        app = {a.company: a for a in built}["Quarry Systems"]
        appmod.apply_ghosting([app], [], datetime(2027, 1, 1, tzinfo=timezone.utc), 21)
        assert app.current_stage == "ghosted"
        ov = ovr.parse_overrides({"applications": [{"match": {"company": "Quarry Systems"}, "stage": "recruiter_screen", "stage_at": "2026-12-20"}]}, FIXTURES)
        res = ovr.apply_overrides(built, contacts, events, ov, datetime(2027, 1, 1, tzinfo=timezone.utc))
        got = {a.company: a for a in res.apps}["Quarry Systems"]
        assert [h["stage"] for h in got.stage_history] == ["applied", "recruiter_screen"]

    def test_stage_date_never_precedes_existing_history(self):
        res = apply({"applications": [{"match": {"company": "Acme Corp"}, "stage": "accepted", "stage_at": "2026-09-01"}]})
        app = by_company(res)["Acme Corp"]
        ats = [h["at"] for h in app.stage_history]
        assert ats == sorted(ats) and app.current_stage == "accepted"


# ---------------------------------------------------------------- manual applications

class TestManualApplications:
    DATA = {"applications": [{"company": "Stealth startup", "role": "Head of Data", "applied_at": date(2026, 10, 3),
                              "stage": "recruiter_screen", "stage_at": date(2026, 10, 7), "source": "recruiter", "notes": "Intro via a friend."}]}

    def test_created_with_manual_origin(self):
        app = by_company(apply(self.DATA))["Stealth startup"]
        assert app.origin == "manual" and app.source == "recruiter" and app.role == "Head of Data"
        assert app.id == application_id("Stealth startup", "Head of Data")
        assert app.message_ids == [] and app.contact_ids == [] and app.event_ids == []
        assert app.notes == "Intro via a friend."
        assert app.applied_at == "2026-10-03"

    def test_history_has_manual_applied_and_the_forced_stage(self):
        app = by_company(apply(self.DATA))["Stealth startup"]
        assert app.stage_history == [
            {"stage": "applied", "at": "2026-10-03T00:00:00Z", "message_id": None, "evidence": "manual"},
            {"stage": "recruiter_screen", "at": "2026-10-07T00:00:00Z", "message_id": None, "evidence": "override"},
        ]
        assert app.last_activity_at == datetime(2026, 10, 7, tzinfo=timezone.utc)

    def test_defaults(self):
        res = apply({"applications": [{"company": "Quiet Co", "role": "Analyst"}]})
        app = by_company(res)["Quiet Co"]
        assert app.source == "manual" and app.applied_at == "2026-10-14"
        assert [h["stage"] for h in app.stage_history] == ["applied"]

    def test_entry_without_match_that_equals_an_email_application_modifies_it(self):
        res = apply({"applications": [{"company": "Brightloom", "role": "Analytics Engineer", "notes": "Follow up in spring."}]})
        assert len(res.apps) == 9
        app = by_company(res)["Brightloom"]
        assert app.origin == "email" and app.notes == "Follow up in spring."

    def test_stage_equal_to_applied_is_not_duplicated(self):
        res = apply({"applications": [{"company": "Quiet Co", "stage": "applied", "applied_at": "2026-10-03"}]})
        assert len(by_company(res)["Quiet Co"].stage_history) == 1


# ---------------------------------------------------------------- descriptions and contacts

class TestDescriptions:
    def test_description_file_relative_to_overrides(self):
        res = apply({"applications": [{"match": {"company": "Acme Corp"}, "description_file": "job_text/acme-sde.md"}]})
        d = by_company(res)["Acme Corp"].description
        assert d["source"] == "manual" and "ingestion layer" in d["text"] and d["fetched_at"] == "2026-10-14T18:00:00Z"
        assert d["url"] is None

    def test_file_wins_over_inline_text(self):
        res = apply({"applications": [{"match": {"company": "Acme Corp"}, "description": "inline", "description_file": "job_text/acme-sde.md"}]})
        assert "ingestion layer" in by_company(res)["Acme Corp"].description["text"]

    def test_inline_description_and_url(self):
        res = apply({"applications": [{"match": {"company": "Acme Corp"}, "description": "Inline text.", "description_url": "https://acme.example/jobs/1"}]})
        assert by_company(res)["Acme Corp"].description == {"url": "https://acme.example/jobs/1", "text": "Inline text.", "source": "manual", "fetched_at": "2026-10-14T18:00:00Z"}

    def test_url_only_is_fetched_and_marked_manual(self):
        res = apply({"applications": [{"match": {"company": "Acme Corp"}, "description_url": "https://acme.example/jobs/1"}]},
                    fetch_text=lambda url: "Fetched text")
        d = by_company(res)["Acme Corp"].description
        assert d["source"] == "manual" and d["text"] == "Fetched text" and d["url"] == "https://acme.example/jobs/1"

    def test_missing_description_file_is_an_error(self):
        with pytest.raises(ovr.OverridesError):
            apply({"applications": [{"match": {"company": "Acme Corp"}, "description_file": "nope.md"}]})


class TestContacts:
    def test_enrich_existing_contact_by_email(self):
        res = apply({"applications": [{"match": {"company": "Acme Corp"}, "contacts": [{"name": "Jane Doe", "email": "JANE.DOE@acme.example", "title": "Senior Technical Recruiter"}]}]})
        jane = [c for c in res.contacts if c["email"] == "jane.doe@acme.example"]
        assert len(jane) == 1 and jane[0]["title"] == "Senior Technical Recruiter"

    def test_new_contact_is_added_and_linked(self):
        res = apply({"applications": [{"match": {"company": "Quarry Systems"}, "contacts": [{"name": "Rae Kim", "email": "rae@quarry.example", "title": "Hiring Manager"}]}]})
        rae = [c for c in res.contacts if c["email"] == "rae@quarry.example"][0]
        app = by_company(res)["Quarry Systems"]
        assert rae["application_ids"] == [app.id] and rae["company"] == "Quarry Systems" and rae["message_count"] == 0
        assert rae["id"] in app.contact_ids
        assert rae["first_seen"] == "2026-10-14T18:00:00Z"
        assert rae["id"].startswith("c_")


def test_full_sample_file_applies(tmp_path):
    built, contacts, events = fresh()
    ov = ovr.load_overrides(SAMPLE)
    res = ovr.apply_overrides(built, contacts, events, ov, NOW)
    companies = {a.company for a in res.apps}
    assert "Orbitwave" not in companies and "Harborlight Health" in companies and "Stealth startup" in companies
    assert by_company(res)["Quarry Systems"].current_stage == "rejected"
    assert by_company(res)["Acme Corp"].description["source"] == "manual"
