"""Getting messages out of a mailbox and into ParsedMessage objects.

Two sources produce the same RawMessage shape: Gmail over IMAP (with the Gmail-only message id,
thread id and label extensions) and an mbox export for offline tuning. Everything after that
(body cleanup, calendar invites, direction, ids) is shared.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
import imaplib
import logging
import mailbox
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from email.message import EmailMessage
from pathlib import Path

from bs4 import BeautifulSoup

from .model import CalendarEvent, ParsedMessage, Party, sha1_hex

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
ALL_MAIL = '"[Gmail]/All Mail"'
FETCH_BATCH = 50
BODY_CAP = 20000
SNIPPET_CAP = 280
SENT_LABEL = "\\Sent"


class MailError(RuntimeError):
    """Raised when the mailbox cannot be read."""


@dataclass
class RawMessage:
    gm_msgid: str
    gm_thrid: str
    labels: list[str]
    message: EmailMessage


@dataclass
class FetchAttrs:
    uid: str = ""
    msgid: str = ""
    thrid: str = ""
    labels: list[str] | None = None

    def __post_init__(self):
        if self.labels is None:
            self.labels = []


# ---------------------------------------------------------------- IMAP response parsing

_UID_RE = re.compile(r"\bUID (\d+)")
_MSGID_RE = re.compile(r"X-GM-MSGID (\d+)")
_THRID_RE = re.compile(r"X-GM-THRID (\d+)")
_LABELS_RE = re.compile(r"X-GM-LABELS\s*\(")  # X-GM-LABELS ("\\Inbox" "jobs/applied")


def _parse_label_list(text: str, start: int) -> list[str]:
    """Read atoms and quoted strings from `start` (just after the opening paren) to the closing paren."""
    labels: list[str] = []
    i, n = start, len(text)
    while i < n:
        ch = text[i]
        if ch == ")":
            break
        if ch.isspace():
            i += 1
        elif ch == '"':
            i += 1
            buf: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in ('\\', '"'):
                    buf.append(text[i + 1])
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1  # closing quote
            labels.append("".join(buf))
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] != ")":
                j += 1
            labels.append(text[i:j])
            i = j
    return labels


def parse_fetch_attrs(blob: bytes | str) -> FetchAttrs:
    """Pull the Gmail attributes out of the parenthesized part of an untagged FETCH response."""
    text = blob.decode("utf-8", errors="replace") if isinstance(blob, bytes) else blob
    attrs = FetchAttrs()
    m = _UID_RE.search(text)
    if m:
        attrs.uid = m.group(1)
    m = _MSGID_RE.search(text)
    if m:
        attrs.msgid = m.group(1)
    m = _THRID_RE.search(text)
    if m:
        attrs.thrid = m.group(1)
    m = _LABELS_RE.search(text)
    if m:
        attrs.labels = _parse_label_list(text, m.end())
    return attrs


def parse_fetch_response(data: list) -> list[tuple[FetchAttrs, bytes]]:
    """Pair each literal (the raw message) with the attributes around it.

    imaplib returns ``(header_bytes, literal)`` tuples; anything after the literal (including
    attributes the server chose to send last) arrives as a bare bytes item.
    """
    out: list[list] = []
    for item in data:
        if isinstance(item, tuple):
            out.append([parse_fetch_attrs(item[0]), item[1]])
        elif isinstance(item, (bytes, bytearray)) and out:
            extra = parse_fetch_attrs(bytes(item))
            attrs = out[-1][0]
            attrs.uid = attrs.uid or extra.uid
            attrs.msgid = attrs.msgid or extra.msgid
            attrs.thrid = attrs.thrid or extra.thrid
            if not attrs.labels:
                attrs.labels = extra.labels
    return [(a, raw) for a, raw in out]


def keep_labels(labels: list[str]) -> list[str]:
    """Only the owner's `jobs...` labels matter, plus \\Sent which helps decide direction."""
    return [lab for lab in labels if lab.lower().startswith("jobs") or lab == SENT_LABEL]


def labels_from_header(value: str) -> list[str]:
    """Google Takeout writes labels into an X-Gmail-Labels header, comma separated, `Sent` unprefixed."""
    labels = []
    for part in value.split(","):
        part = part.strip()
        if part in ("Sent", "Sent Mail"):
            labels.append(SENT_LABEL)
        elif part.lower().startswith("jobs"):
            labels.append(part)
    return labels


def fetch_imap(address: str, app_password: str, since: date, logger: logging.Logger) -> list[RawMessage]:
    imaplib._MAXLINE = 4_000_000  # label lists on busy threads exceed the 10k default
    cutoff = since - timedelta(days=1)
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(address, app_password)
        typ, _ = conn.select(ALL_MAIL, readonly=True)
        if typ != "OK":
            raise MailError("could not open All Mail")
        typ, data = conn.uid("SEARCH", "X-GM-RAW", f'"after:{cutoff:%Y/%m/%d}"')
        if typ != "OK":
            raise MailError("search failed")
        uids = data[0].split() if data and data[0] else []
        logger.info("imap search matched %d messages", len(uids))
        out: list[RawMessage] = []
        for i in range(0, len(uids), FETCH_BATCH):
            batch = b",".join(uids[i:i + FETCH_BATCH]).decode()
            typ, chunk = conn.uid("FETCH", batch, "(X-GM-MSGID X-GM-THRID X-GM-LABELS BODY.PEEK[])")
            if typ != "OK":
                raise MailError("fetch failed")
            for attrs, raw in parse_fetch_response(chunk):
                if not attrs.msgid:
                    continue
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                out.append(RawMessage(attrs.msgid, attrs.thrid or attrs.msgid, keep_labels(attrs.labels or []), msg))
        return out
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - closing a dead connection is best effort
            pass


# ---------------------------------------------------------------- mbox

def _decimal_id(seed: str) -> str:
    return str(int(sha1_hex(seed)[:15], 16))


def _header_ids(value: str) -> list[str]:
    return re.findall(r"<[^>]+>", value or "")


def read_mbox(path: str | Path, since: date) -> list[RawMessage]:
    """Read an mbox (Takeout) export. Uses X-GM-* headers when present, else rebuilds threads from headers."""
    cutoff = since - timedelta(days=1)
    if not Path(path).is_file():
        raise MailError("mbox file not found")
    box = mailbox.mbox(str(path))
    items: list[tuple[EmailMessage, bytes]] = []
    try:
        for key in box.iterkeys():
            raw = box.get_bytes(key)
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            items.append((msg, raw))
    finally:
        box.close()

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for msg, raw in items:
        mid = str(msg["Message-ID"] or "").strip() or "raw:" + sha1_hex(raw.decode("latin-1"))
        find(mid)
        for ref in _header_ids(str(msg["In-Reply-To"] or "")) + _header_ids(str(msg["References"] or "")):
            union(mid, ref)

    out: list[RawMessage] = []
    for msg, raw in items:
        if message_datetime(msg).date() < cutoff:
            continue
        mid = str(msg["Message-ID"] or "").strip() or "raw:" + sha1_hex(raw.decode("latin-1"))
        gm_msgid = str(msg["X-GM-MSGID"] or "").strip() or _decimal_id(mid)
        gm_thrid = str(msg["X-GM-THRID"] or "").strip() or _decimal_id(find(mid))
        labels = labels_from_header(str(msg["X-Gmail-Labels"] or ""))
        out.append(RawMessage(gm_msgid, gm_thrid, labels, msg))
    return out


# ---------------------------------------------------------------- bodies

_QUOTE_HEADER_RE = re.compile(r"^On .*wrote:\s*$")  # On Mon, Oct 5, 2026 at 9:00 AM Jane <j@x.com> wrote:
_ON_START_RE = re.compile(r"^On .+")
_WROTE_ONLY_RE = re.compile(r"^wrote:\s*$")
_FROM_BLOCK_RE = re.compile(r"^From: .*")
_ORIGINAL_RE = re.compile(r"^-{3,}\s*Original Message\s*-{3,}\s*$", re.I)
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")  # https://boards.greenhouse.io/acme/jobs/1


def _collapse_blank(text: str) -> str:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    return _collapse_blank(soup.get_text("\n"))


def _part_text(part) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError, KeyError, AttributeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def _html_parts(msg: EmailMessage) -> list[str]:
    return [_part_text(p) for p in msg.walk() if p.get_content_type() == "text/html" and not p.is_multipart()]


def message_text(msg: EmailMessage) -> str:
    """Plain text of a message: text/plain when it has content, else the HTML converted."""
    plain = ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and not part.is_multipart() and part.get_content_disposition() != "attachment":
            plain += _part_text(part) + "\n"
    plain = _collapse_blank(plain)
    if len(plain) >= 20:
        return plain
    html_parts = _html_parts(msg)
    if html_parts:
        converted = _collapse_blank("\n".join(html_to_text(h) for h in html_parts))
        if converted:
            return converted
    return plain


def strip_quoted(text: str) -> str:
    """Drop quoted reply blocks: `>` lines and everything after an attribution or forwarded-header line."""
    lines = text.split("\n")
    kept: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        is_marker = bool(
            _QUOTE_HEADER_RE.match(stripped)
            or (_ON_START_RE.match(stripped) and _WROTE_ONLY_RE.match(nxt))
            or _ORIGINAL_RE.match(stripped)
            or (i > 0 and _FROM_BLOCK_RE.match(stripped))
        )
        if is_marker:
            break
        if stripped.startswith(">"):
            continue
        kept.append(line)
    return _collapse_blank("\n".join(kept))


def _collect_urls(msg: EmailMessage, text: str) -> list[str]:
    found: list[str] = []
    for html in _html_parts(msg):
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            found.append(a["href"])
    found.extend(_URL_RE.findall(text))
    seen: dict[str, None] = {}
    for url in found:
        url = url.strip().rstrip(".,;:!?")
        if url.lower().startswith(("http://", "https://")):
            seen.setdefault(url, None)
    return list(seen)


# ---------------------------------------------------------------- calendar invites

_FALLBACK_ZONES: dict[str, tuple[int, bool]] = {
    "America/New_York": (-5, True), "US/Eastern": (-5, True), "Eastern Standard Time": (-5, True),
    "Eastern Time (US & Canada)": (-5, True), "America/Detroit": (-5, True), "America/Toronto": (-5, True),
    "America/Chicago": (-6, True), "US/Central": (-6, True), "Central Standard Time": (-6, True),
    "Central Time (US & Canada)": (-6, True),
    "America/Denver": (-7, True), "US/Mountain": (-7, True), "Mountain Standard Time": (-7, True),
    "Mountain Time (US & Canada)": (-7, True),
    "America/Phoenix": (-7, False), "US/Arizona": (-7, False),
    "America/Los_Angeles": (-8, True), "US/Pacific": (-8, True), "Pacific Standard Time": (-8, True),
    "Pacific Time (US & Canada)": (-8, True),
    "America/Anchorage": (-9, True), "Pacific/Honolulu": (-10, False),
}


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


class _UsZone(tzinfo):
    """US rules (DST from the second Sunday of March to the first Sunday of November).

    Only used when the system has no IANA database (a stock Windows Python, for example).
    """

    def __init__(self, std_hours: int, observes_dst: bool):
        self._std = timedelta(hours=std_hours)
        self._dst = observes_dst

    def _in_dst(self, dt: datetime) -> bool:
        if not self._dst:
            return False
        start = datetime.combine(_nth_sunday(dt.year, 3, 2), datetime.min.time()) + timedelta(hours=2)
        end = datetime.combine(_nth_sunday(dt.year, 11, 1), datetime.min.time()) + timedelta(hours=2)
        return start <= dt.replace(tzinfo=None) < end

    def utcoffset(self, dt):
        return self._std + (timedelta(hours=1) if dt is not None and self._in_dst(dt) else timedelta(0))

    def dst(self, dt):
        return timedelta(hours=1) if dt is not None and self._in_dst(dt) else timedelta(0)

    def tzname(self, dt):
        return "DST" if dt is not None and self._in_dst(dt) else "STD"


def _zone(name: str) -> tzinfo:
    name = (name or "").strip().strip('"')
    if not name or name.upper() in ("UTC", "Z", "GMT", "ETC/UTC"):
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - missing tz database or unknown key
        pass
    if name in _FALLBACK_ZONES:
        std, dst = _FALLBACK_ZONES[name]
        return _UsZone(std, dst)
    return timezone.utc


_ICS_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?)?(Z)?$")  # 20261008T100000Z


def _ics_datetime(value: str, params: dict[str, str]) -> datetime | None:
    m = _ICS_DT_RE.match(value.strip())
    if not m:
        return None
    y, mo, d, hh, mm, ss, z = m.groups()
    naive = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
    if z:
        return naive.replace(tzinfo=timezone.utc)
    tz = _zone(params.get("TZID", ""))
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    """NAME;PARAM=a;PARAM2="b;c":value -> (NAME, {PARAM: a, ...}, value), honoring quotes."""
    in_quote = False
    head_end = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == ":" and not in_quote:
            head_end = i
            break
    if head_end < 0:
        return None
    head, value = line[:head_end], line[head_end + 1:]
    parts: list[str] = []
    buf, in_quote = "", False
    for ch in head:
        if ch == '"':
            in_quote = not in_quote
            buf += ch
        elif ch == ";" and not in_quote:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip().strip('"')
    return parts[0].upper(), params, value


def _unescape_ics(text: str) -> str:
    return text.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _party_from_ics(params: dict[str, str], value: str) -> Party:
    addr = re.sub(r"^mailto:", "", value.strip(), flags=re.I).lower()
    return Party(name=params.get("CN", ""), email=addr)


def parse_ics(text: str) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    current: dict | None = None
    for line in _unfold(text):
        prop = _split_property(line)
        if line.strip().upper() == "BEGIN:VEVENT":
            current = {"attendees": []}
            continue
        if line.strip().upper() == "END:VEVENT":
            if current and current.get("start"):
                events.append(CalendarEvent(
                    start=current["start"], end=current.get("end"), summary=current.get("summary", ""),
                    attendees=current["attendees"], organizer=current.get("organizer")))
            current = None
            continue
        if current is None or prop is None:
            continue
        name, params, value = prop
        if name == "DTSTART":
            current["start"] = _ics_datetime(value, params)
        elif name == "DTEND":
            current["end"] = _ics_datetime(value, params)
        elif name == "SUMMARY":
            current["summary"] = _unescape_ics(value)
        elif name == "ORGANIZER":
            current["organizer"] = _party_from_ics(params, value)
        elif name == "ATTENDEE":
            current["attendees"].append(_party_from_ics(params, value))
    return events


def _calendar_events(msg: EmailMessage) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = (part.get_filename() or "").lower()
        if part.get_content_type() == "text/calendar" or filename.endswith(".ics"):
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            events.extend(parse_ics(payload.decode("utf-8", errors="replace")))
    return events


# ---------------------------------------------------------------- whole-message parsing

def message_datetime(msg: EmailMessage) -> datetime:
    value = str(msg["Date"] or "").strip()
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parties(msg: EmailMessage, header: str) -> list[Party]:
    values = [str(v) for v in (msg.get_all(header) or [])]
    return [Party(name=name.strip(), email=addr.strip().lower())
            for name, addr in email.utils.getaddresses(values) if addr]


def parse_message(raw: RawMessage, owner: str) -> ParsedMessage:
    msg = raw.message
    sender = (_parties(msg, "From") or [Party("", "")])[0]
    labels = keep_labels(raw.labels)
    direction = "out" if (owner and sender.email == owner.lower()) or SENT_LABEL in raw.labels else "in"
    text = message_text(msg)
    clean = strip_quoted(text)
    snippet = re.sub(r"\s+", " ", clean).strip()[:SNIPPET_CAP]
    try:
        gmail_url = "https://mail.google.com/mail/u/0/#all/" + format(int(raw.gm_msgid), "x")
    except ValueError:
        gmail_url = "https://mail.google.com/mail/u/0/#all/" + raw.gm_msgid
    return ParsedMessage(
        gm_msgid=raw.gm_msgid,
        gm_thrid=raw.gm_thrid,
        id="m_" + sha1_hex(raw.gm_msgid)[:8],
        thread_id="t_" + sha1_hex(raw.gm_thrid)[:8],
        date=message_datetime(msg),
        direction=direction,
        sender=sender,
        to=_parties(msg, "To"),
        cc=_parties(msg, "Cc"),
        subject=re.sub(r"\s+", " ", str(msg["Subject"] or "")).strip(),
        text=text[:BODY_CAP],
        clean=clean,
        snippet=snippet,
        labels=[lab for lab in labels if lab != SENT_LABEL],
        gmail_url=gmail_url,
        events=_calendar_events(msg),
        urls=_collect_urls(msg, text),
        list_unsubscribe=bool(msg["List-Unsubscribe"]),
        bulk=str(msg["Precedence"] or "").strip().lower() in ("bulk", "list", "junk"),
    )
