# Data contract

This file is the agreement between the scanner (Python, runs in GitHub Actions) and the site
(static HTML/JS on GitHub Pages). Both sides are tested against it. Change it deliberately.

## Files

| Path | Written by | Read by | Committed? |
|---|---|---|---|
| `docs/data/pipeline.enc` | scanner (CI) | site | yes (ciphertext only) |
| `docs/data/demo.json` | `python -m scanner demo` | site (demo mode) | yes (fake data) |
| `overrides.yml` | the owner, by hand | scanner | NO, lives in the private companion repo |
| `scanner/schema.json` | this repo | both test suites | yes |

The plaintext pipeline JSON is never committed, never uploaded as an artifact, never printed.
CI logs on a public repo are public: the scanner prints counts only unless `--verbose`
is passed, and `--verbose` is refused when `CI=true`.

## Pipeline JSON (plaintext, validated by `scanner/schema.json`)

```jsonc
{
  "version": 1,
  "generated_at": "2026-10-14T18:00:00Z",
  "scan_window_start": "2026-10-01",
  "stages": [                      // fixed, in pipeline order; the site uses this order for the Sankey
    {"id": "applied",          "label": "Applied",          "kind": "active"},
    {"id": "recruiter_screen", "label": "Recruiter screen", "kind": "active"},
    {"id": "hiring_manager",   "label": "Hiring manager",   "kind": "active"},
    {"id": "technical",        "label": "Technical",        "kind": "active"},
    {"id": "onsite",           "label": "Onsite / panel",   "kind": "active"},
    {"id": "offer",            "label": "Offer",            "kind": "active"},
    {"id": "accepted",         "label": "Accepted",         "kind": "terminal"},
    {"id": "rejected",         "label": "Rejected",         "kind": "terminal"},
    {"id": "withdrew",         "label": "Withdrew",         "kind": "terminal"},
    {"id": "ghosted",          "label": "Ghosted",          "kind": "terminal"}
  ],
  "applications": [
    {
      "id": "app_8f2c1a",                 // stable: sha1(company_norm + "|" + role_norm)[:6]
      "company": "Acme Corp",
      "role": "Senior Data Engineer",     // "" when unknown
      "source": "greenhouse",             // linkedin | indeed | greenhouse | lever | workday | ashby | icims |
                                          // smartrecruiters | recruiter | company_site | manual | other
      "applied_at": "2026-10-02",         // date, may equal first-seen date when no confirmation email
      "current_stage": "recruiter_screen",
      "terminal": false,
      "stage_history": [                  // ascending by `at`; first entry is normally "applied"
        {"stage": "applied", "at": "2026-10-02T14:03:00Z", "message_id": "m_1a2b3c4d", "evidence": "confirmation"},
        {"stage": "recruiter_screen", "at": "2026-10-06T16:30:00Z", "message_id": "m_3c4d5e6f", "evidence": "calendar_invite"}
      ],
      "contact_ids": ["c_01"],
      "message_ids": ["m_1a2b3c4d", "m_3c4d5e6f"],   // ascending by date
      "event_ids": ["e_01"],
      "description": {                    // null when nothing known
        "url": "https://boards.greenhouse.io/acme/jobs/123",
        "text": "plain text, may be long",
        "source": "greenhouse",           // greenhouse | lever | ashby | workday | jsonld | page | manual
        "fetched_at": "2026-10-02T14:05:00Z"
      },
      "notes": "",                        // from overrides only
      "last_activity_at": "2026-10-06T16:30:00Z",
      "origin": "email"                   // email | manual (manual = exists only in overrides)
    }
  ],
  "contacts": [
    {"id": "c_01", "name": "Jane Doe", "email": "jane@acme.com", "title": "Technical Recruiter",
     "company": "Acme Corp", "first_seen": "2026-10-06T16:30:00Z", "last_seen": "2026-10-09T12:00:00Z",
     "message_count": 3, "application_ids": ["app_8f2c1a"]}
  ],
  "messages": [
    {"id": "m_1a2b3c4d",                  // "m_" + first 8 hex of sha1(gmail X-GM-MSGID)
     "thread_id": "t_9z8y7x6w",           // "t_" + first 8 hex of sha1(gmail X-GM-THRID)
     "date": "2026-10-02T14:03:00Z",
     "direction": "in",                   // in | out  (out = sent by the owner)
     "from": {"name": "Acme Recruiting", "email": "no-reply@us.greenhouse-mail.io"},
     "to": [{"name": "", "email": "owner@example.com"}],
     "subject": "Thank you for applying to Acme Corp",
     "snippet": "first ~280 chars of the plain text body, whitespace collapsed",
     "body": "full plain text body, capped at 20000 chars",
     "gmail_url": "https://mail.google.com/mail/u/0/#all/18f0c9a2b3c4d5e6",   // hex of X-GM-MSGID
     "stage_signal": "applied",           // stage id the classifier read from this message, or null
     "labels": ["jobs/applied"]           // Gmail labels on the message (only ones starting with "jobs")
    }
  ],
  "events": [                             // from calendar invites found in email
    {"id": "e_01", "application_id": "app_8f2c1a", "at": "2026-10-08T15:00:00Z", "end_at": "2026-10-08T15:30:00Z",
     "title": "Phone screen: Acme Corp", "attendees": [{"name": "Jane Doe", "email": "jane@acme.com"}],
     "stage": "recruiter_screen", "message_id": "m_3c4d5e6f"}
  ],
  "stats": {                              // precomputed, the site may recompute
    "applications": 12, "active": 5, "offers": 1, "rejected": 4, "ghosted": 2, "response_rate": 0.58
  }
}
```

Rules the site relies on:

- `stages` order is the Sankey order. Active stages flow left to right; terminal nodes hang off whichever
  stage the application was in when it ended. Applications that are not terminal flow into a virtual
  "Active" sink that the site adds itself from `current_stage`.
- `stage_history` may skip stages (applied -> technical is legal). It never goes backwards in pipeline
  order except into a terminal stage. At most one terminal entry, and it is last.
- Every id referenced anywhere exists in its collection.
- Dates: ISO 8601 UTC with a trailing `Z` for timestamps, `YYYY-MM-DD` for dates.
- Messages for an application contain real email text. The site shows them only after decryption.

## Encrypted blob `docs/data/pipeline.enc`

A JSON envelope, pretty-printed, so git diffs stay sane:

```json
{"v": 1, "kdf": "PBKDF2-SHA256", "iterations": 600000, "salt": "<base64 16 bytes>",
 "cipher": "AES-256-GCM", "iv": "<base64 12 bytes>", "compression": "gzip",
 "ct": "<base64 ciphertext incl. 16-byte GCM tag>", "generated_at": "2026-10-14T18:00:00Z",
 "applications": 12}
```

- Key = PBKDF2-HMAC-SHA256(passphrase UTF-8, salt, iterations, 32 bytes). Fresh random salt and iv every run.
- Plaintext = gzip(UTF-8 JSON). AES-GCM with no additional authenticated data.
- `generated_at` and `applications` (a count) are the only plaintext facts about the data.
- The browser decrypts with WebCrypto (`PBKDF2` -> `AES-GCM`) and `DecompressionStream("gzip")`.
- The scanner also decrypts the previous blob (same passphrase) at the start of a run to carry state
  forward: job description snapshots are reused instead of refetched, and first-seen timestamps are kept.

## Overrides (`overrides.yml`, private repo)

```yaml
# Every key is optional. Matching is on normalized company + role; `id` wins when given.
applications:
  - match: {company: "Acme", role: "Senior Data Engineer"}   # or  match: {id: app_8f2c1a}
    company: "Acme Corp"                 # rename
    role: "Senior Data Engineer"         # rename
    stage: rejected                      # force current stage (adds a stage_history entry, evidence "override")
    stage_at: 2026-10-10
    notes: "Recruiter said budget froze."
    description: |                       # inline job description text
      ...
    description_file: descriptions/acme-sde.md   # path relative to overrides.yml; wins over `description`
    description_url: https://...
    ignore: true                         # drop the application entirely (false positive)
    contacts:                            # add or enrich
      - {name: "Jane Doe", email: "jane@acme.com", title: "Technical Recruiter"}
  - company: "Stealth startup"           # no `match` + no email trail = manual application (origin "manual")
    role: "Head of Data"
    applied_at: 2026-10-03
    stage: recruiter_screen
    stage_at: 2026-10-07
    source: recruiter
ignore_senders: ["newsletter@example.com"]
ignore_subjects_matching: ["^Weekly digest"]
ghost_after_days: 21                     # default 21
```

## Gmail labels (second correction channel)

Labels the owner can put on any message in a thread (IMAP `X-GM-LABELS`):

| Label | Effect |
|---|---|
| `jobs` | force the thread to be treated as job-related |
| `jobs/ignore` | drop the thread entirely |
| `jobs/applied`, `jobs/screen`, `jobs/hm`, `jobs/technical`, `jobs/onsite`, `jobs/offer` | force that stage, dated at that message |
| `jobs/rejected`, `jobs/withdrew`, `jobs/accepted` | force that terminal stage, dated at that message |

Label mapping: `screen` -> `recruiter_screen`, `hm` -> `hiring_manager`. Labels beat classifier
inference; overrides beat labels.

## Scanner CLI

```
python -m scanner build  --since 2026-10-01 [--overrides PATH] --out docs/data/pipeline.enc
                         [--previous docs/data/pipeline.enc] [--plain-out /tmp/x.json] [--verbose]
                         [--mbox PATH]        # read from an mbox export instead of IMAP (testing/tuning)
python -m scanner demo   --out docs/data/demo.json [--seed 7]
python -m scanner decrypt --in docs/data/pipeline.enc --out /tmp/x.json      # owner-side debugging
```

Environment: `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` (IMAP, `[Gmail]/All Mail`, search
`X-GM-RAW "after:<since minus 1 day>"`), `DATA_PASSPHRASE`. Exit 0 on success, 1 on a fatal error, 2
when the scan completed but encryption or writing failed. Never print message subjects, bodies,
addresses, or company names unless `--verbose` and not CI.
