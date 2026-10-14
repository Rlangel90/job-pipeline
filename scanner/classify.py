"""The inference engine: pure functions that decide what an email means for a job search.

Everything here works on already-parsed messages and touches no network or disk, so the rules
can be tuned with plain unit tests. The owner applies everywhere, including company websites,
so most of the work is reading company, role and stage out of free text.
"""
from __future__ import annotations

import fnmatch
import re
from collections import Counter
from dataclasses import dataclass, field

from .model import ACTIVE_ORDER, TERMINAL, ParsedMessage

# ---------------------------------------------------------------- domains

# Systems whose mail is, by itself, about a job application.
ATS_DOMAINS = [
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "myworkday.com", "myworkdayjobs.com", "ashbyhq.com",
    "icims.com", "smartrecruiters.com", "jobvite.com", "bamboohr.com", "breezy.hr", "workable.com", "dover.com",
    "gem.com", "hireflix.com", "taleo.net", "successfactors.com", "ultipro.com", "jazz.co", "applytojob.com",
    "recruitee.com", "teamtailor.com", "pinpointhq.com", "eightfold.ai", "phenom.com", "avature.net",
    "brassring.com", "hirevue.com", "goodtime.io", "modernloop.io",
]
# Boards, schedulers and payroll vendors send plenty of unrelated mail, so they need a job phrase too.
BOARD_DOMAINS = [
    "linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com", "dice.com", "hired.com", "otta.com",
    "welcometothejungle.com", "wellfound.com", "calendly.com", "rippling.com", "paylocity.com",
    "paycomonline.net", "adp.com",
]
GENERIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "google.com", "example.com",
}

SOURCE_BY_DOMAIN = [
    (("greenhouse.io", "greenhouse-mail.io"), "greenhouse"),
    (("lever.co",), "lever"),
    (("myworkday.com", "myworkdayjobs.com"), "workday"),
    (("ashbyhq.com",), "ashby"),
    (("icims.com",), "icims"),
    (("smartrecruiters.com",), "smartrecruiters"),
    (("linkedin.com",), "linkedin"),
    (("indeed.com",), "indeed"),
]


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


def domain_in(domain: str, suffixes: list[str] | tuple[str, ...]) -> bool:
    return any(domain == s or domain.endswith("." + s) for s in suffixes)


def is_ats(domain: str) -> bool:
    return domain_in(domain, ATS_DOMAINS)


def is_board(domain: str) -> bool:
    return domain_in(domain, BOARD_DOMAINS)


def counterparty_addresses(msg: ParsedMessage, owner: str = "") -> list:
    """Whoever the message is to or from, other than the owner."""
    owner = owner.lower()
    parties = [msg.sender] if msg.direction == "in" else (msg.to + msg.cc)
    return [p for p in parties if p.email and p.email != owner]


def registrable_label(domain: str) -> str:
    """mail.acmecorp.com -> acmecorp; shop.acme.co.uk -> acme."""
    labels = domain.split(".")
    if len(labels) < 2:
        return labels[0]
    if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac", "gov", "edu") and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2]


# ---------------------------------------------------------------- rules from overrides

@dataclass
class Rules:
    ignore_senders: list[str] = field(default_factory=list)
    ignore_subjects: list = field(default_factory=list)

    def __post_init__(self):
        self.ignore_subjects = [re.compile(p, re.I) if isinstance(p, str) else p for p in self.ignore_subjects]


def _sender_ignored(address: str, patterns: list[str]) -> bool:
    address = address.lower()
    for raw in patterns:
        pat = raw.strip().lower()
        if not pat:
            continue
        if pat.startswith("@"):
            if address.endswith(pat) or domain_of(address).endswith(pat[1:]):
                return True
        elif "@" in pat or "*" in pat:
            if fnmatch.fnmatch(address, pat):
                return True
        elif domain_of(address) == pat or domain_of(address).endswith("." + pat):
            return True
    return False


# ---------------------------------------------------------------- labels

LABEL_STAGES = {
    "jobs/applied": "applied", "jobs/screen": "recruiter_screen", "jobs/recruiter_screen": "recruiter_screen",
    "jobs/hm": "hiring_manager", "jobs/hiring_manager": "hiring_manager", "jobs/technical": "technical",
    "jobs/onsite": "onsite", "jobs/offer": "offer", "jobs/rejected": "rejected", "jobs/withdrew": "withdrew",
    "jobs/accepted": "accepted",
}


def has_ignore_label(msg: ParsedMessage) -> bool:
    return any(lab.lower() == "jobs/ignore" for lab in msg.labels)


def has_force_label(msg: ParsedMessage) -> bool:
    return any(lab.lower() == "jobs" or (lab.lower().startswith("jobs/") and lab.lower() != "jobs/ignore") for lab in msg.labels)


def label_stage(labels: list[str]) -> str | None:
    for lab in labels:
        stage = LABEL_STAGES.get(lab.lower())
        if stage:
            return stage
    return None


# ---------------------------------------------------------------- noise

NOISE_SUBJECT_RE = re.compile(
    r"jobs? (?:for you|you may|matching|alert|recommend)"            # "5 jobs for you"
    r"|\bnew jobs\b|\d+ new (?:jobs|roles)"                           # "8 new roles this week"
    r"|who(?:'s|’s| is) viewing your profile|people are looking at"
    r"|job alert|daily digest|weekly digest|newsletter",
    re.I,
)
NOISE_SENDER_RE = re.compile(
    r"^(?:jobalerts?-noreply|job-?alerts?|jobs-listings|alerts?|newsletter|digest|recommendations?|jobs-recommendations?)@"
    r"|^noreply@glassdoor\.com$",
    re.I,
)  # jobalerts-noreply@linkedin.com, alert@indeed.com, noreply@glassdoor.com
APPLICATION_ACTIVITY_RE = re.compile(r"your application|you applied|applied to|application (?:was|has been)|interview", re.I)


def is_noise(msg: ParsedMessage, rules: Rules | None = None) -> bool:
    """Alerts, digests and marketing that mention jobs without being about one of the owner's applications."""
    addr = msg.sender.email
    domain = domain_of(addr)
    if rules:
        if _sender_ignored(addr, rules.ignore_senders):
            return True
        if any(p.search(msg.subject) for p in rules.ignore_subjects):
            return True
    if msg.direction == "out" and msg.to and all(p.email == addr for p in msg.to + msg.cc):
        return True  # a note to self
    if NOISE_SUBJECT_RE.search(msg.subject):
        return True
    if (is_board(domain) or domain_in(domain, ["glassdoor.com", "linkedin.com", "indeed.com", "ziprecruiter.com"])) and NOISE_SENDER_RE.search(addr):
        return True
    if msg.list_unsubscribe and msg.bulk and not is_ats(domain):
        return True
    if is_board(domain) and (msg.list_unsubscribe or "unsubscribe" in msg.clean.lower()) \
            and not APPLICATION_ACTIVITY_RE.search(msg.subject + "\n" + msg.clean):
        return True
    return False


# ---------------------------------------------------------------- job relatedness

JOB_SIGNALS = [re.compile(p, re.I) for p in [
    r"thank(?:s| you) for applying",
    r"your application",
    r"application (?:received|submitted)",
    r"we(?:['’]ve| have)? received your application",
    r"application for",
    r"applied to",
    r"\binterview",
    r"phone screen",
    r"next steps",
    r"hiring manager",
    r"\brecruiter",
    r"talent acquisition",
    r"take-?home",
    r"technical assessment",
    r"coding challenge",
    r"hackerrank|codility|coderpad",
    r"offer letter",
    r"we would like to extend",
    r"not moving forward",
    r"other candidates",
    r"position has been filled",
    r"no longer under consideration",
    r"decided to pursue",
    r"unfortunately",
    r"background check",
    r"job description",
    r"role at",
    r"position at",
    r"opportunity at",
    r"my application",
]]  # each pattern is one signal, e.g. "we received your application"

SENIORITY = r"(?:Senior|Sr\.?|Staff|Lead|Principal|Head of|Director of|Junior|Associate)"
FIELD = r"(?:Data|Analytics|Software|ML|Machine Learning|Platform|BI|Business Intelligence|Backend|Full[- ]Stack|Cloud|DevOps|Solutions|Product)"
NOUN = r"(?:Engineering Manager|Engineer|Scientist|Analyst|Architect|Developer|Manager|Lead|Engineering)"
TITLE_RE = re.compile(
    rf"\b(?:{SENIORITY}\s+)?{FIELD}\s*{NOUN}\b(?:\s+(?:III|II|IV|I|\d)\b)?"   # Senior Data Engineer II
    r"|\bHead of (?:Data|Analytics|Engineering|BI)\b|\bData Engineering Manager\b",
    re.I,
)


def signal_count(msg: ParsedMessage) -> int:
    text = msg.subject + "\n" + msg.clean
    n = sum(1 for p in JOB_SIGNALS if p.search(text))
    if TITLE_RE.search(msg.subject) or TITLE_RE.search(msg.clean[:3000]):
        n += 1
    if msg.events and (_stage_by_rules("\n".join(e.summary for e in msg.events), "in", middle_only=True) or any(
            re.search(r"interview|screen|call|chat|intro|meet", e.summary, re.I) for e in msg.events)):
        n += 1
    return n


def is_job_related(msg: ParsedMessage, rules: Rules | None = None, owner: str = "") -> bool:
    if has_ignore_label(msg):
        return False
    if is_noise(msg, rules):
        return False
    if has_force_label(msg):
        return True
    domains = [domain_of(p.email) for p in counterparty_addresses(msg, owner)]
    if any(is_ats(d) for d in domains):
        return True
    n = signal_count(msg)
    if any(is_board(d) for d in domains):
        return n >= 1
    return n >= 2


# ---------------------------------------------------------------- names

_SUFFIX_WORDS = {"careers", "recruiting", "recruitment", "talent", "team", "hr", "hiring", "jobs", "inc", "llc", "ltd",
                 "corporation", "acquisition"}
_KEY_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "incorporated", "limited", "gmbh"}
_GENERIC_COMPANY_KEYS = {
    "linkedin", "indeed", "glassdoor", "ziprecruiter", "greenhouse", "lever", "workday", "ashby", "icims", "jobvite",
    "smartrecruiters", "calendly", "googlecalendar", "google", "hiringteam", "recruitingteam", "talentacquisition",
    "humanresources", "you", "your", "us", "our", "the", "noreply", "donotreply", "notifications", "myworkday",
}
_STARTER_WORDS = {"We", "Our", "The", "Please", "Thank", "Thanks", "You", "Your", "If", "I", "It", "This", "Hi", "Hello",
                  "Do", "Are", "Is", "As", "At", "In", "On", "For", "To", "A", "An", "And", "But", "Let", "Here", "My", "Dear"}
_ABBREV = {"inc", "corp", "co", "ltd", "llc", "bros", "mr", "mrs", "ms", "dr", "jr", "sr", "st", "no"}
_LEADING_REJECT = {"I", "We", "You", "Your", "The", "Our", "My", "This", "That", "These", "It", "If", "Thanks", "Thank",
                   "Hi", "Hello", "Dear", "Best", "Regards", "Please", "Us", "A", "An"}


def normalize_company(raw: str) -> str:
    """Strip career-site suffixes and legal endings, collapse spaces, fix casing."""
    tokens = re.sub(r"\s+", " ", raw or "").strip().split(" ")
    tokens = [t for t in tokens if t]
    while tokens and tokens[-1].rstrip(".,;:!?").lower() in _SUFFIX_WORDS:
        tokens.pop()
        if tokens:
            tokens[-1] = tokens[-1].rstrip(",;:")
    if not tokens:
        return ""
    tokens[-1] = tokens[-1].rstrip(",;:!?")
    if tokens[-1].lower() not in ("corp.", "co.", "inc.", "ltd."):
        tokens[-1] = tokens[-1].rstrip(".")
    out = []
    for tok in tokens:
        bare = tok.rstrip(".,")
        if bare.isupper() and len(bare) <= 4:
            out.append(tok)
        elif bare.isupper() or bare.islower():
            out.append(tok.title())
        else:
            out.append(tok)
    return " ".join(out).replace("Corp.", "Corp")


def company_key(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    trimmed = list(words)
    while len(trimmed) > 1 and trimmed[-1] in _KEY_SUFFIXES:
        trimmed.pop()
    return "".join(trimmed)


# ---------------------------------------------------------------- company extraction

_CAP = r"([A-Z][\w&.'-]+(?: [A-Z&][\w&.'-]+){0,4})"
COMPANY_PREFIX_RE = re.compile(
    r"\b(?i:application (?:to|for|with)|applying (?:to|at|for)|your interest in|interest in joining|"
    r"opportunity (?:at|with)|position (?:at|with)|role (?:at|with)|interview (?:at|with)|team at|applied to|"
    r"application was sent to|application was submitted to) " + _CAP
)  # "Thank you for applying to Acme Corp" -> Acme Corp
COMPANY_FROM_RE = re.compile(r"\b(?i:from) " + _CAP)  # subject only: "Update from Acme Corp"
COMPANY_TITLE_AT_RE = re.compile(
    r"\b(?:Engineer|Scientist|Analyst|Architect|Developer|Manager|Lead|Engineering|Head of Data) (?i:at) " + _CAP
)  # "Senior Data Engineer at Acme Corp"
COMPANY_VIEWED_RE = re.compile(_CAP + r" (?i:has )?(?i:viewed your application)")  # "Orbitwave viewed your application"
COMPANY_SUMMARY_RE = re.compile(
    r"\b(?i:phone screen|recruiter screen|screen|intro(?:ductory)? call|call|chat|interview|sync|onsite|final round)"
    r"\s*(?:[:–-]|(?i:at))\s+" + _CAP
)  # "Phone screen: Acme Corp @ Tue Oct 6"

CONF_EXPLICIT, CONF_SIGNATURE, CONF_NAME, CONF_FROM, CONF_DOMAIN = 3.0, 2.0, 2.0, 1.5, 1.0
GENERIC_MAILBOXES = {"careers", "recruiting", "recruitment", "talent", "hr", "jobs", "apply", "applications", "application",
                     "hiring", "people", "team", "hello", "info"}


def _clip_head(tokens: list[str]) -> list[str]:
    """Keep tokens up to the end of the first sentence ("Acme Corp. We" -> Acme Corp.)."""
    out: list[str] = []
    for tok in tokens:
        if out:
            prev = out[-1]
            ends = prev.endswith((".", "!", "?"))
            if ends and prev.rstrip(".!?").lower() not in _ABBREV:
                break
            if prev.endswith(".") and tok in _STARTER_WORDS:
                break
        out.append(tok)
    return out


def _clip_tail(tokens: list[str]) -> list[str]:
    """Keep tokens after the last sentence break ("Good news. Orbitwave" -> Orbitwave)."""
    out: list[str] = []
    for tok in reversed(tokens):
        out.append(tok)
        idx = len(tokens) - len(out) - 1
        if idx >= 0:
            prev = tokens[idx]
            if prev.endswith((".", "!", "?")) and prev.rstrip(".!?").lower() not in _ABBREV:
                break
    return list(reversed(out))


def _clean_company_candidate(raw: str, *, tail: bool = False) -> str:
    tokens = raw.split(" ")
    tokens = _clip_tail(tokens) if tail else _clip_head(tokens)
    cand = " ".join(tokens)
    m = TITLE_RE.search(cand)
    if m:
        if m.start() == 0:
            return ""
        cand = cand[:m.start()].strip()
    cand = normalize_company(cand)
    if not cand or cand.split(" ")[0] in _LEADING_REJECT:
        return ""
    if company_key(cand) in _GENERIC_COMPANY_KEYS or len(company_key(cand)) < 2:
        return ""
    return cand


def _person_keys(msg: ParsedMessage) -> set[str]:
    keys = set()
    parties = [msg.sender] + msg.to + msg.cc
    for ev in msg.events:
        parties += ev.attendees + ([ev.organizer] if ev.organizer else [])
    for p in parties:
        if p.name:
            keys.add(re.sub(r"[^a-z0-9]", "", p.name.lower()))
    return keys


def _explicit_candidates(msg: ParsedMessage) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    people = _person_keys(msg)
    sources = [msg.subject] + [e.summary for e in msg.events] + [msg.clean[:4000]]
    for i, text in enumerate(sources):
        for rx, tail, conf, subject_only in (
            (COMPANY_PREFIX_RE, False, CONF_EXPLICIT, False),
            (COMPANY_VIEWED_RE, True, CONF_EXPLICIT, False),
            (COMPANY_SUMMARY_RE, False, CONF_EXPLICIT, False),
            (COMPANY_TITLE_AT_RE, False, CONF_EXPLICIT, False),
            (COMPANY_FROM_RE, False, CONF_FROM, True),
        ):
            if subject_only and i != 0:
                continue
            for m in rx.finditer(text):
                cand = _clean_company_candidate(m.group(1), tail=tail)
                if cand and re.sub(r"[^a-z0-9]", "", cand.lower()) not in people:
                    found.append((cand, conf))
    return found


_SIGNOFF_RE = re.compile(
    r"^(?:thanks|thank you|many thanks|best|best regards|kind regards|warm regards|regards|cheers|sincerely|warmly|"
    r"talk soon|best wishes|all the best)[,.!]?\s*$", re.I)
_TITLE_WORDS_RE = re.compile(r"Recruit|Talent|Sourc|Engineer|Manager|Director|Head|Lead|Partner|VP|Chief|Coordinator|Acquisition", re.I)


def looks_like_title(line: str) -> bool:
    return bool(_TITLE_WORDS_RE.search(line)) and len(line.split()) <= 8


def signature_block(text: str, max_lines: int = 4) -> list[str]:
    """Up to four non-empty lines after the last sign-off ("Thanks,", "Best,"...)."""
    lines = text.split("\n")
    idx = None
    for i, line in enumerate(lines):
        if _SIGNOFF_RE.match(line.strip()):
            idx = i
    if idx is None:
        return []
    block = [ln.strip() for ln in lines[idx + 1:] if ln.strip()]
    return block[:max_lines]


@dataclass
class Signature:
    name: str
    title: str
    company: str


def _split_title_company(line: str) -> tuple[str, str]:
    for sep in (",", "|", " at ", " @ ", " - "):
        if sep in line:
            left, right = line.split(sep, 1)
            if looks_like_title(left):
                return left.strip(), right.strip()
    return line.strip(), ""


def _short_name_like(line: str) -> bool:
    return bool(line) and len(line.split()) <= 6 and not re.search(r"[@\d]|https?:", line)


def parse_signature(clean_text: str, display_name: str, address: str) -> Signature | None:
    block = signature_block(clean_text)
    if not block:
        return None
    bare = display_name.strip() if display_name and "@" not in display_name else ""
    first = (bare.split()[0] if bare else re.split(r"[._\-+]", address.split("@")[0])[0]).lower()
    if not first or not block[0].lower().startswith(first):
        return None
    title = company = ""
    if len(block) > 1 and looks_like_title(block[1]):
        title, company = _split_title_company(block[1])
        if not company and len(block) > 2 and _short_name_like(block[2]):
            company = block[2]
    return Signature(name=block[0], title=title, company=company)


def _signature_company(msg: ParsedMessage) -> str:
    block = signature_block(msg.clean)
    for i, line in enumerate(block):
        if looks_like_title(line):
            title, company = _split_title_company(line)
            if not company and i + 1 < len(block) and _short_name_like(block[i + 1]):
                company = block[i + 1]
            if company:
                return normalize_company(company)
    return ""


def company_candidates(msg: ParsedMessage, owner: str = "") -> list[tuple[str, float]]:
    found = _explicit_candidates(msg)
    if msg.direction == "in":
        sender = msg.sender
        local, domain = sender.email.split("@", 1) if "@" in sender.email else (sender.email, "")
        name = normalize_company(sender.name)
        if sender.name and name and company_key(name) not in _GENERIC_COMPANY_KEYS and name.lower() != local.lower():
            if domain_in(domain, ATS_DOMAINS) and not is_board(domain):
                found.append((name, CONF_NAME))
            elif local.lower() in GENERIC_MAILBOXES and not is_board(domain) and not domain_in(domain, GENERIC_MAIL_DOMAINS):
                found.append((name, CONF_NAME))
        sig = _signature_company(msg)
        if sig and company_key(sig) not in _GENERIC_COMPANY_KEYS:
            found.append((sig, CONF_SIGNATURE))
    owner_domain = domain_of(owner)
    for party in counterparty_addresses(msg, owner):
        domain = domain_of(party.email)
        if not domain or domain == owner_domain or domain_in(domain, GENERIC_MAIL_DOMAINS) or is_ats(domain) or is_board(domain):
            continue
        label = registrable_label(domain)
        cand = normalize_company(label.replace("-", " ").replace("_", " "))
        if cand:
            found.append((cand, CONF_DOMAIN))
    return found


def best_company(messages: list[ParsedMessage], owner: str = "") -> tuple[str, float]:
    """Highest-confidence company across a thread; ties go to the name seen most often."""
    scored: dict[str, dict] = {}
    for msg in sorted(messages, key=lambda m: m.date):
        for name, conf in company_candidates(msg, owner):
            key = company_key(name)
            if not key:
                continue
            slot = scored.setdefault(key, {"conf": 0.0, "count": 0, "forms": {}})
            slot["count"] += 1
            if conf > slot["conf"]:
                slot["conf"] = conf
                slot["forms"] = Counter()
            if conf == slot["conf"]:
                slot["forms"][name] += 1
    if not scored:
        return "", 0.0
    key = max(scored, key=lambda k: (scored[k]["conf"], scored[k]["count"]))
    forms = scored[key]["forms"]
    name = max(forms, key=lambda f: (forms[f], len(f)))
    return name, scored[key]["conf"]


# ---------------------------------------------------------------- role extraction

ROLE_PATTERNS = [re.compile(p, re.I | re.M) for p in [
    r"for the (.+?) (?:position|role|opening|job)",          # "for the Senior Data Engineer position"
    r"application for (.+?)(?: at | with |$)",               # "application for Data Analyst"
    r"(?:position|role) of (.+?)[.,\n]",                     # "the position of Staff Analytics Engineer,"
    r"applied for (.+?)(?: at |$)",                          # "You applied for Lead BI Developer at Foo"
]]
_SEGMENT_SPLIT_RE = re.compile(r"\s[-–—|]\s|:\s")
_LEFT_STOP = {"application", "thank", "thanks", "you", "your", "for", "the", "at", "re", "fwd", "fw", "invitation", "offer",
              "letter", "next", "steps", "interview", "phone", "screen", "call", "with", "and", "about", "new", "a", "an",
              "to", "of", "job", "role", "position", "our", "my", "update", "updated", "received"}
_REQ_RES = [
    re.compile(r"\((?:[A-Za-z]{1,4}[-\s#]*)?[\w-]*\d[\w-]*\)"),   # (R12345) (REQ-2026-9)
    re.compile(r"\s[-–]\s*\d{4}-\d+\b"),                       # - 2026-123
    re.compile(r"\bReq(?:uisition)?\.?\s*#?\s*[\w-]*\d[\w-]*", re.I),  # Req #4411
    re.compile(r"#\d+"),
]
_ROLE_TRAILING_RE = re.compile(r"\s+(?:position|role|opening|job)\s*$", re.I)


def clean_role(text: str) -> str:
    out = text
    for rx in _REQ_RES:
        out = rx.sub("", out)
    out = re.sub(r"\s+", " ", out).strip(" -–:,.;|")
    out = re.sub(r"^(?:the|a|an)\s+", "", out, flags=re.I)
    out = _ROLE_TRAILING_RE.sub("", out)
    return out.strip(" -–:,.;|")


def _extended_title(text: str, m: re.Match) -> str:
    """The title regex hit plus up to two capitalized words just before it (Data + Platform Engineer)."""
    start = m.start()
    words = re.findall(r"\S+", text[:start])
    take = 0
    while take < 2 and take < len(words):
        w = words[-1 - take]
        if not re.fullmatch(r"[A-Z][A-Za-z]+", w) or w.lower() in _LEFT_STOP:
            break
        take += 1
    prefix = " ".join(words[len(words) - take:]) if take else ""
    return clean_role((prefix + " " + m.group(0)).strip())


def _titles_in(text: str) -> list[str]:
    return [t for t in (_extended_title(text, m) for m in TITLE_RE.finditer(text)) if t]


def _plain_role(candidate: str) -> str:
    """An explicit-pattern capture: use the title inside it, else accept a short capitalized phrase."""
    candidate = candidate.strip()
    m = TITLE_RE.search(candidate)
    if m:
        return _extended_title(candidate, m)
    words = candidate.split()
    cleaned = clean_role(candidate)
    if cleaned and len(words) <= 4 and all(w[:1].isupper() or w.lower() in ("of", "and", "&", "the", "for") for w in words):
        return cleaned
    return ""


def role_candidates(msg: ParsedMessage) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    sources = [msg.subject] + [e.summary for e in msg.events] + [msg.clean[:4000]]
    for text in sources:
        for rx in ROLE_PATTERNS:
            for m in rx.finditer(text):
                role = _plain_role(m.group(1))
                if role and len(role) <= 80:
                    found.append((role, 3.0))
    for text in [msg.subject] + [e.summary for e in msg.events]:
        for seg in _SEGMENT_SPLIT_RE.split(text):
            for role in _titles_in(seg):
                found.append((role, 2.0))
    for line in msg.clean[:4000].split("\n"):
        line = line.strip()
        if 0 < len(line.split()) <= 8:
            for role in _titles_in(line):
                if clean_role(line) == role:
                    found.append((role, 1.5))
    for role in _titles_in(msg.clean[:3000]):
        found.append((role, 1.0))
    return found


def best_role(messages: list[ParsedMessage]) -> str:
    scored: dict[str, dict] = {}
    for msg in sorted(messages, key=lambda m: m.date):
        for role, conf in role_candidates(msg):
            slot = scored.setdefault(role.lower(), {"conf": 0.0, "count": 0, "form": role})
            slot["count"] += 1
            if conf > slot["conf"]:
                slot["conf"] = conf
                slot["form"] = role
    if not scored:
        return ""
    best = max(scored.values(), key=lambda s: (s["conf"], s["count"], len(s["form"])))
    return best["form"]


def best_role_with_conf(messages: list[ParsedMessage]) -> tuple[str, float]:
    role = best_role(messages)
    if not role:
        return "", 0.0
    conf = max((c for m in messages for r, c in role_candidates(m) if r.lower() == role.lower()), default=0.0)
    return role, conf


# ---------------------------------------------------------------- stage signals

_RULES_OUT = [
    ("accepted", r"accept(?:ing)? (?:the|your) offer|happy to accept|\bI accept\b"),
    ("withdrew", r"withdraw|withdrawing|no longer interested|remove my application|pursue other"),
]
_RULES_IN = [
    ("rejected", r"unfortunately|not (?:be )?moving forward|other candidates|not selected|decided to (?:pursue|proceed|move forward) with|"
                 r"will not be (?:moving|proceeding)|no longer under consideration|position has been filled|"
                 r"closed the (?:position|role|req)|not a (?:match|fit) at this time|we have decided|regret to inform|won['’]t be moving"),
    ("offer", r"offer letter|extend (?:you )?an offer|pleased to offer|offer of employment|compensation package|we would like to offer"),
    ("onsite", r"onsite|on-site|final round|final interview|panel interview|virtual onsite|interview loop|meet the team|super ?day"),
    ("technical", r"technical (?:interview|screen|assessment|round)|take[- ]home|coding (?:challenge|exercise|interview)|hackerrank|"
                  r"codility|coderpad|case study|sql (?:assessment|exercise|test)|assessment link|technical phone"),
    ("hiring_manager", r"hiring manager|meet (?:with )?the manager|conversation with the (?:team lead|manager|director)|"
                       r"manager (?:screen|interview|call)"),
    ("recruiter_screen", r"phone screen|intro(?:ductory)? call|initial (?:call|conversation|screen)|recruiter (?:call|screen|chat)|"
                         r"schedule (?:a|some) time|quick (?:chat|call)|15[- ]minute|20[- ]minute|30[- ]minute|calendly\.com|"
                         r"love to (?:chat|connect)|set up (?:a|some) time|availability (?:for|to)|would you be available"),
    ("applied", r"thank(?:s| you) for (?:applying|your application|your interest)|application (?:received|submitted|was sent|"
                r"has been received|was submitted)|we(?:['’]ve| have) received your application|successfully (?:applied|submitted)|"
                r"confirm(?:ation|ing)? (?:of )?your application"),
]
STAGE_OUT = [(s, re.compile(p, re.I)) for s, p in _RULES_OUT]
STAGE_IN = [(s, re.compile(p, re.I)) for s, p in _RULES_IN]
_MIDDLE = {"onsite", "technical", "hiring_manager", "recruiter_screen"}
_APPLIED_RE = dict(STAGE_IN)["applied"]


@dataclass
class StageSignal:
    stage: str
    evidence: str  # confirmation | email | calendar_invite | label


def _stage_by_rules(text: str, direction: str, *, middle_only: bool = False, subject: str = "") -> str | None:
    rules = STAGE_OUT if direction == "out" else STAGE_IN
    applied_subject = bool(subject and _APPLIED_RE.search(subject))
    for stage, rx in rules:
        if middle_only and stage not in _MIDDLE:
            continue
        haystack = text
        if applied_subject and stage in _MIDDLE:
            haystack = subject  # confirmation boilerplate talks about future screens; trust the subject
        if rx.search(haystack):
            return stage
    return None


def stage_signal(msg: ParsedMessage) -> StageSignal | None:
    """What this message says about pipeline stage; a Gmail label on the message beats inference."""
    labelled = label_stage(msg.labels)
    if labelled:
        return StageSignal(labelled, "label")
    summaries = "\n".join(e.summary for e in msg.events)
    text = "\n".join(part for part in (msg.subject, summaries, msg.clean) if part)
    stage = _stage_by_rules(text, msg.direction, subject=msg.subject)
    if stage is None:
        if msg.events and msg.direction == "in":
            return StageSignal("recruiter_screen", "calendar_invite")
        return None
    if stage in _MIDDLE and msg.events:
        return StageSignal(stage, "calendar_invite")
    return StageSignal(stage, "confirmation" if stage == "applied" else "email")


def event_stage(summary: str, subject: str = "") -> str:
    return _stage_by_rules(summary + "\n" + subject, "in", middle_only=True) or "recruiter_screen"


# ---------------------------------------------------------------- people

_NOT_PERSON_RE = re.compile(
    r"no-?reply|donotreply|notifications?@|mailer|automated|jobs-noreply|alerts?@|calendar-notification|"
    r"team@|hello@|careers@|recruiting@|talent@|hr@|apply@|applications?@|support@|info@|jobs@|recruitment@",
    re.I,
)


def is_person(address: str, owner: str = "") -> bool:
    address = address.lower().strip()
    if not address or "@" not in address or address == owner.lower():
        return False
    if _NOT_PERSON_RE.search(address):
        return False
    domain = domain_of(address)
    return not (is_ats(domain) or is_board(domain))


# ---------------------------------------------------------------- stage helpers used by applications

def stage_rank(stage: str) -> int:
    return ACTIVE_ORDER.get(stage, 100 if stage in TERMINAL else -1)


def detect_source(messages: list[ParsedMessage], has_applied_confirmation: bool, recruiter_first: bool) -> str:
    """Where the application came from, judged by who sent the mail."""
    ordered = sorted(messages, key=lambda m: m.date)
    for msg in ordered:
        if msg.direction != "in":
            continue
        domain = domain_of(msg.sender.email)
        for suffixes, source in SOURCE_BY_DOMAIN:
            if domain_in(domain, list(suffixes)):
                return source
    if recruiter_first:
        return "recruiter"
    if has_applied_confirmation:
        return "company_site"
    return "other"
