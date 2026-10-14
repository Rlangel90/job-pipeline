"""Mail parsing: Gmail attribute lists, bodies, calendar invites, directions, mbox export."""
from __future__ import annotations

import email
import email.policy
import hashlib
import logging
import mailbox
from datetime import date, datetime, timezone

from scanner import mail
from tests.helpers import load_eml, write_mbox

OWNER = "owner@example.com"


def _msg(text: str):
    return email.message_from_string(text, policy=email.policy.default)


# ---------------------------------------------------------------- IMAP attribute parsing

class TestFetchAttributeParser:
    def test_plain_attributes(self):
        header = b'12 (UID 12 X-GM-MSGID 1778011223344556677 X-GM-THRID 1778011223344550000 X-GM-LABELS () BODY[] {2048}'
        attrs = mail.parse_fetch_attrs(header)
        assert attrs.msgid == "1778011223344556677"
        assert attrs.thrid == "1778011223344550000"
        assert attrs.labels == []
        assert attrs.uid == "12"

    def test_quoted_labels_with_spaces_and_escapes(self):
        header = (b'3 (X-GM-MSGID 11 X-GM-THRID 22 '
                  b'X-GM-LABELS ("\\\\Inbox" "jobs/applied" "\\\\Important" "My Label" "say \\"hi\\"") BODY[] {10}')
        attrs = mail.parse_fetch_attrs(header)
        assert attrs.labels == ["\\Inbox", "jobs/applied", "\\Important", "My Label", 'say "hi"']

    def test_unquoted_system_labels(self):
        header = b'3 (X-GM-MSGID 11 X-GM-THRID 22 X-GM-LABELS (\\Inbox \\Sent "jobs/screen") BODY[] {10}'
        assert mail.parse_fetch_attrs(header).labels == ["\\Inbox", "\\Sent", "jobs/screen"]

    def test_single_backslash_inside_quotes_is_kept(self):
        header = b'3 (X-GM-MSGID 11 X-GM-THRID 22 X-GM-LABELS ("\\Sent") BODY[] {10}'
        assert mail.parse_fetch_attrs(header).labels == ["\\Sent"]

    def test_attributes_in_any_order_and_trailing_part(self):
        head = b'7 (BODY[] {300}'
        tail = b' X-GM-LABELS ("jobs/hm") X-GM-THRID 999 X-GM-MSGID 888)'
        attrs = mail.parse_fetch_attrs(head + tail)
        assert (attrs.msgid, attrs.thrid, attrs.labels) == ("888", "999", ["jobs/hm"])

    def test_missing_attributes_are_empty(self):
        attrs = mail.parse_fetch_attrs(b"9 (UID 9 BODY[] {5}")
        assert attrs.msgid == "" and attrs.thrid == "" and attrs.labels == []

    def test_parse_fetch_response_pairs_headers_with_literals(self):
        data = [
            (b'1 (UID 1 X-GM-MSGID 100 X-GM-THRID 200 X-GM-LABELS ("\\\\Inbox" "jobs") BODY[] {5}', b"raw-1"),
            b")",
            (b'2 (UID 2 X-GM-MSGID 101 X-GM-THRID 200 BODY[] {5}', b"raw-2"),
            b' X-GM-LABELS ("\\\\Sent"))',
        ]
        out = mail.parse_fetch_response(data)
        assert [(a.msgid, a.thrid, a.labels, raw) for a, raw in out] == [
            ("100", "200", ["\\Inbox", "jobs"], b"raw-1"),
            ("101", "200", ["\\Sent"], b"raw-2"),
        ]

    def test_label_filter_keeps_jobs_and_sent_only(self):
        kept = mail.keep_labels(["\\Inbox", "Jobs/Applied", "\\Sent", "jobs", "Jobsite", "other", "\\Important"])
        assert kept == ["Jobs/Applied", "\\Sent", "jobs", "Jobsite"]

    def test_labels_from_takeout_header(self):
        assert mail.labels_from_header("Inbox,Sent,jobs/applied,Important") == ["\\Sent", "jobs/applied"]


class _FakeImap:
    """Just enough of imaplib.IMAP4_SSL to exercise fetch_imap without a network."""
    instances: list = []

    def __init__(self, host, port=993):
        self.host, self.port = host, port
        self.calls = []
        _FakeImap.instances.append(self)

    def login(self, user, pw):
        self.calls.append(("login", user))
        return "OK", [b"ok"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"2"]

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "SEARCH":
            return "OK", [b"1 2"]
        raw1 = b"From: a@x.example\r\nSubject: one\r\nDate: Fri, 02 Oct 2026 14:03:00 +0000\r\n\r\nhello one"
        raw2 = b"From: owner@example.com\r\nSubject: two\r\nDate: Sat, 03 Oct 2026 14:03:00 +0000\r\n\r\nhello two"
        return "OK", [
            (b'1 (UID 1 X-GM-MSGID 11 X-GM-THRID 90 X-GM-LABELS ("\\\\Inbox" "jobs/applied") BODY[] {10}', raw1),
            b")",
            (b'2 (UID 2 X-GM-MSGID 12 X-GM-THRID 90 X-GM-LABELS ("\\\\Sent") BODY[] {10}', raw2),
            b")",
        ]

    def logout(self):
        self.calls.append(("logout",))


def test_fetch_imap_searches_all_mail_readonly_and_decodes(monkeypatch):
    _FakeImap.instances.clear()
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", _FakeImap)
    msgs = mail.fetch_imap(OWNER, "app-password", date(2026, 10, 1), logging.getLogger("t"))
    conn = _FakeImap.instances[0]
    assert (conn.host, conn.port) == ("imap.gmail.com", 993)
    assert ("select", '"[Gmail]/All Mail"', True) in conn.calls
    search = [c for c in conn.calls if c[0] == "SEARCH"][0]
    assert search[1] == ("X-GM-RAW", '"after:2026/09/30"')
    assert [m.gm_msgid for m in msgs] == ["11", "12"]
    assert msgs[0].labels == ["jobs/applied"]
    assert msgs[1].labels == ["\\Sent"]
    assert msgs[0].message["Subject"] == "one"
    assert ("logout",) in conn.calls


# ---------------------------------------------------------------- bodies

class TestBodies:
    def test_plain_text_preferred(self):
        text = mail.message_text(load_eml("greenhouse_confirmation").message)
        assert "received your application" in text

    def test_html_only_is_converted_to_text(self):
        msg = _msg(
            "From: a@x.example\nSubject: s\nContent-Type: text/html; charset=utf-8\n\n"
            "<html><style>p{color:red}</style><body><p>Hello <b>there</b></p><p>Second</p>"
            "<br><br><br><br><p>Third</p></body></html>")
        text = mail.message_text(msg)
        assert "Hello" in text and "there" in text and "Second" in text
        assert "color:red" not in text
        assert "\n\n\n" not in text

    def test_quoted_reply_is_stripped_for_snippet_but_kept_in_body(self):
        parsed = mail.parse_message(load_eml("sent_reply"), OWNER)
        assert "wrote:" not in parsed.clean
        assert "take-home" not in parsed.clean
        assert "Monday works for me" in parsed.snippet
        assert "wrote:" in parsed.text

    def test_strip_quoted_markers(self):
        assert mail.strip_quoted("Hi\n\nOn Mon, Oct 5, 2026 at 9:00 AM Jane Doe <j@x.example>\nwrote:\n> older") == "Hi"
        assert mail.strip_quoted("Hi\n-----Original Message-----\nFrom: a\nold") == "Hi"
        assert mail.strip_quoted("Reply here\nFrom: Jane <j@x.example>\nSent: Monday\nold stuff") == "Reply here"
        assert mail.strip_quoted("Line one\n> quoted\nLine two") == "Line one\nLine two"

    def test_snippet_is_collapsed_and_capped(self):
        long = "word  \n\n" * 200
        parsed = mail.parse_message(mail.RawMessage("1", "1", [], _msg(f"From: a@x.example\nSubject: s\n\n{long}")), OWNER)
        assert len(parsed.snippet) <= 280
        assert "  " not in parsed.snippet and "\n" not in parsed.snippet

    def test_body_is_capped(self):
        msg = _msg("From: a@x.example\nSubject: s\n\n" + ("x" * 30000))
        parsed = mail.parse_message(mail.RawMessage("1", "1", [], msg), OWNER)
        assert len(parsed.text) == 20000


# ---------------------------------------------------------------- calendar

ICS_UTC = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261008T150000Z
DTEND:20261008T153000Z
SUMMARY:Interview with Acme
ORGANIZER;CN=Pat Lee:mailto:pat@acme.example
ATTENDEE;CN=Alex:mailto:owner@example.com
END:VEVENT
END:VCALENDAR
"""


class TestCalendar:
    def test_utc_event(self):
        ev = mail.parse_ics(ICS_UTC)[0]
        assert ev.start == datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)
        assert ev.end == datetime(2026, 10, 8, 15, 30, tzinfo=timezone.utc)
        assert ev.summary == "Interview with Acme"
        assert ev.organizer.email == "pat@acme.example" and ev.organizer.name == "Pat Lee"
        assert [a.email for a in ev.attendees] == ["owner@example.com"]

    def test_tzid_event_converts_to_utc_with_dst(self):
        text = "BEGIN:VEVENT\nDTSTART;TZID=America/Chicago:20261008T100000\nSUMMARY:x\nEND:VEVENT\n"
        assert mail.parse_ics(text)[0].start == datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc)
        winter = "BEGIN:VEVENT\nDTSTART;TZID=America/Chicago:20261208T100000\nSUMMARY:x\nEND:VEVENT\n"
        assert mail.parse_ics(winter)[0].start == datetime(2026, 12, 8, 16, 0, tzinfo=timezone.utc)

    def test_dst_boundaries_for_fallback_zones(self):
        # 2026: DST starts Mar 8, ends Nov 1.
        before = "BEGIN:VEVENT\nDTSTART;TZID=America/New_York:20261101T001500\nSUMMARY:x\nEND:VEVENT\n"
        after = "BEGIN:VEVENT\nDTSTART;TZID=America/New_York:20261102T001500\nSUMMARY:x\nEND:VEVENT\n"
        assert mail.parse_ics(before)[0].start == datetime(2026, 11, 1, 4, 15, tzinfo=timezone.utc)
        assert mail.parse_ics(after)[0].start == datetime(2026, 11, 2, 5, 15, tzinfo=timezone.utc)

    def test_line_folding(self):
        text = "BEGIN:VEVENT\nDTSTART:20261008T150000Z\nSUMMARY:A very long sum\n mary that was folded\nEND:VEVENT\n"
        assert mail.parse_ics(text)[0].summary == "A very long summary that was folded"

    def test_date_only_and_floating(self):
        text = "BEGIN:VEVENT\nDTSTART;VALUE=DATE:20261008\nSUMMARY:x\nEND:VEVENT\n"
        assert mail.parse_ics(text)[0].start == datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)
        floating = "BEGIN:VEVENT\nDTSTART:20261008T093000\nSUMMARY:x\nEND:VEVENT\n"
        assert mail.parse_ics(floating)[0].start == datetime(2026, 10, 8, 9, 30, tzinfo=timezone.utc)

    def test_escaped_text_is_unescaped(self):
        text = "BEGIN:VEVENT\nDTSTART:20261008T150000Z\nSUMMARY:Chat\\, intro\\; ok\nEND:VEVENT\n"
        assert mail.parse_ics(text)[0].summary == "Chat, intro; ok"

    def test_invite_email_fixture(self):
        parsed = mail.parse_message(load_eml("calendar_invite"), OWNER)
        assert len(parsed.events) == 1
        ev = parsed.events[0]
        assert ev.start == datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
        assert ev.end == datetime(2026, 10, 6, 15, 30, tzinfo=timezone.utc)
        assert ev.summary == "Phone screen: Acme Corp"
        assert {a.email for a in ev.attendees} == {"owner@example.com", "jane.doe@acme.example"}
        assert any(a.name == "Doe, Jane" for a in ev.attendees)

    def test_ics_attachment_by_filename(self):
        raw = ("From: a@x.example\nSubject: s\nMIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=B\n\n"
               "--B\nContent-Type: text/plain\n\nhello\n--B\nContent-Type: application/octet-stream\n"
               "Content-Disposition: attachment; filename=invite.ics\n\n" + ICS_UTC + "--B--\n")
        parsed = mail.parse_message(mail.RawMessage("1", "1", [], _msg(raw)), OWNER)
        assert len(parsed.events) == 1


# ---------------------------------------------------------------- direction, ids, urls

class TestMessageFields:
    def test_direction_by_from_address(self):
        assert mail.parse_message(load_eml("sent_withdrawal", labels=[]), OWNER).direction == "out"
        assert mail.parse_message(load_eml("offer"), OWNER).direction == "in"

    def test_direction_by_sent_label(self):
        raw = load_eml("offer", labels=["\\Sent"])
        assert mail.parse_message(raw, OWNER).direction == "out"

    def test_owner_address_is_case_insensitive(self):
        assert mail.parse_message(load_eml("sent_reply", labels=[]), "Owner@Example.COM").direction == "out"

    def test_gmail_url_uses_hex_of_msgid(self):
        raw = load_eml("offer")
        parsed = mail.parse_message(raw, OWNER)
        assert parsed.gmail_url == "https://mail.google.com/mail/u/0/#all/" + format(int(raw.gm_msgid), "x")

    def test_hashed_ids(self):
        raw = load_eml("offer")
        parsed = mail.parse_message(raw, OWNER)
        assert parsed.id == "m_" + hashlib.sha1(raw.gm_msgid.encode()).hexdigest()[:8]
        assert parsed.thread_id == "t_" + hashlib.sha1(raw.gm_thrid.encode()).hexdigest()[:8]

    def test_date_is_utc(self):
        assert mail.parse_message(load_eml("offer"), OWNER).date == datetime(2026, 10, 13, 19, 0, tzinfo=timezone.utc)

    def test_non_jobs_labels_are_dropped_from_output_labels(self):
        raw = load_eml("offer", labels=["\\Sent", "jobs/offer", "Inbox"])
        assert mail.parse_message(raw, OWNER).labels == ["jobs/offer"]

    def test_parties_and_bulk_flags(self):
        parsed = mail.parse_message(load_eml("job_alert_digest"), OWNER)
        assert parsed.sender.email == "jobalerts-noreply@linkedin.com"
        assert parsed.sender.name == "LinkedIn Job Alerts"
        assert parsed.to[0].email == OWNER
        assert parsed.list_unsubscribe and parsed.bulk

    def test_urls_extracted_from_text(self):
        parsed = mail.parse_message(load_eml("greenhouse_confirmation"), OWNER)
        assert "https://boards.greenhouse.io/acme/jobs/4012345" in parsed.urls

    def test_urls_extracted_from_html_links(self):
        msg = _msg('From: a@x.example\nSubject: s\nContent-Type: text/html; charset=utf-8\n\n'
                   '<p><a href="https://jobs.lever.co/acme/6f1c2d3e-aaaa-4bbb-8ccc-0123456789ab">Posting</a></p>')
        parsed = mail.parse_message(mail.RawMessage("1", "1", [], msg), OWNER)
        assert "https://jobs.lever.co/acme/6f1c2d3e-aaaa-4bbb-8ccc-0123456789ab" in parsed.urls


# ---------------------------------------------------------------- mbox

class TestMbox:
    def test_reads_ids_labels_and_filters_by_date(self, tmp_path):
        path = tmp_path / "x.mbox"
        write_mbox(path, ["greenhouse_confirmation", "offer", "sent_reply", "labelled_rejected"])
        msgs = mail.read_mbox(path, date(2026, 10, 9))
        by_id = {m.gm_msgid: m for m in msgs}
        assert set(by_id) == {"1000000000000001004", "1000000000000001103"}  # Oct 2 and Oct 7 fall before the window
        assert by_id["1000000000000001103"].labels == ["\\Sent"]
        assert by_id["1000000000000001103"].gm_thrid == "2000000000000001003"

    def test_labels_header_keeps_jobs_labels(self, tmp_path):
        path = tmp_path / "x.mbox"
        write_mbox(path, ["labelled_rejected"])
        msgs = mail.read_mbox(path, date(2026, 10, 1))
        assert msgs[0].labels == ["jobs/rejected"]

    def test_without_gmail_headers_threads_by_references(self, tmp_path):
        path = tmp_path / "plain.mbox"
        box = mailbox.mbox(str(path))
        box.add(mailbox.mboxMessage("From: a@x.example\nTo: owner@example.com\nSubject: Hi\nDate: Fri, 02 Oct 2026 14:03:00 +0000\nMessage-ID: <a@x>\n\nfirst"))
        box.add(mailbox.mboxMessage("From: owner@example.com\nTo: a@x.example\nSubject: Re: Hi\nDate: Fri, 02 Oct 2026 15:03:00 +0000\nMessage-ID: <b@x>\nIn-Reply-To: <a@x>\nReferences: <a@x>\n\nreply"))
        box.add(mailbox.mboxMessage("From: z@x.example\nTo: owner@example.com\nSubject: Other\nDate: Fri, 02 Oct 2026 16:03:00 +0000\nMessage-ID: <c@x>\n\nother"))
        box.flush()
        box.close()
        msgs = mail.read_mbox(path, date(2026, 10, 1))
        assert len(msgs) == 3
        by_subject = {m.message["Subject"]: m for m in msgs}
        assert by_subject["Hi"].gm_thrid == by_subject["Re: Hi"].gm_thrid
        assert by_subject["Other"].gm_thrid != by_subject["Hi"].gm_thrid
        assert by_subject["Hi"].gm_msgid != by_subject["Re: Hi"].gm_msgid
        assert by_subject["Hi"].gm_msgid.isdigit()
