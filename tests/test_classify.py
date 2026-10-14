"""Message-level inference: relatedness, noise, company, role, stage signals, signatures."""
from __future__ import annotations

import email
import email.policy

import pytest

from scanner import classify, mail
from tests.helpers import load_eml

OWNER = "owner@example.com"


def parse(name: str, labels=None):
    return mail.parse_message(load_eml(name, labels=labels), OWNER)


def synthetic(subject: str, body: str, sender: str = "Pat Lee <pat@unknown-co.example>", direction_owner: bool = False, extra: str = ""):
    frm = f"Alex <{OWNER}>" if direction_owner else sender
    raw = (f"From: {frm}\nTo: Alex <{OWNER}>\nSubject: {subject}\nDate: Mon, 05 Oct 2026 12:00:00 +0000\n{extra}\n{body}")
    msg = email.message_from_string(raw, policy=email.policy.default)
    return mail.parse_message(mail.RawMessage("5", "5", [], msg), OWNER)


# ---------------------------------------------------------------- relatedness

JOB_FIXTURES = [
    "greenhouse_confirmation", "lever_confirmation", "ashby_confirmation", "workday_confirmation",
    "linkedin_application_sent", "indeed_viewed", "company_confirmation", "recruiter_intro",
    "calendar_invite", "takehome", "offer", "rejection", "sent_withdrawal",
]


@pytest.mark.parametrize("name", JOB_FIXTURES)
def test_job_emails_are_related(name):
    assert classify.is_job_related(parse(name)), name


@pytest.mark.parametrize("name", ["job_alert_digest", "profile_views"])
def test_noise_fixtures_are_excluded(name):
    msg = parse(name)
    assert classify.is_noise(msg)
    assert not classify.is_job_related(msg)


def test_unknown_human_domain_needs_two_signals():
    one = synthetic("Quick question", "We had an interview scheduled somewhere.")
    two = synthetic("Quick question", "Thanks for the interview. Our recruiter will send next steps.")
    assert not classify.is_job_related(one)
    assert classify.is_job_related(two)


def test_ordinary_personal_mail_is_not_job_related():
    assert not classify.is_job_related(synthetic("Dinner on Friday?", "Are we still on for dinner? Unfortunately I may be late."))


def test_known_ats_sender_alone_is_enough():
    msg = synthetic("Hello", "Welcome aboard.", sender="Someone <x@boards.greenhouse.io>")
    assert classify.is_job_related(msg)


def test_job_board_sender_needs_a_job_phrase():
    social = synthetic("Alex, you have a new connection", "Say hello to Sam.", sender="LinkedIn <invitations@linkedin.com>")
    activity = synthetic("Update", "Your application is under review.", sender="LinkedIn <jobs-noreply@linkedin.com>")
    assert not classify.is_job_related(social)
    assert classify.is_job_related(activity)


def test_jobs_label_forces_relatedness_and_ignore_label_drops():
    plain = synthetic("Lunch", "See you at noon.")
    assert not classify.is_job_related(plain)
    forced = mail.parse_message(mail.RawMessage("5", "5", ["jobs"], email.message_from_string(
        f"From: x@y.example\nTo: {OWNER}\nSubject: Lunch\nDate: Mon, 05 Oct 2026 12:00:00 +0000\n\nSee you at noon.", policy=email.policy.default)), OWNER)
    assert classify.is_job_related(forced)
    ignored = mail.parse_message(mail.RawMessage("5", "5", ["jobs/ignore"], email.message_from_string(
        f"From: x@y.example\nTo: {OWNER}\nSubject: Interview next steps\nDate: Mon, 05 Oct 2026 12:00:00 +0000\n\nrecruiter", policy=email.policy.default)), OWNER)
    assert classify.has_ignore_label(ignored)


# ---------------------------------------------------------------- noise

class TestNoise:
    @pytest.mark.parametrize("subject", [
        "5 jobs for you in Chicago", "Jobs you may be interested in", "New jobs matching Data Engineer",
        "8 new roles this week", "Job alert: Analytics Engineer", "Your daily digest", "Weekly digest",
        "Acme newsletter", "Who’s viewing your profile", "people are looking at your profile",
    ])
    def test_subject_patterns(self, subject):
        assert classify.is_noise(synthetic(subject, "body"))

    @pytest.mark.parametrize("sender", [
        "jobs-listings@linkedin.com", "alert@indeed.com", "noreply@glassdoor.com", "jobalerts-noreply@linkedin.com",
    ])
    def test_alert_senders(self, sender):
        assert classify.is_noise(synthetic("Hello", "body", sender=f"X <{sender}>"))

    def test_bulk_from_non_ats_domain(self):
        msg = synthetic("Our latest roles", "body", extra="List-Unsubscribe: <https://x.example/u>\nPrecedence: bulk\n")
        assert classify.is_noise(msg)

    def test_bulk_from_known_ats_is_kept(self):
        msg = synthetic("Thanks for applying", "body", sender="Acme <no-reply@us.greenhouse-mail.io>",
                        extra="List-Unsubscribe: <https://x.example/u>\nPrecedence: bulk\n")
        assert not classify.is_noise(msg)

    def test_note_to_self(self):
        msg = synthetic("Interview prep", "my recruiter notes", direction_owner=True)
        assert classify.is_noise(msg)

    def test_real_application_mail_from_a_board_is_not_noise(self):
        assert not classify.is_noise(parse("linkedin_application_sent"))
        assert not classify.is_noise(parse("indeed_viewed"))

    def test_override_rules(self):
        rules = classify.Rules(ignore_senders=["pat@unknown-co.example", "@blocked.example"],
                               ignore_subjects=[r"^Weekly digest"])
        assert classify.is_noise(synthetic("Hi", "b"), rules)
        assert classify.is_noise(synthetic("Hi", "b", sender="Z <z@blocked.example>"), rules)
        assert classify.is_noise(synthetic("Weekly digest of things", "b", sender="Q <q@other.example>"), rules)
        assert not classify.is_noise(synthetic("Hi", "b", sender="Q <q@other.example>"), rules)


# ---------------------------------------------------------------- company

COMPANY_CASES = [
    ("greenhouse_confirmation", "Acme Corp"),
    ("lever_confirmation", "Brightloom"),
    ("ashby_confirmation", "Fernhill Labs"),
    ("workday_confirmation", "Quarry Systems"),
    ("linkedin_application_sent", "Tallpine Data"),
    ("indeed_viewed", "Orbitwave"),
    ("company_confirmation", "Harborlight"),
    ("recruiter_intro", "Cobaltreef"),
    ("calendar_invite", "Acme Corp"),
    ("rejection", "Brightloom"),
    ("offer", "Acme Corp"),
    ("sent_withdrawal", "Fernhill Labs"),
    ("labelled_rejected", "Lumenridge"),
]


@pytest.mark.parametrize("name,expected", COMPANY_CASES)
def test_company_from_fixture(name, expected):
    name_found, _conf = classify.best_company([parse(name)], OWNER)
    assert name_found == expected


def test_company_from_signature_beats_domain():
    msg = synthetic("Quick hello", "Hi Alex,\n\nLet us schedule a phone screen.\n\nThanks,\nPat Lee\nTalent Acquisition, Globex Dynamics\n",
                    sender="Pat Lee <pat@gdx-mail.example>")
    assert classify.best_company([msg], OWNER)[0] == "Globex Dynamics"


def test_company_falls_back_to_domain_registrable_part():
    msg = synthetic("Chat?", "Hi Alex, quick chat about the interview process, from the recruiter.", sender="Jane <jane@mail.acmecorp.com>")
    assert classify.best_company([msg], OWNER)[0] == "Acmecorp"


def test_no_company_for_generic_mail_domain():
    msg = synthetic("Chat?", "Hi Alex, interview next steps from the recruiter.", sender="Jane <jane@gmail.com>")
    assert classify.best_company([msg], OWNER)[0] == ""


def test_explicit_company_beats_sender_name_and_domain():
    msg = synthetic("Your application to Zenith Works", "We received your application.",
                    sender="Careers <jobs@someother.example>")
    name, conf = classify.best_company([msg], OWNER)
    assert name == "Zenith Works" and conf >= 3


def test_company_after_a_job_title():
    msg = synthetic("Senior Data Engineer at Zenith Works - next steps", "Hello there.", sender="Pat <pat@gmail.com>")
    assert classify.best_company([msg], OWNER)[0] == "Zenith Works"


def test_role_title_is_not_mistaken_for_a_company():
    msg = synthetic("Update on your application for Senior Data Engineer", "Hello there.", sender="Pat <pat@zeta-labs.example>")
    assert classify.best_company([msg], OWNER)[0] == "Zeta Labs"


class TestNormalizeCompany:
    @pytest.mark.parametrize("raw,expected", [
        ("Acme Careers", "Acme"),
        ("Acme Recruiting Team", "Acme"),
        ("Acme Inc.", "Acme"),
        ("Acme, LLC", "Acme"),
        ("Acme Talent Acquisition", "Acme"),
        ("  Acme   Corp  ", "Acme Corp"),
        ("Acme Corp.", "Acme Corp"),
        ("IBM Hiring", "IBM"),
        ("NASA", "NASA"),
        ("ACMECORP", "Acmecorp"),
        ("fernhilllabs", "Fernhilllabs"),
    ])
    def test_cases(self, raw, expected):
        assert classify.normalize_company(raw) == expected

    def test_never_reduces_to_nothing(self):
        assert classify.normalize_company("Careers") == ""

    def test_key_ignores_legal_suffixes_and_punctuation(self):
        assert classify.company_key("Acme Corp") == classify.company_key("ACME, Inc.") == "acme"
        assert classify.company_key("Tall Pine Data") == "tallpinedata"


# ---------------------------------------------------------------- role

ROLE_CASES = [
    ("greenhouse_confirmation", "Senior Data Engineer"),
    ("lever_confirmation", "Analytics Engineer"),
    ("ashby_confirmation", "Analytics Engineer"),
    ("workday_confirmation", "Data Platform Engineer"),
    ("linkedin_application_sent", "Data Analyst"),
    ("indeed_viewed", "Data Analyst"),
    ("company_confirmation", "Analytics Engineer"),
    ("recruiter_intro", "Data Engineering Manager"),
    ("takehome", "Senior Data Engineer"),
    ("offer", "Senior Data Engineer"),
    ("sent_withdrawal", "Analytics Engineer"),
    ("calendar_invite", ""),
]


@pytest.mark.parametrize("name,expected", ROLE_CASES)
def test_role_from_fixture(name, expected):
    assert classify.best_role([parse(name)]) == expected


def test_requisition_ids_are_stripped():
    assert classify.clean_role("Senior Data Engineer (R12345)") == "Senior Data Engineer"
    assert classify.clean_role("Data Engineer - 2026-123") == "Data Engineer"
    assert classify.clean_role("Analytics Engineer Req #4411") == "Analytics Engineer"


def test_head_of_data_and_data_engineering_manager():
    assert classify.best_role([synthetic("Interview - Head of Data", "hello")]) == "Head of Data"
    assert classify.best_role([synthetic("Interview - Data Engineering Manager", "hello")]) == "Data Engineering Manager"


def test_role_patterns_in_body():
    assert classify.best_role([synthetic("Hello", "Regarding the position of Staff Analytics Engineer, we would like to talk.")]) == "Staff Analytics Engineer"
    assert classify.best_role([synthetic("Hello", "You applied for Lead BI Developer at Foo.")]) == "Lead BI Developer"


def test_unknown_role_is_empty():
    assert classify.best_role([synthetic("Hello", "Thanks for your time on the phone.")]) == ""


# ---------------------------------------------------------------- stage signals

STAGE_CASES = [
    ("greenhouse_confirmation", "applied"),
    ("lever_confirmation", "applied"),
    ("ashby_confirmation", "applied"),
    ("workday_confirmation", "applied"),
    ("linkedin_application_sent", "applied"),
    ("indeed_viewed", None),
    ("company_confirmation", "applied"),
    ("recruiter_intro", "recruiter_screen"),
    ("calendar_invite", "recruiter_screen"),
    ("takehome", "technical"),
    ("sent_reply", None),
    ("offer", "offer"),
    ("rejection", "rejected"),
    ("sent_withdrawal", "withdrew"),
]


@pytest.mark.parametrize("name,expected", STAGE_CASES)
def test_stage_signal_from_fixture(name, expected):
    sig = classify.stage_signal(parse(name))
    assert (sig.stage if sig else None) == expected


class TestStageRules:
    @pytest.mark.parametrize("body,stage", [
        ("We have decided to go with other candidates, not moving forward.", "rejected"),
        ("Pleased to offer you the role. Offer of employment attached.", "offer"),
        ("Join us for the virtual onsite and meet the team.", "onsite"),
        ("Here is the HackerRank link for the coding challenge.", "technical"),
        ("Next you will have a manager interview with the hiring manager.", "hiring_manager"),
        ("Do you have a 20-minute window? I'd love to chat.", "recruiter_screen"),
        ("Thank you for applying to this role.", "applied"),
        ("We have received your application.", "applied"),
    ])
    def test_inbound(self, body, stage):
        assert classify.stage_signal(synthetic("Re: update", body)).stage == stage

    def test_rule_order_rejected_beats_applied(self):
        msg = synthetic("Thank you for applying", "Unfortunately we are not moving forward.")
        assert classify.stage_signal(msg).stage == "rejected"

    def test_confirmation_boilerplate_does_not_look_like_a_screen(self):
        msg = synthetic("Thanks for applying to Foo", "If we would like to talk, we will schedule a time with you.", sender="Foo <no-reply@greenhouse.io>")
        assert classify.stage_signal(msg).stage == "applied"

    def test_outbound_only_accepts_or_withdraws(self):
        accept = synthetic("Re: offer", "I am happy to accept the offer.", direction_owner=True)
        assert classify.stage_signal(accept).stage == "accepted"
        withdraw = synthetic("Re: process", "Please remove my application, I am no longer interested.", direction_owner=True)
        assert classify.stage_signal(withdraw).stage == "withdrew"
        chatty = synthetic("Re: call", "I would love to chat, any availability for a quick call?", direction_owner=True)
        assert classify.stage_signal(chatty) is None

    def test_inbound_text_cannot_withdraw(self):
        assert classify.stage_signal(synthetic("Re", "Please withdraw my application.")) is None

    def test_calendar_invite_defaults_to_screen_and_follows_summary(self):
        ics = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20261008T150000Z\nSUMMARY:{s}\nEND:VEVENT\nEND:VCALENDAR\n"
        def invite(summary):
            raw = ("From: Pat <pat@x.example>\nTo: owner@example.com\nSubject: Invitation\nDate: Mon, 05 Oct 2026 12:00:00 +0000\n"
                   "MIME-Version: 1.0\nContent-Type: multipart/alternative; boundary=B\n\n--B\nContent-Type: text/plain\n\nSee invite.\n"
                   "--B\nContent-Type: text/calendar\n\n" + ics.format(s=summary) + "--B--\n")
            msg = email.message_from_string(raw, policy=email.policy.default)
            return mail.parse_message(mail.RawMessage("7", "7", [], msg), OWNER)
        assert classify.stage_signal(invite("Meeting with Pat")).stage == "recruiter_screen"
        assert classify.stage_signal(invite("Technical interview with Acme")).stage == "technical"
        assert classify.stage_signal(invite("Final round at Acme")).stage == "onsite"
        assert classify.stage_signal(invite("Meeting with Pat")).evidence == "calendar_invite"

    def test_label_beats_inference(self):
        msg = parse("labelled_rejected")
        sig = classify.stage_signal(msg)
        assert sig.stage == "rejected" and sig.evidence == "label"
        assert classify.stage_signal(parse("offer", labels=["jobs/screen"])).stage == "recruiter_screen"
        assert classify.stage_signal(parse("offer", labels=["Jobs/HM"])).stage == "hiring_manager"
        assert classify.stage_signal(parse("greenhouse_confirmation", labels=["jobs/onsite"])).stage == "onsite"

    def test_evidence_names(self):
        assert classify.stage_signal(parse("greenhouse_confirmation")).evidence == "confirmation"
        assert classify.stage_signal(parse("rejection")).evidence == "email"
        assert classify.stage_signal(parse("calendar_invite")).evidence == "calendar_invite"


# ---------------------------------------------------------------- people

class TestPeople:
    @pytest.mark.parametrize("address,person", [
        ("jane.doe@acme.example", True),
        ("no-reply@us.greenhouse-mail.io", False),
        ("noreply@x.example", False),
        ("notifications@calendly.com", False),
        ("careers@acme.example", False),
        ("recruiting@acme.example", False),
        ("talent@acme.example", False),
        ("hr@acme.example", False),
        ("support@acme.example", False),
        ("info@acme.example", False),
        ("calendar-notification@google.com", False),
        ("quarry@myworkday.com", False),
        ("owner@example.com", False),
    ])
    def test_is_person(self, address, person):
        assert classify.is_person(address, OWNER) is person

    def test_signature_with_title_and_company(self):
        sig = classify.parse_signature(parse("takehome").clean, "Jane Doe", "jane.doe@acme.example")
        assert (sig.name, sig.title, sig.company) == ("Jane Doe", "Technical Recruiter", "Acme Corp")

    def test_signature_three_lines(self):
        sig = classify.parse_signature(parse("recruiter_intro").clean, "Marcus Webb", "marcus.webb@cobaltreef.example")
        assert (sig.name, sig.title, sig.company) == ("Marcus Webb", "Senior Technical Recruiter", "Cobaltreef")

    def test_signature_must_start_with_the_contact_name(self):
        assert classify.parse_signature("Thanks,\nSomeone Else\nTechnical Recruiter\nFoo", "Jane Doe", "jane@x.example") is None

    def test_signature_title_must_look_like_a_title(self):
        sig = classify.parse_signature("Best,\nJane Doe\nSent from my phone", "Jane Doe", "jane@x.example")
        assert sig is not None and sig.title == ""

    def test_name_only_signature(self):
        sig = classify.parse_signature(parse("labelled_rejected").clean, "Sam Ortiz", "sam.ortiz@lumenridge.example")
        assert sig is not None and sig.name == "Sam" and sig.title == ""
