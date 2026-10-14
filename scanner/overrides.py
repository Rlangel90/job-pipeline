"""Hand-written corrections applied after the classifier has had its say.

The owner keeps overrides.yml in a private repo. Overrides win over Gmail labels, which win over
inference. Errors in the file are fatal on purpose: silently dropping a correction would publish
a snapshot that disagrees with what the owner wrote down.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from . import classify
from .applications import merge_applications, roles_compatible
from .classify import Rules
from .model import STAGES, TERMINAL, Application, application_id, iso_ts, parse_ts, sha1_hex

STAGE_IDS = {s for s, _, _ in STAGES}
SOURCES = {"linkedin", "indeed", "greenhouse", "lever", "workday", "ashby", "icims", "smartrecruiters", "recruiter",
           "company_site", "manual", "other"}


class OverridesError(ValueError):
    """The overrides file is missing, unreadable, or contains something we cannot honor."""


@dataclass
class Overrides:
    applications: list[dict] = field(default_factory=list)
    ignore_senders: list[str] = field(default_factory=list)
    ignore_subjects_matching: list[str] = field(default_factory=list)
    ghost_after_days: int = 21
    base_dir: Path = Path(".")

    def rules(self) -> Rules:
        return Rules(ignore_senders=list(self.ignore_senders), ignore_subjects=list(self.ignore_subjects_matching))


@dataclass
class OverrideResult:
    apps: list[Application]
    contacts: list[dict]
    events: list[dict]
    stats: dict


def _string_list(value, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise OverridesError(f"{name} must be a list of strings")
    return value


def parse_overrides(data, base_dir: Path) -> Overrides:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise OverridesError("top level must be a mapping")
    apps = data.get("applications") or []
    if not isinstance(apps, list) or not all(isinstance(a, dict) for a in apps):
        raise OverridesError("applications must be a list of mappings")
    for entry in apps:
        if "stage" in entry and entry["stage"] not in STAGE_IDS:
            raise OverridesError("unknown stage in an application entry")
        if "source" in entry and entry["source"] not in SOURCES:
            raise OverridesError("unknown source in an application entry")
    subjects = _string_list(data.get("ignore_subjects_matching"), "ignore_subjects_matching")
    for pattern in subjects:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise OverridesError("ignore_subjects_matching has an invalid pattern") from exc
    days = data.get("ghost_after_days", 21)
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise OverridesError("ghost_after_days must be a positive integer")
    return Overrides(applications=apps, ignore_senders=_string_list(data.get("ignore_senders"), "ignore_senders"),
                     ignore_subjects_matching=subjects, ghost_after_days=days, base_dir=Path(base_dir))


def load_overrides(path: str | Path | None) -> Overrides:
    if path is None:
        return Overrides()
    path = Path(path)
    if not path.is_file():
        raise OverridesError("overrides file not found")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
        raise OverridesError("overrides file could not be read as YAML") from exc
    return parse_overrides(data, path.parent)


# ---------------------------------------------------------------- helpers

def _to_dt(value, default: datetime) -> datetime:
    if value is None or value == "":
        return default
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    try:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise OverridesError("a date in the overrides file is not ISO formatted") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _matches(app: Application, match: dict) -> bool:
    if match.get("id"):
        return app.id == match["id"]
    company = match.get("company")
    if not company or classify.company_key(app.company) != classify.company_key(company):
        return False
    role = match.get("role")
    return not role or roles_compatible(app.role, role)


def _force_stage(app: Application, stage: str, when: datetime) -> None:
    history = [h for h in app.stage_history if h["stage"] not in TERMINAL]  # a correction replaces any ghosted/terminal ending
    at = when
    if history:
        at = max(when, parse_ts(history[-1]["at"]))
    history.append({"stage": stage, "at": iso_ts(at), "message_id": None, "evidence": "override"})
    app.stage_history = history
    app.applied_at = history[0]["at"][:10]
    app.last_activity_at = max(app.last_activity_at, when)


def _read_description(entry: dict, ov: Overrides, now: datetime, fetch_text: Callable[[str], str | None] | None) -> dict | None:
    url = entry.get("description_url")
    text = None
    if entry.get("description_file"):
        path = (ov.base_dir / str(entry["description_file"])).resolve()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise OverridesError("description_file could not be read") from exc
    elif entry.get("description"):
        text = str(entry["description"])
    elif url and fetch_text:
        text = fetch_text(str(url))
    if text is None and not url:
        return None
    return {"url": str(url) if url else None, "text": (text or "").strip(), "source": "manual", "fetched_at": iso_ts(now)}


def _apply_contacts(app: Application, entry: dict, contacts: list[dict], now: datetime) -> None:
    for raw in entry.get("contacts") or []:
        address = str(raw.get("email", "")).strip().lower()
        if not address:
            continue
        existing = next((c for c in contacts if c["email"].lower() == address), None)
        if existing is None:
            existing = {"id": "c_" + sha1_hex(address)[:8], "name": "", "email": address, "title": "", "company": app.company,
                        "first_seen": iso_ts(now), "last_seen": iso_ts(now), "message_count": 0, "application_ids": []}
            contacts.append(existing)
        for key in ("name", "title", "company"):
            if raw.get(key):
                existing[key] = str(raw[key])
        if not existing["name"]:
            existing["name"] = address.split("@")[0]
        if app.id not in existing["application_ids"]:
            existing["application_ids"] = sorted(existing["application_ids"] + [app.id])
        if existing["id"] not in app.contact_ids:
            app.contact_ids = sorted(app.contact_ids + [existing["id"]])


def _remap(contacts: list[dict], events: list[dict], old: str, new: str) -> None:
    for c in contacts:
        c["application_ids"] = sorted({new if a == old else a for a in c["application_ids"]})
    for e in events:
        if e["application_id"] == old:
            e["application_id"] = new


def _new_manual(entry: dict, now: datetime) -> Application:
    company = str(entry.get("company", "")).strip()
    if not company:
        raise OverridesError("a manual application needs a company")
    role = str(entry.get("role", "") or "").strip()
    applied = _to_dt(entry.get("applied_at"), datetime(now.year, now.month, now.day, tzinfo=timezone.utc))
    history = [{"stage": "applied", "at": iso_ts(applied), "message_id": None, "evidence": "manual"}]
    app = Application(id=application_id(company, role), company=company, role=role, source=entry.get("source", "manual"),
                      applied_at=applied.date().isoformat(), stage_history=history, last_activity_at=applied, origin="manual")
    stage = entry.get("stage")
    if stage and stage != "applied":
        _force_stage(app, stage, _to_dt(entry.get("stage_at"), now))
    return app


# ---------------------------------------------------------------- main entry point

def apply_overrides(apps: list[Application], contacts: list[dict], events: list[dict], ov: Overrides, now: datetime,
                    fetch_text: Callable[[str], str | None] | None = None) -> OverrideResult:
    apps = list(apps)
    contacts = [dict(c) for c in contacts]
    events = [dict(e) for e in events]
    stats = {"applied": 0, "unmatched": 0, "manual": 0, "ignored": 0}
    removed: set[str] = set()

    for entry in ov.applications:
        match = entry.get("match")
        if match is None:
            # No `match`: an application the owner is declaring. If email already found it, treat as a correction.
            twin = application_id(str(entry.get("company", "")), str(entry.get("role", "") or ""))
            targets = [a for a in apps if a.id == twin]
            if not targets:
                if entry.get("ignore"):
                    continue
                app = _new_manual(entry, now)
                apps.append(app)
                app.notes = str(entry.get("notes", "") or "")
                app.description = _read_description(entry, ov, now, fetch_text)
                _apply_contacts(app, entry, contacts, now)
                stats["manual"] += 1
                continue
        else:
            targets = [a for a in apps if _matches(a, match)]
            if not targets:
                stats["unmatched"] += 1
                continue

        for app in targets:
            stats["applied"] += 1
            if entry.get("ignore"):
                apps.remove(app)
                removed.add(app.id)
                stats["ignored"] += 1
                continue
            if match is not None and ("company" in entry or "role" in entry):
                old_id = app.id
                app.company = str(entry.get("company") or app.company)
                app.role = str(entry["role"]) if "role" in entry and entry["role"] is not None else app.role
                app.id = application_id(app.company, app.role)
                clash = next((a for a in apps if a is not app and a.id == app.id), None)
                if clash is not None:
                    merge_applications(clash, app)
                    clash.company, clash.role = app.company, app.role
                    apps.remove(app)
                    app = clash
                if old_id != app.id:
                    _remap(contacts, events, old_id, app.id)
            if entry.get("stage"):
                _force_stage(app, entry["stage"], _to_dt(entry.get("stage_at"), now))
            if "notes" in entry:
                app.notes = str(entry["notes"] or "")
            if entry.get("source"):
                app.source = entry["source"]
            description = _read_description(entry, ov, now, fetch_text)
            if description is not None:
                app.description = description
            _apply_contacts(app, entry, contacts, now)

    live = {a.id for a in apps}
    events = [e for e in events if e["application_id"] in live]
    for c in contacts:
        c["application_ids"] = [a for a in c["application_ids"] if a in live]
    contacts = [c for c in contacts if c["application_ids"]]
    contacts.sort(key=lambda c: (c["last_seen"], c["email"]), reverse=True)
    for app in apps:
        app.event_ids = [e["id"] for e in events if e["application_id"] == app.id]
    return OverrideResult(apps=apps, contacts=contacts, events=events, stats=stats)
