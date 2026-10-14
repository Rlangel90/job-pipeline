"""Fixture loading helpers shared by the test modules."""
from __future__ import annotations

import email
import email.policy
import mailbox
from pathlib import Path

from scanner import mail
from scanner.mail import RawMessage

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_eml(name: str, labels: list[str] | None = None) -> RawMessage:
    """Load tests/fixtures/<name>.eml as a RawMessage, taking Gmail ids and labels from its headers."""
    raw = (FIXTURES / f"{name}.eml").read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    gm_msgid = str(msg["X-GM-MSGID"] or "1")
    gm_thrid = str(msg["X-GM-THRID"] or gm_msgid)
    found = mail.labels_from_header(str(msg["X-Gmail-Labels"] or ""))
    return RawMessage(gm_msgid=gm_msgid, gm_thrid=gm_thrid, labels=labels if labels is not None else found, message=msg)


def load_many(*names: str) -> list[RawMessage]:
    return [load_eml(n) for n in names]


ALL_FIXTURES = [
    "greenhouse_confirmation", "lever_confirmation", "ashby_confirmation", "workday_confirmation",
    "linkedin_application_sent", "indeed_viewed", "company_confirmation", "recruiter_intro",
    "calendar_invite", "takehome", "sent_reply", "offer", "rejection", "sent_withdrawal",
    "job_alert_digest", "profile_views", "labelled_rejected",
]


def write_mbox(path: Path, names: list[str]) -> None:
    box = mailbox.mbox(str(path))
    try:
        for name in names:
            raw = (FIXTURES / f"{name}.eml").read_bytes()
            box.add(mailbox.mboxMessage(email.message_from_bytes(raw)))
        box.flush()
    finally:
        box.close()
