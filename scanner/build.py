"""Putting a scan together: run the pipeline, assemble the output document, validate it.

`run_scan` is the whole mailbox-to-JSON path with every outside dependency (mail source, HTTP
session, clock) passed in, so it can be tested end to end without a network.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import jsonschema

from . import applications as appmod
from . import classify, descriptions, mail, overrides as ovrmod
from .mail import RawMessage
from .model import STAGES, TERMINAL, Application, ParsedMessage, iso_ts, parse_ts

SCHEMA_PATH = Path(__file__).with_name("schema.json")
_log = logging.getLogger(__name__)


class ValidationFailure(ValueError):
    """The assembled document broke the schema or an invariant; `paths` lists where (never the values)."""

    def __init__(self, paths: list[str]):
        super().__init__("; ".join(paths[:10]))
        self.paths = paths


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _path(parts) -> str:
    return "/" + "/".join(str(p) for p in parts)


def validate(data: dict) -> list[str]:
    """Schema errors plus the cross-reference and stage-history rules the site relies on. Paths only."""
    paths: list[str] = []
    validator = jsonschema.Draft202012Validator(load_schema())
    for err in sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path]):
        paths.append(_path(err.absolute_path) if err.absolute_path else "/")
    if paths:
        return paths
    contact_ids = {c["id"] for c in data["contacts"]}
    message_ids = {m["id"] for m in data["messages"]}
    event_ids = {e["id"] for e in data["events"]}
    app_ids = {a["id"] for a in data["applications"]}
    if len(app_ids) != len(data["applications"]):
        paths.append("/applications")
    for i, app in enumerate(data["applications"]):
        for field, valid in (("contact_ids", contact_ids), ("message_ids", message_ids), ("event_ids", event_ids)):
            for j, ref in enumerate(app[field]):
                if ref not in valid:
                    paths.append(f"/applications/{i}/{field}/{j}")
        history = app["stage_history"]
        for j, entry in enumerate(history):
            if entry["message_id"] is not None and entry["message_id"] not in message_ids:
                paths.append(f"/applications/{i}/stage_history/{j}/message_id")
        if [h["at"] for h in history] != sorted(h["at"] for h in history):
            paths.append(f"/applications/{i}/stage_history")
        terminals = [j for j, h in enumerate(history) if h["stage"] in TERMINAL]
        if terminals and terminals != [len(history) - 1]:
            paths.append(f"/applications/{i}/stage_history")
        if app["current_stage"] != history[-1]["stage"] or app["terminal"] != (history[-1]["stage"] in TERMINAL):
            paths.append(f"/applications/{i}/current_stage")
    for i, c in enumerate(data["contacts"]):
        for j, ref in enumerate(c["application_ids"]):
            if ref not in app_ids:
                paths.append(f"/contacts/{i}/application_ids/{j}")
    for i, e in enumerate(data["events"]):
        if e["application_id"] not in app_ids:
            paths.append(f"/events/{i}/application_id")
        if e["message_id"] is not None and e["message_id"] not in message_ids:
            paths.append(f"/events/{i}/message_id")
    return paths


def check(data: dict) -> None:
    paths = validate(data)
    if paths:
        raise ValidationFailure(paths)


def compute_stats(apps: list[dict]) -> dict:
    total = len(apps)
    responded = sum(1 for a in apps if any(h["stage"] not in ("applied", "ghosted") for h in a["stage_history"]))
    return {
        "applications": total,
        "active": sum(1 for a in apps if not a["terminal"]),
        "offers": sum(1 for a in apps if any(h["stage"] in ("offer", "accepted") for h in a["stage_history"])),
        "rejected": sum(1 for a in apps if a["current_stage"] == "rejected"),
        "ghosted": sum(1 for a in apps if a["current_stage"] == "ghosted"),
        "response_rate": round(responded / total, 3) if total else 0.0,
    }


def stages_block() -> list[dict]:
    return [{"id": s, "label": label, "kind": kind} for s, label, kind in STAGES]


def message_dict(msg: ParsedMessage) -> dict:
    signal = classify.stage_signal(msg)
    return {
        "id": msg.id, "thread_id": msg.thread_id, "date": iso_ts(msg.date), "direction": msg.direction,
        "from": msg.sender.to_dict(), "to": [p.to_dict() for p in msg.to], "subject": msg.subject,
        "snippet": msg.snippet, "body": msg.text, "gmail_url": msg.gmail_url,
        "stage_signal": signal.stage if signal else None, "labels": list(msg.labels),
    }


def assemble(apps: list[Application], contacts: list[dict], events: list[dict], messages_by_id: dict[str, ParsedMessage],
             now: datetime, since: date) -> dict:
    app_dicts = sorted((a.to_dict() for a in apps), key=lambda a: (a["last_activity_at"], a["id"]), reverse=True)
    wanted = {mid for a in apps for mid in a.message_ids}
    messages = sorted((messages_by_id[i] for i in wanted if i in messages_by_id), key=lambda m: (m.date, m.id))
    return {
        "version": 1,
        "generated_at": iso_ts(now),
        "scan_window_start": since.isoformat(),
        "stages": stages_block(),
        "applications": app_dicts,
        "contacts": sorted(contacts, key=lambda c: (c["last_seen"], c["email"]), reverse=True),
        "messages": [message_dict(m) for m in messages],
        "events": sorted(events, key=lambda e: (e["at"], e["id"])),
        "stats": compute_stats(app_dicts),
    }


def previous_descriptions(previous: dict | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for app in (previous or {}).get("applications", []) or []:
        desc = app.get("description")
        if desc and (desc.get("text") or "").strip():
            out[app["id"]] = desc
    return out


def _carry_first_seen(contacts: list[dict], previous: dict | None) -> None:
    old = {c["id"]: c["first_seen"] for c in (previous or {}).get("contacts", []) or []}
    for c in contacts:
        if c["id"] in old and old[c["id"]] < c["first_seen"]:
            c["first_seen"] = old[c["id"]]


@dataclass
class ScanResult:
    data: dict
    counts: dict


def run_scan(raws: list[RawMessage], owner: str, since: date, ov: ovrmod.Overrides, previous: dict | None, now: datetime,
             session=None, logger: logging.Logger | None = None) -> ScanResult:
    logger = logger or _log
    unique: dict[str, RawMessage] = {}
    for raw in raws:
        unique.setdefault(raw.gm_msgid, raw)
    parsed: list[ParsedMessage] = []
    unparsable = 0
    for raw in unique.values():
        try:
            parsed.append(mail.parse_message(raw, owner))
        except Exception:  # noqa: BLE001 - one malformed message must not sink the scan; only the count is logged
            unparsable += 1
    if unparsable:
        logger.info("messages that could not be parsed: %d", unparsable)

    threads = appmod.analyse_threads(parsed, ov.rules(), owner)
    apps = appmod.build_applications(threads, owner)
    messages_by_id = {m.id: m for t in threads for m in t.messages}
    contacts = appmod.build_contacts(apps, messages_by_id, owner)
    events = appmod.build_events(apps, messages_by_id, owner)

    fetcher = descriptions.DescriptionFetcher(session=session, logger=logger, now=now)
    result = ovrmod.apply_overrides(apps, contacts, events, ov, now, fetch_text=fetcher.fetch_text)
    apps, contacts, events = result.apps, result.contacts, result.events
    desc_counts = descriptions.collect(apps, previous_descriptions(previous), fetcher)
    appmod.apply_ghosting(apps, events, now, ov.ghost_after_days)
    _carry_first_seen(contacts, previous)

    data = assemble(apps, contacts, events, messages_by_id, now, since)
    counts = {
        "messages_fetched": len(unique), "messages_unparsable": unparsable, "candidate_threads": len(threads), "applications": len(apps),
        "contacts": len(contacts), "descriptions_fetched": desc_counts["fetched"],
        "descriptions_reused": desc_counts["reused"], "descriptions_failed": desc_counts["failed"],
        "overrides_unmatched": result.stats["unmatched"],
    }
    return ScanResult(data=data, counts=counts)


def parse_since(text: str) -> date:
    return date.fromisoformat(text)


def parse_now(text: str) -> datetime:
    return parse_ts(text)
