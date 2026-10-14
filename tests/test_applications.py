"""Turning classified threads into applications: merging, stage history, ghosting, contacts, events."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from scanner import applications as apps
from scanner import classify, mail
from scanner.classify import StageSignal
from scanner.model import Application, application_id, iso_ts
from tests.helpers import ALL_FIXTURES, load_eml

OWNER = "owner@example.com"
NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)


def dt(day: int, hour: int = 12, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, 0, 0, tzinfo=timezone.utc)


def sig(stage: str, evidence: str = "email") -> StageSignal:
    return StageSignal(stage, evidence)


def stages(history):
    return [h["stage"] for h in history]


# ---------------------------------------------------------------- stage history

class TestStageHistory:
    def test_walks_forward_through_stages(self):
        hist, last = apps.build_history(
            [(dt(2), "m1", sig("applied", "confirmation")), (dt(5), "m2", sig("recruiter_screen")), (dt(9), "m3", sig("technical"))],
            earliest=dt(2))
        assert stages(hist) == ["applied", "recruiter_screen", "technical"]
        assert hist[0] == {"stage": "applied", "at": "2026-10-02T12:00:00Z", "message_id": "m1", "evidence": "confirmation"}
        assert last == dt(9)

    def test_stage_may_skip_ahead(self):
        hist, _ = apps.build_history([(dt(2), "m1", sig("applied")), (dt(5), "m2", sig("technical"))], earliest=dt(2))
        assert stages(hist) == ["applied", "technical"]

    def test_never_moves_backwards(self):
        hist, _ = apps.build_history(
            [(dt(2), "m1", sig("applied")), (dt(5), "m2", sig("technical")), (dt(6), "m3", sig("applied")), (dt(7), "m4", sig("recruiter_screen"))],
            earliest=dt(2))
        assert stages(hist) == ["applied", "technical"]

    def test_repeated_stage_is_not_duplicated(self):
        hist, _ = apps.build_history([(dt(2), "m1", sig("applied")), (dt(3), "m2", sig("applied"))], earliest=dt(2))
        assert stages(hist) == ["applied"]

    @pytest.mark.parametrize("terminal", ["rejected", "withdrew", "accepted"])
    def test_terminal_ends_the_history(self, terminal):
        hist, last = apps.build_history(
            [(dt(2), "m1", sig("applied")), (dt(5), "m2", sig("offer")), (dt(6), "m3", sig(terminal)), (dt(8), "m4", sig("technical")),
             (dt(9), "m5", None)],
            earliest=dt(2))
        assert stages(hist) == ["applied", "offer", terminal]
        assert last == dt(9)  # later chatter still counts as activity

    def test_terminal_allowed_from_any_stage(self):
        hist, _ = apps.build_history([(dt(2), "m1", sig("technical")), (dt(5), "m2", sig("rejected"))], earliest=dt(2))
        assert stages(hist) == ["applied", "technical", "rejected"]

    def test_applied_is_synthesized_when_first_signal_is_later_stage(self):
        hist, _ = apps.build_history([(dt(6), "m2", sig("recruiter_screen"))], earliest=dt(4))
        assert hist[0] == {"stage": "applied", "at": "2026-10-04T12:00:00Z", "message_id": None, "evidence": "inferred"}
        assert stages(hist) == ["applied", "recruiter_screen"]

    def test_applied_is_synthesized_when_there_are_no_signals(self):
        hist, last = apps.build_history([(dt(4), "m1", None), (dt(6), "m2", None)], earliest=dt(4))
        assert hist == [{"stage": "applied", "at": "2026-10-04T12:00:00Z", "message_id": None, "evidence": "inferred"}]
        assert last == dt(6)

    def test_message_without_signal_updates_only_last_activity(self):
        hist, last = apps.build_history([(dt(2), "m1", sig("applied")), (dt(10), "m2", None)], earliest=dt(2))
        assert stages(hist) == ["applied"] and last == dt(10)

    def test_history_is_ascending(self):
        hist, _ = apps.build_history([(dt(9), "m3", sig("technical")), (dt(2), "m1", sig("applied")), (dt(5), "m2", sig("recruiter_screen"))], earliest=dt(2))
        assert [h["at"] for h in hist] == sorted(h["at"] for h in hist)


# ---------------------------------------------------------------- ghosting

def make_app(last: datetime, stage_list=("applied",), app_id="app_aaaaaa") -> Application:
    history = [{"stage": s, "at": iso_ts(last), "message_id": None, "evidence": "email"} for s in stage_list]
    return Application(id=app_id, company="Acme", role="", source="other", applied_at=last.date().isoformat(),
                       stage_history=history, last_activity_at=last)


class TestGhosting:
    def test_old_open_application_gets_ghosted_entry(self):
        app = make_app(datetime(2026, 9, 20, 12, tzinfo=timezone.utc))
        apps.apply_ghosting([app], [], NOW, 21)
        assert app.current_stage == "ghosted" and app.terminal
        last = app.stage_history[-1]
        assert last == {"stage": "ghosted", "at": "2026-10-11T12:00:00Z", "message_id": None, "evidence": "timeout"}

    def test_boundary_is_strictly_older_than_the_window(self):
        exactly = make_app(NOW - timedelta(days=21))
        just_over = make_app(NOW - timedelta(days=21, seconds=1), app_id="app_bbbbbb")
        apps.apply_ghosting([exactly, just_over], [], NOW, 21)
        assert not exactly.terminal
        assert just_over.current_stage == "ghosted"

    def test_recent_application_is_left_alone(self):
        app = make_app(dt(1))
        apps.apply_ghosting([app], [], NOW, 21)
        assert app.current_stage == "applied"

    def test_terminal_application_is_never_ghosted(self):
        app = make_app(datetime(2026, 8, 1, tzinfo=timezone.utc), ("applied", "rejected"))
        apps.apply_ghosting([app], [], NOW, 21)
        assert app.current_stage == "rejected" and len(app.stage_history) == 2

    def test_future_event_prevents_ghosting(self):
        app = make_app(datetime(2026, 9, 1, tzinfo=timezone.utc))
        apps.apply_ghosting([app], [{"application_id": app.id, "at": "2026-10-20T15:00:00Z"}], NOW, 21)
        assert not app.terminal

    def test_past_event_does_not_prevent_ghosting(self):
        app = make_app(datetime(2026, 9, 1, tzinfo=timezone.utc))
        apps.apply_ghosting([app], [{"application_id": app.id, "at": "2026-09-05T15:00:00Z"}], NOW, 21)
        assert app.current_stage == "ghosted"

    def test_custom_window(self):
        app = make_app(dt(1))
        apps.apply_ghosting([app], [], NOW, 10)
        assert app.current_stage == "ghosted"
        assert app.stage_history[-1]["at"] == "2026-10-11T12:00:00Z"


# ---------------------------------------------------------------- merging

def result(thread_id: str, company: str, role: str, day: int, stage: str | None = "applied") -> apps.ThreadResult:
    raw = (f"From: Pat <pat@{company.lower().replace(' ', '')}.example>\nTo: owner@example.com\nSubject: {company} {thread_id}\n"
           f"Date: Mon, {day:02d} Oct 2026 12:00:00 +0000\n\nbody")
    import email
    import email.policy
    msg = mail.parse_message(mail.RawMessage(thread_id + "m", thread_id, [], email.message_from_string(raw, policy=email.policy.default)), OWNER)
    sigs = {msg.id: StageSignal(stage, "email") if stage else None}
    return apps.ThreadResult(thread_id=msg.thread_id, messages=[msg], company=company, company_conf=3.0, role=role, role_conf=3.0, signals=sigs)


class TestMerging:
    def test_same_company_and_same_role_merge(self):
        out = apps.build_applications([result("a", "Acme Corp", "Data Engineer", 2), result("b", "ACME Inc.", "Data Engineer", 5, "recruiter_screen")], OWNER)
        assert len(out) == 1
        assert stages(out[0].stage_history) == ["applied", "recruiter_screen"]
        assert len(out[0].message_ids) == 2

    def test_loose_role_overlap_merges(self):
        out = apps.build_applications([result("a", "Acme", "Senior Data Engineer", 2), result("b", "Acme", "Data Engineer", 5)], OWNER)
        assert len(out) == 1

    def test_blank_role_merges_and_adopts_the_known_role(self):
        out = apps.build_applications([result("a", "Acme", "", 2), result("b", "Acme", "Analytics Engineer", 5)], OWNER)
        assert len(out) == 1 and out[0].role == "Analytics Engineer"
        assert out[0].id == application_id("Acme", "Analytics Engineer")

    def test_different_roles_merge_while_only_one_application_is_open(self):
        out = apps.build_applications([result("a", "Acme", "Data Engineer", 2), result("b", "Acme", "Product Manager", 5)], OWNER)
        assert len(out) == 1

    def test_finished_application_does_not_absorb_a_new_one(self):
        out = apps.build_applications(
            [result("a", "Acme", "Data Engineer", 2, "rejected"), result("b", "Acme", "Product Manager", 6)], OWNER)
        assert len(out) == 2

    def test_two_open_applications_keep_different_roles_apart(self):
        out = apps.build_applications([
            result("a", "Acme", "Data Engineer", 2, "rejected"),
            result("b", "Acme", "Product Manager", 3),
            result("c", "Acme", "Product Manager", 4),
            result("d", "Acme", "Machine Learning Scientist", 5),
        ], OWNER)
        assert {a.role for a in out} == {"Data Engineer", "Product Manager"}  # d joined the single open PM application

    def test_different_companies_never_merge(self):
        out = apps.build_applications([result("a", "Acme", "Data Engineer", 2), result("b", "Globex", "Data Engineer", 2)], OWNER)
        assert len(out) == 2

    def test_application_id_is_stable_sha1_prefix(self):
        expected = "app_" + hashlib.sha1(b"acme|dataengineer").hexdigest()[:6]
        assert application_id("Acme", "Data Engineer") == expected
        assert application_id("Acme", "") == "app_" + hashlib.sha1(b"acme|").hexdigest()[:6]

    def test_role_overlap_function(self):
        assert apps.roles_compatible("Senior Data Engineer", "Data Engineer")
        assert apps.roles_compatible("", "Data Engineer")
        assert not apps.roles_compatible("Data Engineer", "Data Scientist")

    def test_merge_applications_combines_histories(self):
        a = make_app(dt(2), ("applied",), "app_aaaaaa")
        a.message_ids, a.message_dates = ["m1"], {"m1": dt(2)}
        b = make_app(dt(6), ("recruiter_screen",), "app_bbbbbb")
        b.message_ids, b.message_dates = ["m2"], {"m2": dt(6)}
        merged = apps.merge_applications(a, b)
        assert stages(merged.stage_history) == ["applied", "recruiter_screen"]
        assert merged.message_ids == ["m1", "m2"]
        assert merged.last_activity_at == dt(6)


# ---------------------------------------------------------------- end to end over the fixture mailbox

@pytest.fixture(scope="module")
def pipeline():
    parsed = [mail.parse_message(load_eml(name), OWNER) for name in ALL_FIXTURES]
    results = apps.analyse_threads(parsed, None, OWNER)
    built = apps.build_applications(results, OWNER)
    by_company = {a.company: a for a in built}
    messages = {m.id: m for m in parsed}
    return parsed, results, built, by_company, messages


def test_noise_threads_are_dropped(pipeline):
    parsed, results, built, by_company, _ = pipeline
    assert len(built) == 9
    assert "Foo Industries" not in by_company
    subjects = {m.subject for r in results for m in r.messages}
    assert not any("new jobs" in s or "viewing your profile" in s for s in subjects)


def test_acme_threads_merge_into_one_progressing_application(pipeline):
    _, _, _, by_company, _ = pipeline
    acme = by_company["Acme Corp"]
    assert acme.role == "Senior Data Engineer"
    assert acme.id == application_id("Acme Corp", "Senior Data Engineer")
    assert acme.source == "greenhouse"
    assert stages(acme.stage_history) == ["applied", "recruiter_screen", "technical", "offer"]
    assert acme.stage_history[0]["evidence"] == "confirmation"
    assert acme.stage_history[1]["evidence"] == "calendar_invite"
    assert acme.current_stage == "offer" and not acme.terminal
    assert acme.applied_at == "2026-10-02"
    assert len(acme.message_ids) == 5  # confirmation, invite, take-home, reply, offer
    assert acme.last_activity_at == datetime(2026, 10, 13, 19, 0, tzinfo=timezone.utc)


def test_brightloom_is_rejected(pipeline):
    app = pipeline[3]["Brightloom"]
    assert stages(app.stage_history) == ["applied", "rejected"] and app.terminal
    assert app.source == "lever" and app.role == "Analytics Engineer"


def test_fernhill_withdrew_by_the_owner(pipeline):
    app = pipeline[3]["Fernhill Labs"]
    assert stages(app.stage_history) == ["applied", "withdrew"]
    assert app.source == "ashby"


def test_sources(pipeline):
    by_company = pipeline[3]
    got = {c: a.source for c, a in by_company.items()}
    assert got == {
        "Acme Corp": "greenhouse", "Brightloom": "lever", "Fernhill Labs": "ashby", "Quarry Systems": "workday",
        "Tallpine Data": "linkedin", "Orbitwave": "indeed", "Harborlight": "company_site", "Cobaltreef": "recruiter",
        "Lumenridge": "other",
    }


def test_recruiter_outreach_synthesizes_applied(pipeline):
    app = pipeline[3]["Cobaltreef"]
    assert stages(app.stage_history) == ["applied", "recruiter_screen"]
    assert app.stage_history[0]["evidence"] == "inferred" and app.stage_history[0]["message_id"] is None


def test_viewed_email_without_stage_is_inferred_applied(pipeline):
    app = pipeline[3]["Orbitwave"]
    assert stages(app.stage_history) == ["applied"] and app.stage_history[0]["evidence"] == "inferred"


def test_gmail_label_overrides_inference(pipeline):
    app = pipeline[3]["Lumenridge"]
    assert app.current_stage == "rejected"
    assert app.stage_history[-1]["evidence"] == "label"


def test_unchanged_open_applications(pipeline):
    by_company = pipeline[3]
    for name in ("Quarry Systems", "Tallpine Data", "Harborlight"):
        assert stages(by_company[name].stage_history) == ["applied"]


# ---------------------------------------------------------------- contacts and events

def test_contacts(pipeline):
    _, _, built, by_company, messages = pipeline
    contacts = apps.build_contacts(built, messages, OWNER)
    by_email = {c["email"]: c for c in contacts}
    assert set(by_email) == {"jane.doe@acme.example", "marcus.webb@cobaltreef.example", "sam.ortiz@lumenridge.example"}
    jane = by_email["jane.doe@acme.example"]
    assert jane["id"] == "c_" + hashlib.sha1(b"jane.doe@acme.example").hexdigest()[:8]
    assert jane["name"] == "Jane Doe"
    assert jane["title"] == "Technical Recruiter"
    assert jane["company"] == "Acme Corp"
    assert jane["message_count"] == 4
    assert jane["first_seen"] == "2026-10-05T20:00:00Z" and jane["last_seen"] == "2026-10-13T19:00:00Z"
    assert jane["application_ids"] == [by_company["Acme Corp"].id]
    assert by_email["marcus.webb@cobaltreef.example"]["title"] == "Senior Technical Recruiter"
    assert by_email["sam.ortiz@lumenridge.example"]["title"] == ""
    assert by_company["Acme Corp"].contact_ids == [jane["id"]]
    assert contacts == sorted(contacts, key=lambda c: c["last_seen"], reverse=True)


def test_events_from_calendar_invites(pipeline):
    _, _, built, by_company, messages = pipeline
    events = apps.build_events(built, messages, OWNER)
    assert len(events) == 1
    ev = events[0]
    assert ev["id"] == "e_01"
    assert ev["application_id"] == by_company["Acme Corp"].id
    assert ev["at"] == "2026-10-06T15:00:00Z" and ev["end_at"] == "2026-10-06T15:30:00Z"
    assert ev["title"] == "Phone screen: Acme Corp"
    assert ev["stage"] == "recruiter_screen"
    assert [a["email"] for a in ev["attendees"]] == ["jane.doe@acme.example"]
    assert by_company["Acme Corp"].event_ids == ["e_01"]


def test_calendar_only_thread_without_company_is_dropped():
    raw = ("From: calendar-notification@google.com\nTo: owner@example.com\nSubject: Invitation: Sync @ Tue\nDate: Mon, 05 Oct 2026 12:00:00 +0000\n"
           "MIME-Version: 1.0\nContent-Type: text/calendar; charset=utf-8\n\nBEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20261008T150000Z\nSUMMARY:Sync\nEND:VEVENT\nEND:VCALENDAR\n")
    import email
    import email.policy
    msg = mail.parse_message(mail.RawMessage("1", "1", ["jobs"], email.message_from_string(raw, policy=email.policy.default)), OWNER)
    assert apps.analyse_threads([msg], None, OWNER) == []


def test_ignore_label_drops_whole_thread():
    a = mail.parse_message(load_eml("offer"), OWNER)
    b = mail.parse_message(load_eml("offer", labels=["jobs/ignore"]), OWNER)
    b.gm_thrid = a.gm_thrid
    b.thread_id = a.thread_id
    b.id = "m_other"
    assert apps.analyse_threads([a, b], None, OWNER) == []
