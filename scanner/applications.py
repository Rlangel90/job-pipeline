"""From classified messages to applications: thread analysis, merging, stage history, ghosting.

Stage history only moves forward through the pipeline, a terminal stage closes it, and an
application nobody has answered for a while is marked ghosted. All of it is deterministic given
the messages and a pinned `now`, which is what makes it testable.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import classify
from .classify import Rules, StageSignal
from .model import (ACTIVE_ORDER, TERMINAL, Application, ParsedMessage, application_id, iso_ts, parse_ts, sha1_hex)

UNKNOWN_COMPANY = "Unknown company"


@dataclass
class ThreadResult:
    """One Gmail thread after noise removal, with what the classifier read out of it."""
    thread_id: str
    messages: list[ParsedMessage]
    company: str
    company_conf: float
    role: str
    role_conf: float
    signals: dict  # message id -> StageSignal | None


# ---------------------------------------------------------------- thread analysis

def analyse_threads(parsed: list[ParsedMessage], rules: Rules | None, owner: str) -> list[ThreadResult]:
    by_thread: dict[str, list[ParsedMessage]] = defaultdict(list)
    for msg in parsed:
        by_thread[msg.thread_id].append(msg)
    results: list[ThreadResult] = []
    for thread_id, msgs in by_thread.items():
        msgs = sorted(msgs, key=lambda m: (m.date, m.id))
        if any(classify.has_ignore_label(m) for m in msgs):
            continue
        kept = [m for m in msgs if not classify.is_noise(m, rules)]
        related = [m for m in kept if classify.is_job_related(m, rules, owner)]
        if not related:
            continue
        company, company_conf = classify.best_company(kept, owner)
        if not company and not any(not m.events for m in related):
            continue  # calendar chatter with no company to attach it to
        role, role_conf = classify.best_role_with_conf(kept)
        results.append(ThreadResult(
            thread_id=thread_id, messages=kept, company=company or UNKNOWN_COMPANY, company_conf=company_conf,
            role=role, role_conf=role_conf, signals={m.id: classify.stage_signal(m) for m in kept}))
    results.sort(key=lambda r: (r.messages[0].date, r.thread_id))
    return results


# ---------------------------------------------------------------- stage history

def _entry(stage: str, at: datetime, message_id: str | None, evidence: str) -> dict:
    return {"stage": stage, "at": iso_ts(at), "message_id": message_id, "evidence": evidence}


def build_history(items: list[tuple[datetime, str, StageSignal | None]], earliest: datetime) -> tuple[list[dict], datetime]:
    """Walk messages in date order and build the stage history.

    Active stages only advance, a terminal stage ends the history (later messages still count as
    activity), and an `applied` entry is invented at the earliest message when the first thing we
    saw was already further along.
    """
    history: list[dict] = []
    rank = -1
    ended = False
    last = earliest
    for when, message_id, signal in sorted(items, key=lambda it: (it[0], it[1])):
        last = max(last, when)
        if signal is None or ended:
            continue
        stage = signal.stage
        if stage in TERMINAL:
            if not history:
                history.append(_entry("applied", earliest, None, "inferred"))
            history.append(_entry(stage, when, message_id, signal.evidence))
            ended = True
        elif ACTIVE_ORDER[stage] > rank:
            if not history and stage != "applied":
                history.append(_entry("applied", earliest, None, "inferred"))
            history.append(_entry(stage, when, message_id, signal.evidence))
            rank = ACTIVE_ORDER[stage]
    if not history:
        history.append(_entry("applied", earliest, None, "inferred"))
    return history, last


def normalize_history(entries: list[dict]) -> list[dict]:
    """Re-run the ordering rules over entries from several sources (used when applications merge)."""
    items = [(parse_ts(e["at"]), e["message_id"] or "", StageSignal(e["stage"], e["evidence"])) for e in entries]
    earliest = min(w for w, _, _ in items)
    history, _ = build_history(items, earliest)
    for entry in history:
        if entry["message_id"] == "":
            entry["message_id"] = None
    return history


# ---------------------------------------------------------------- ghosting

def apply_ghosting(apps: list[Application], events: list[dict], now: datetime, ghost_after_days: int) -> None:
    """Close out applications that have gone quiet, unless something is still on the calendar."""
    future = {e["application_id"] for e in events if parse_ts(e["at"]) > now}
    window = timedelta(days=ghost_after_days)
    for app in apps:
        if app.terminal or app.id in future or app.last_activity_at is None:
            continue
        if now - app.last_activity_at > window:
            app.stage_history.append(_entry("ghosted", app.last_activity_at + window, None, "timeout"))


# ---------------------------------------------------------------- merging

def _role_tokens(role: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", role.lower()))


def roles_compatible(a: str, b: str) -> bool:
    """Blank roles match anything; otherwise token overlap (Jaccard) of at least one half."""
    if not a or not b:
        return True
    ta, tb = _role_tokens(a), _role_tokens(b)
    if not ta or not tb:
        return True
    return len(ta & tb) / len(ta | tb) >= 0.5


@dataclass
class _Group:
    results: list[ThreadResult]
    role: str
    role_conf: float

    @property
    def terminal_seen(self) -> bool:
        return any(s and s.stage in TERMINAL for r in self.results for s in r.signals.values())

    def add(self, r: ThreadResult) -> None:
        self.results.append(r)
        if r.role and (not self.role or r.role_conf > self.role_conf):
            self.role, self.role_conf = r.role, r.role_conf


def _company_name(results: list[ThreadResult]) -> str:
    best = max(r.company_conf for r in results)
    forms = Counter(r.company for r in results if r.company_conf == best)
    return max(forms, key=lambda f: (forms[f], len(f)))


def _finalize(group: _Group, owner: str) -> Application:
    results = sorted(group.results, key=lambda r: r.messages[0].date)
    messages = sorted((m for r in results for m in r.messages), key=lambda m: (m.date, m.id))
    signals = {}
    for r in results:
        signals.update(r.signals)
    earliest = messages[0].date
    history, last = build_history([(m.date, m.id, signals[m.id]) for m in messages], earliest)
    company = _company_name(results)
    role = group.role
    applied_confirmed = any(s and s.stage == "applied" and s.evidence == "confirmation" for s in signals.values())
    first_in = next((m for m in messages if m.direction == "in"), None)
    recruiter_first = False
    if first_in and classify.is_person(first_in.sender.email, owner):
        sig = classify.parse_signature(first_in.clean, first_in.sender.name, first_in.sender.email)
        recruiter_first = bool(sig and re.search(r"recruit|talent|sourc", sig.title, re.I))
    urls: list[str] = []
    for m in messages:
        for u in m.urls:
            if u not in urls:
                urls.append(u)
    return Application(
        id=application_id(company, role), company=company, role=role,
        source=classify.detect_source(messages, applied_confirmed, recruiter_first),
        applied_at=history[0]["at"][:10], stage_history=history,
        message_ids=[m.id for m in messages], thread_ids=sorted({r.thread_id for r in results}),
        last_activity_at=last, origin="email", urls=urls, message_dates={m.id: m.date for m in messages},
    )


def merge_applications(a: Application, b: Application) -> Application:
    """Fold b into a (same real application seen twice). Histories are re-validated, ids unioned."""
    dates = {**b.message_dates, **a.message_dates}
    ids = sorted(set(a.message_ids) | set(b.message_ids), key=lambda i: (dates.get(i, datetime.min.replace(tzinfo=a.last_activity_at.tzinfo)), i))
    a.message_ids = ids
    a.message_dates = dates
    a.stage_history = normalize_history(a.stage_history + b.stage_history)
    a.applied_at = a.stage_history[0]["at"][:10]
    a.last_activity_at = max(a.last_activity_at, b.last_activity_at)
    a.thread_ids = sorted(set(a.thread_ids) | set(b.thread_ids))
    a.contact_ids = sorted(set(a.contact_ids) | set(b.contact_ids))
    a.event_ids = sorted(set(a.event_ids) | set(b.event_ids))
    a.urls = list(dict.fromkeys(a.urls + b.urls))
    if not a.role and b.role:
        a.role = b.role
    if a.source in ("other", "manual") and b.source not in ("other", "manual"):
        a.source = b.source
    if a.description is None:
        a.description = b.description
    if b.notes and b.notes not in a.notes:
        a.notes = (a.notes + "\n" + b.notes).strip()
    if b.origin == "email":
        a.origin = "email"
    return a


def build_applications(results: list[ThreadResult], owner: str) -> list[Application]:
    by_company: dict[str, list[ThreadResult]] = defaultdict(list)
    for r in sorted(results, key=lambda r: (r.messages[0].date, r.thread_id)):
        key = classify.company_key(r.company) if r.company != UNKNOWN_COMPANY else "unknown:" + r.thread_id
        by_company[key].append(r)

    apps: list[Application] = []
    for rs in by_company.values():
        groups: list[_Group] = []
        for r in rs:
            compatible = [g for g in groups if roles_compatible(g.role, r.role)]
            compatible.sort(key=lambda g: g.terminal_seen)  # prefer a still-open application
            target = compatible[0] if compatible else None
            if target is None:
                open_groups = [g for g in groups if not g.terminal_seen]
                if len(open_groups) == 1:
                    target = open_groups[0]
            if target is None:
                groups.append(_Group([r], r.role, r.role_conf))
            else:
                target.add(r)
        apps.extend(_finalize(g, owner) for g in groups)

    unique: dict[str, Application] = {}
    for app in apps:
        if app.id in unique:
            merge_applications(unique[app.id], app)
        else:
            unique[app.id] = app
    return sorted(unique.values(), key=lambda a: (a.applied_at, a.id))


# ---------------------------------------------------------------- contacts and events

def _fallback_name(address: str) -> str:
    local = re.split(r"[+]", address.split("@")[0])[0]
    return " ".join(w.capitalize() for w in re.split(r"[._\-]+", local) if w)


def build_contacts(apps: list[Application], messages_by_id: dict[str, ParsedMessage], owner: str) -> list[dict]:
    people: dict[str, dict] = {}
    by_recency = sorted(apps, key=lambda a: a.last_activity_at)
    for app in by_recency:
        for mid in app.message_ids:
            msg = messages_by_id.get(mid)
            if msg is None:
                continue
            parties = [(msg.sender, 0)] + [(p, 1) for p in msg.to + msg.cc]
            for ev in msg.events:
                parties += [(p, 2) for p in ev.attendees] + ([(ev.organizer, 2)] if ev.organizer else [])
            counted: set[str] = set()
            for party, priority in parties:
                if not classify.is_person(party.email, owner):
                    continue
                rec = people.setdefault(party.email, {"names": [], "title": "", "title_at": None, "first": msg.date,
                                                      "last": msg.date, "count": 0, "apps": set(), "company": ""})
                if party.name and "@" not in party.name:
                    rec["names"].append((priority, party.name))
                if party.email not in counted:
                    counted.add(party.email)
                    rec["count"] += 1
                    rec["first"], rec["last"] = min(rec["first"], msg.date), max(rec["last"], msg.date)
                    rec["apps"].add(app.id)
                    rec["company"] = app.company
            sender = msg.sender.email
            if sender in people:
                sig = classify.parse_signature(msg.clean, msg.sender.name, sender)
                if sig and sig.title and (people[sender]["title_at"] is None or msg.date >= people[sender]["title_at"]):
                    people[sender]["title"], people[sender]["title_at"] = sig.title, msg.date
    contacts = []
    for address, rec in people.items():
        name = min(rec["names"], key=lambda n: n[0])[1] if rec["names"] else _fallback_name(address)
        contacts.append({
            "id": "c_" + sha1_hex(address.lower())[:8], "name": name, "email": address, "title": rec["title"],
            "company": rec["company"], "first_seen": iso_ts(rec["first"]), "last_seen": iso_ts(rec["last"]),
            "message_count": rec["count"], "application_ids": sorted(rec["apps"]),
        })
    contacts.sort(key=lambda c: (c["last_seen"], c["email"]), reverse=True)
    by_email = {c["email"]: c["id"] for c in contacts}
    for app in apps:
        ids = {by_email[a] for a, rec in people.items() if app.id in rec["apps"]}
        app.contact_ids = sorted(ids)
    return contacts


def build_events(apps: list[Application], messages_by_id: dict[str, ParsedMessage], owner: str) -> list[dict]:
    found: dict[tuple, tuple] = {}
    for app in apps:
        for mid in app.message_ids:
            msg = messages_by_id.get(mid)
            if msg is None:
                continue
            for ev in msg.events:
                key = (app.id, ev.start, ev.summary)
                if key not in found or msg.date >= found[key][1].date:
                    found[key] = (ev, msg, app)
    ordered = sorted(found.values(), key=lambda t: (t[0].start, t[1].id, t[0].summary))
    events = []
    for n, (ev, msg, app) in enumerate(ordered, start=1):
        attendees = [p for p in ev.attendees + ([ev.organizer] if ev.organizer else []) if p.email and p.email != owner.lower()]
        unique = {p.email: p for p in attendees}
        events.append({
            "id": f"e_{n:02d}", "application_id": app.id, "at": iso_ts(ev.start),
            "end_at": iso_ts(ev.end) if ev.end else None, "title": ev.summary or msg.subject,
            "attendees": [p.to_dict() for p in unique.values()],
            "stage": classify.event_stage(ev.summary, msg.subject), "message_id": msg.id,
        })
    for app in apps:
        app.event_ids = [e["id"] for e in events if e["application_id"] == app.id]
    return events
