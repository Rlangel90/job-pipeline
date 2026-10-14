"""A made-up but believable pipeline for the site's demo mode.

Everything here is invented: company names, people, and `.example` addresses. The generator is
seeded and uses no clock, so the same seed always writes the same bytes. Histories are produced
by the same functions the real scanner uses, so the demo cannot drift from real output.
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import applications as appmod
from . import build
from .classify import StageSignal
from .model import Application, application_id, iso_ts, sha1_hex

GENERATED_AT = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)
WINDOW_START = "2026-10-01"
DEMO_GHOST_DAYS = 10  # the real default is 21, which a two-week window can never reach
GMAIL_URL = "https://mail.google.com/mail/u/0/#all/demo"
OWNER_NAME, OWNER_EMAIL = "Demo User", "demo.user@example.com"

COMPANIES = [
    "Northgate Analytics", "Brightwater Labs", "Cedarline Systems", "Dovetail Freight", "Emberlight Health",
    "Fieldstone Capital", "Glasshouse Retail", "Harbor & Pine", "Ironbridge Logistics", "Juniper Row",
    "Kestrel Mobility", "Lanternworks", "Marlowe Energy", "Nightingale Bio", "Oakmont Insurance",
    "Pinecrest Media", "Quillfeather", "Redwood Ledger", "Saltmarsh Foods", "Tidewater Cloud",
    "Umbra Security", "Verdant Grid", "Willowbrook Pay", "Xenon Parcel", "Yarrow Learning",
    "Zephyr Travel", "Alderbank", "Copperleaf Data",
]
ROLES = [
    "Data Engineer", "Senior Data Engineer", "Analytics Engineer", "Senior Analytics Engineer", "Staff Data Engineer",
    "Data Platform Engineer", "BI Engineer", "Data Engineering Manager", "Lead Analytics Engineer", "Principal Data Engineer",
    "Machine Learning Engineer", "Analytics Engineering Manager",
]
SOURCES = (["linkedin"] * 9 + ["indeed"] * 4 + ["greenhouse"] * 4 + ["lever"] * 3 + ["ashby"] * 2 +
           ["workday", "icims", "smartrecruiters", "recruiter"] + ["company_site"] * 2)
FIRST_NAMES = ["Avery", "Blake", "Casey", "Devon", "Emerson", "Finley", "Greer", "Harper", "Indigo", "Jules", "Kendall", "Lennox",
               "Marlo", "Noel", "Oakley", "Parker", "Quinn", "Reese", "Sasha", "Tatum"]
LAST_NAMES = ["Ashby", "Bellamy", "Calloway", "Dunmore", "Ellery", "Fairchild", "Galloway", "Hartwell", "Ingram", "Jessup",
              "Kerrigan", "Lockhart", "Mercer", "Northcott", "Oakes", "Prescott", "Radcliffe", "Sterling", "Thorne", "Whitlock"]
RECRUITER_TITLES = ["Technical Recruiter", "Senior Recruiter", "Talent Acquisition Partner", "Recruiting Coordinator", "Sourcing Lead",
                    "Senior Technical Recruiter"]
MANAGER_TITLES = ["Director of Data Engineering", "Head of Analytics", "Engineering Manager, Data Platform", "VP of Data",
                  "Analytics Engineering Lead"]

# kind -> (stage the message signals, direction, which contact sends/receives)
KINDS = {
    "confirm": ("applied", "in", None), "status": (None, "in", None), "survey": (None, "in", None),
    "reject_auto": ("rejected", "in", None),
    "intro": ("recruiter_screen", "in", "recruiter"), "reply": (None, "out", "recruiter"),
    "invite_screen": ("recruiter_screen", "in", "recruiter"), "invite_tech": ("technical", "in", "recruiter"),
    "hm": ("hiring_manager", "in", "manager"), "tech": ("technical", "in", "recruiter"),
    "tech_submit": (None, "out", "recruiter"), "onsite": ("onsite", "in", "manager"),
    "onsite_event": ("onsite", "in", "manager"), "offer": ("offer", "in", "recruiter"),
    "accept": ("accepted", "out", "recruiter"), "reject_human": ("rejected", "in", "recruiter"),
    "withdraw": ("withdrew", "out", "recruiter"),
}
EVENT_KINDS = {"invite_screen": "Recruiter screen", "invite_tech": "Technical interview", "onsite_event": "Onsite loop"}

# name -> (beats as (kind, day offset, lag hours), ghost?, still active?)
FLOWS = {
    "rejected_applied": ([("confirm", 0, 0), ("status", 2, 0), ("reject_auto", 5, 0)], False, False),
    "active_applied": ([("confirm", 0, 0), ("status", 1, 0), ("survey", 2, 0)], False, True),
    "ghost_applied": ([("confirm", 0, 0), ("status", 1, 0), ("survey", 2, 0)], True, False),
    "ghost_screen": ([("confirm", 0, 0), ("intro", 1, 0), ("reply", 1, 2)], True, False),
    "rejected_screen": ([("confirm", 0, 0), ("intro", 2, 0), ("reply", 2, 2), ("reject_human", 6, 0)], False, False),
    "active_screen": ([("confirm", 0, 0), ("intro", 2, 0), ("reply", 2, 2), ("invite_screen", 3, 0)], False, True),
    "rejected_hm": ([("confirm", 0, 0), ("intro", 2, 0), ("invite_screen", 3, 0), ("hm", 6, 0), ("reject_human", 9, 0)], False, False),
    "rejected_tech": ([("confirm", 0, 0), ("intro", 1, 0), ("tech", 4, 0), ("tech_submit", 6, 0), ("reject_human", 9, 0)], False, False),
    "rejected_onsite": ([("confirm", 0, 0), ("intro", 1, 0), ("tech", 4, 0), ("onsite", 8, 0), ("reject_human", 11, 0)], False, False),
    "active_tech": ([("confirm", 0, 0), ("intro", 1, 0), ("invite_tech", 4, 0), ("reply", 4, 1)], False, True),
    "active_onsite": ([("confirm", 0, 0), ("intro", 1, 0), ("tech", 3, 0), ("onsite_event", 6, 0)], False, True),
    "offer": ([("confirm", 0, 0), ("intro", 1, 0), ("tech", 3, 0), ("onsite", 6, 0), ("offer", 8, 0)], False, True),
    "accepted": ([("confirm", 0, 0), ("intro", 1, 0), ("tech", 3, 0), ("onsite", 6, 0), ("offer", 8, 0), ("accept", 9, 2)], False, False),
    "withdrew": ([("confirm", 0, 0), ("intro", 1, 0), ("reply", 1, 2), ("withdraw", 4, 0)], False, False),
}
FLOW_COUNTS = [("rejected_applied", 11), ("active_applied", 2), ("ghost_applied", 3), ("ghost_screen", 1), ("rejected_screen", 2),
               ("active_screen", 1), ("rejected_hm", 1), ("rejected_tech", 1), ("rejected_onsite", 1), ("active_tech", 1),
               ("active_onsite", 1), ("offer", 1), ("accepted", 1), ("withdrew", 1)]

TEMPLATES = {
    "confirm": [("Thank you for applying to {company}",
                 "Hi {owner_first},\n\nThanks for applying for the {role} role at {company}. We have received your application and our team will review it shortly.\n\nIf your background looks like a match, someone will be in touch.\n\nThe {company} Talent Team"),
                ("We received your application: {role}",
                 "Hi {owner_first},\n\nYour application for {role} at {company} is in. We read every one and will follow up if there is a fit.\n\nThe {company} Talent Team")],
    "status": [("Your application to {company} is under review",
                "Hi {owner_first},\n\nA quick note to say your application for {role} is still being reviewed. We appreciate your patience.")],
    "survey": [("{company}: a few optional questions",
                "Hi {owner_first},\n\nTo help us review your application for {role}, please complete a short optional questionnaire. It takes about five minutes.")],
    "reject_auto": [("Update on your application to {company}",
                     "Hi {owner_first},\n\nThank you for your interest in the {role} role at {company}. After careful review we have decided to move forward with other candidates. We will keep your details on file.\n\nThe {company} Talent Team")],
    "intro": [("{role} at {company}: quick intro call?",
               "Hi {owner_first},\n\nI am {cname}, {ctitle} at {company}. Your application for the {role} role caught our eye. Do you have 20 minutes this week for an introductory call?\n\nThanks,\n{cname}\n{ctitle}, {company}")],
    "reply": [("Re: {role} at {company}: quick intro call?",
               "Hi {cfirst},\n\nThanks for reaching out. Thursday afternoon works well for me and I am happy to share more about my background then.\n\nBest,\n{owner_name}")],
    "invite_screen": [("Invitation: Recruiter screen with {company}", "{cname} has invited you to a recruiter screen for the {role} role. Join with the link in the calendar invite.")],
    "invite_tech": [("Invitation: Technical interview with {company}", "{cname} has invited you to a technical interview for the {role} role. The invite has the video link.")],
    "hm": [("Next step: conversation with the hiring manager",
            "Hi {owner_first},\n\nThanks for talking with us. We would like to set up a conversation with the hiring manager for the {role} role.\n\nBest,\n{cname}\n{ctitle}, {company}")],
    "tech": [("{company}: technical assessment for {role}",
              "Hi {owner_first},\n\nThe next step is a take-home: a short SQL and data modeling exercise, about three hours. Please send it back within a few days.\n\nBest,\n{cname}\n{ctitle}, {company}")],
    "tech_submit": [("Re: {company}: technical assessment for {role}", "Hi {cfirst},\n\nAttached is my solution along with a short write-up of the modeling choices. Happy to walk through it.\n\nBest,\n{owner_name}")],
    "onsite": [("Final round: virtual onsite at {company}",
                "Hi {owner_first},\n\nWe would like to invite you to a virtual onsite: an interview loop of four sessions with the team, including a short panel interview.\n\nBest,\n{cname}\n{ctitle}, {company}")],
    "onsite_event": [("Invitation: Onsite loop with {company}", "{cname} has invited you to the virtual onsite for the {role} role. Sessions are listed in the calendar invite.")],
    "offer": [("Offer: {role} at {company}",
               "Hi {owner_first},\n\nWe are pleased to offer you the {role} position at {company}. The offer letter and compensation details are attached.\n\nWarmly,\n{cname}\n{ctitle}, {company}")],
    "accept": [("Re: Offer: {role} at {company}", "Hi {cfirst},\n\nI am happy to accept the offer. Thank you to everyone on the team. Please send the onboarding details.\n\nBest,\n{owner_name}")],
    "reject_human": [("Following up on your {company} application",
                      "Hi {owner_first},\n\nThank you for the time you put into the process. Unfortunately we will not be moving forward with your candidacy for the {role} role. We enjoyed meeting you.\n\nBest,\n{cname}\n{ctitle}, {company}")],
    "withdraw": [("Withdrawing my application", "Hi {cfirst},\n\nI have decided to pursue other opportunities and would like to withdraw my application for {role}. Thank you for your time.\n\nBest,\n{owner_name}")],
}
DESCRIPTION_PARAGRAPHS = [
    "{company} is building the data foundation for a growing business, and the {role} will own how data moves from source systems into the warehouse.",
    "You will design and maintain batch and streaming pipelines, keep them tested, and make them cheap to run.",
    "Day to day you will work with analysts and product managers to turn messy operational data into models people trust.",
    "We use SQL and Python heavily, with an orchestrator, a cloud warehouse, and dbt style transformations. Prior experience with any similar stack is fine.",
    "You should be comfortable owning a problem end to end: scoping it, building it, monitoring it, and explaining the result to non-engineers.",
    "The team is small and remote friendly. We review each other's work, write things down, and prefer simple systems that stay up.",
]
DESCRIPTION_SOURCE = {"greenhouse": "greenhouse", "lever": "lever", "ashby": "ashby", "workday": "workday"}


def _slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]", "", company.lower())


def _first(name: str) -> str:
    return name.split()[0]


def _snippet(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip()[:280]


def generate(seed: int = 7) -> dict:
    rng = random.Random(seed)
    flows = [name for name, count in FLOW_COUNTS for _ in range(count)]
    rng.shuffle(flows)
    companies = COMPANIES[:]
    rng.shuffle(companies)
    sources = SOURCES[:]
    rng.shuffle(sources)
    names = [f"{f} {l}" for f, l in zip(rng.sample(FIRST_NAMES, 17), rng.sample(LAST_NAMES, 17))]
    name_iter = iter(names)

    apps: list[Application] = []
    messages: list[dict] = []
    events: list[dict] = []
    people: dict[str, dict] = {}
    app_people: dict[str, list[str]] = {}
    msg_counter = 0

    def person(company: str, slug: str, role_kind: str) -> dict:
        name = next(name_iter)
        title = rng.choice(RECRUITER_TITLES if role_kind == "recruiter" else MANAGER_TITLES)
        email = f"{name.split()[0].lower()}.{name.split()[1].lower()}@{slug}.example"
        rec = {"name": name, "email": email, "title": title, "company": company, "dates": [], "apps": set()}
        people[email] = rec
        return rec

    for index, flow_name in enumerate(flows):
        beats, ghost, active = FLOWS[flow_name]
        company, role, source = companies[index], rng.choice(ROLES), sources[index]
        slug = _slug(company)
        last_off = max(day for _, day, _ in beats)
        if ghost:
            lo, hi = 1, 14 - DEMO_GHOST_DAYS - last_off
        elif active:
            lo, hi = max(1, 5 - last_off), 14 - last_off
        else:
            lo, hi = 1, 14 - last_off
        applied_day = rng.randint(lo, hi)
        base_hour = rng.randint(12, 15)
        base_minute = rng.choice([0, 5, 10, 15, 20, 30, 35, 40, 45, 50])

        contacts_for: dict[str, dict] = {}
        for _, (_, _, who) in ((k, KINDS[k]) for k, _, _ in beats):
            if who and who not in contacts_for:
                contacts_for[who] = person(company, slug, who)
        app_people[company] = [p["email"] for p in contacts_for.values()]

        careers = {"name": f"{company} Careers", "email": f"careers@jobs.{slug}.example"}
        owner = {"name": OWNER_NAME, "email": OWNER_EMAIL}
        thread_ids = {"auto": "t_" + sha1_hex(f"demo-thread-{seed}-{index}-auto")[:8], "human": "t_" + sha1_hex(f"demo-thread-{seed}-{index}-human")[:8]}

        items: list[tuple[datetime, str, StageSignal | None]] = []
        app_msgs: list[dict] = []
        for kind, offset, lag in beats:
            stage, direction, who = KINDS[kind]
            when = datetime(2026, 10, applied_day + offset, base_hour, base_minute, tzinfo=timezone.utc) + timedelta(hours=lag)
            when = min(when, GENERATED_AT - timedelta(minutes=30))
            contact = contacts_for.get(who) if who else None
            fields = {"company": company, "role": role, "owner_first": _first(OWNER_NAME), "owner_name": OWNER_NAME,
                      "cname": contact["name"] if contact else "", "cfirst": _first(contact["name"]) if contact else "",
                      "ctitle": contact["title"] if contact else ""}
            subject_t, body_t = rng.choice(TEMPLATES[kind])
            subject, body = subject_t.format(**fields), body_t.format(**fields)
            counterpart = {"name": contact["name"], "email": contact["email"]} if contact else careers
            sender, to = (owner, [counterpart]) if direction == "out" else (counterpart, [owner])
            msg_counter += 1
            mid = "m_" + sha1_hex(f"demo-message-{seed}-{msg_counter}")[:8]
            message = {
                "id": mid, "thread_id": thread_ids["human" if contact else "auto"], "date": iso_ts(when), "direction": direction,
                "from": sender, "to": to, "subject": subject, "snippet": _snippet(body), "body": body, "gmail_url": GMAIL_URL,
                "stage_signal": stage, "labels": [],
            }
            messages.append(message)
            app_msgs.append(message)
            if contact:
                contact["dates"].append(when)
                contact["apps"].add(company)
            evidence = "confirmation" if stage == "applied" else ("calendar_invite" if kind in EVENT_KINDS else "email")
            items.append((when, mid, StageSignal(stage, evidence) if stage else None))
            if kind in EVENT_KINDS:
                start = when + timedelta(days=1)
                start = start.replace(hour=15, minute=0, second=0)
                events.append({
                    "application_id": None, "_company": company, "at": iso_ts(start), "end_at": iso_ts(start + timedelta(minutes=45)),
                    "title": f"{EVENT_KINDS[kind]}: {company}", "attendees": [{"name": contact["name"], "email": contact["email"]}],
                    "stage": stage, "message_id": mid,
                })

        earliest = min(w for w, _, _ in items)
        history, last = appmod.build_history(items, earliest)
        app = Application(
            id=application_id(company, role), company=company, role=role, source=source, applied_at=history[0]["at"][:10],
            stage_history=history, message_ids=[m["id"] for m in sorted(app_msgs, key=lambda m: m["date"])],
            thread_ids=sorted(set(thread_ids.values())), last_activity_at=last, origin="email")
        if index % 3 == 0:
            paragraphs = [DESCRIPTION_PARAGRAPHS[i] for i in sorted(rng.sample(range(len(DESCRIPTION_PARAGRAPHS)), rng.randint(3, 6)))]
            kind = DESCRIPTION_SOURCE.get(source, rng.choice(["jsonld", "page"]))
            host = {"greenhouse": f"https://boards.greenhouse.io/{slug}/jobs/{rng.randint(4000000, 4999999)}",
                    "lever": f"https://jobs.lever.co/{slug}/{sha1_hex(f'demo-job-{seed}-{index}')[:8]}-0000-4000-8000-000000000000",
                    "ashby": f"https://jobs.ashbyhq.com/{slug}/{sha1_hex(f'demo-job-{seed}-{index}')[:8]}-0000-4000-8000-000000000000",
                    "workday": f"https://{slug}.wd5.myworkdayjobs.com/en-US/External/job/Remote/{role.replace(' ', '-')}_R-{rng.randint(1000, 9999)}"}
            app.description = {"url": host.get(kind, f"https://careers.{slug}.example/jobs/{index + 100}"),
                               "text": "\n\n".join(p.format(company=company, role=role) for p in paragraphs), "source": kind,
                               "fetched_at": iso_ts(earliest + timedelta(minutes=5))}
        apps.append(app)

    ordered = []
    for n, ev in enumerate(sorted(events, key=lambda e: (e["at"], e["message_id"])), start=1):
        ordered.append({
            "id": f"e_{n:02d}", "application_id": next(a.id for a in apps if a.company == ev["_company"]), "at": ev["at"],
            "end_at": ev["end_at"], "title": ev["title"], "attendees": ev["attendees"], "stage": ev["stage"],
            "message_id": ev["message_id"],
        })
    events = ordered
    for app in apps:
        app.event_ids = [e["id"] for e in events if e["application_id"] == app.id]
    appmod.apply_ghosting(apps, events, GENERATED_AT, DEMO_GHOST_DAYS)

    contacts = []
    id_by_email = {}
    app_id_by_company = {a.company: a.id for a in apps}
    for email, rec in people.items():
        cid = "c_" + sha1_hex(email)[:8]
        id_by_email[email] = cid
        contacts.append({
            "id": cid, "name": rec["name"], "email": email, "title": rec["title"], "company": rec["company"],
            "first_seen": iso_ts(min(rec["dates"])), "last_seen": iso_ts(max(rec["dates"])), "message_count": len(rec["dates"]),
            "application_ids": sorted(app_id_by_company[c] for c in rec["apps"]),
        })
    contacts.sort(key=lambda c: (c["last_seen"], c["email"]), reverse=True)
    for app in apps:
        app.contact_ids = sorted(id_by_email[e] for e in app_people[app.company])

    app_dicts = sorted((a.to_dict() for a in apps), key=lambda a: (a["last_activity_at"], a["id"]), reverse=True)
    messages.sort(key=lambda m: (m["date"], m["id"]))
    return {
        "version": 1, "generated_at": iso_ts(GENERATED_AT), "scan_window_start": WINDOW_START, "stages": build.stages_block(),
        "applications": app_dicts, "contacts": contacts, "messages": messages, "events": events,
        "stats": build.compute_stats(app_dicts),
    }


def write_demo(path: str | Path, seed: int = 7) -> None:
    data = generate(seed)
    build.check(data)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
