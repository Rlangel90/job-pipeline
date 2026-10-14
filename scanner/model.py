"""Plain data containers shared by the scanner modules, plus the small id and time helpers.

Keeping these in one place lets mail parsing, classification, overrides and output assembly
talk to each other without importing one another.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

STAGES = [
    ("applied", "Applied", "active"),
    ("recruiter_screen", "Recruiter screen", "active"),
    ("hiring_manager", "Hiring manager", "active"),
    ("technical", "Technical", "active"),
    ("onsite", "Onsite / panel", "active"),
    ("offer", "Offer", "active"),
    ("accepted", "Accepted", "terminal"),
    ("rejected", "Rejected", "terminal"),
    ("withdrew", "Withdrew", "terminal"),
    ("ghosted", "Ghosted", "terminal"),
]
ACTIVE_ORDER = {sid: i for i, (sid, _, kind) in enumerate(STAGES) if kind == "active"}
TERMINAL = {sid for sid, _, kind in STAGES if kind == "terminal"}


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def iso_ts(dt: datetime) -> str:
    """2026-10-14T18:00:00Z, always UTC, no fractional seconds."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def date_to_dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def norm_key(text: str) -> str:
    """Lowercase alphanumerics only: the comparison key for company and role names."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


@dataclass
class Party:
    name: str
    email: str

    def to_dict(self) -> dict:
        return {"name": self.name, "email": self.email}


@dataclass
class CalendarEvent:
    start: datetime
    end: datetime | None
    summary: str
    attendees: list[Party] = field(default_factory=list)
    organizer: Party | None = None


@dataclass
class ParsedMessage:
    gm_msgid: str
    gm_thrid: str
    id: str
    thread_id: str
    date: datetime
    direction: str  # "in" | "out"
    sender: Party
    to: list[Party]
    cc: list[Party]
    subject: str
    text: str  # full plain text, capped
    clean: str  # text without quoted replies, used for classification
    snippet: str
    labels: list[str]  # only the labels starting with "jobs"
    gmail_url: str
    events: list[CalendarEvent] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    list_unsubscribe: bool = False
    bulk: bool = False


@dataclass
class Application:
    """One job application in progress while the pipeline is being assembled."""
    id: str
    company: str
    role: str
    source: str
    applied_at: str  # YYYY-MM-DD
    stage_history: list[dict]
    message_ids: list[str] = field(default_factory=list)
    thread_ids: list[str] = field(default_factory=list)
    contact_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    description: dict | None = None
    notes: str = ""
    last_activity_at: datetime | None = None
    origin: str = "email"
    urls: list[str] = field(default_factory=list)
    message_dates: dict = field(default_factory=dict)  # message id -> datetime, for ordering after merges

    @property
    def current_stage(self) -> str:
        return self.stage_history[-1]["stage"]

    @property
    def terminal(self) -> bool:
        return self.current_stage in TERMINAL

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "company": self.company,
            "role": self.role,
            "source": self.source,
            "applied_at": self.applied_at,
            "current_stage": self.current_stage,
            "terminal": self.terminal,
            "stage_history": self.stage_history,
            "contact_ids": sorted(self.contact_ids),
            "message_ids": self.message_ids,
            "event_ids": self.event_ids,
            "description": self.description,
            "notes": self.notes,
            "last_activity_at": iso_ts(self.last_activity_at),
            "origin": self.origin,
        }


def application_id(company: str, role: str) -> str:
    """Stable id: sha1(company_key|role_key)[:6], with an empty role key when the role is unknown."""
    return "app_" + sha1_hex(f"{norm_key(company)}|{norm_key(role)}")[:6]
