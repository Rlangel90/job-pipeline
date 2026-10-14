# Job pipeline

A job-application tracker that fills itself in from my mailbox and draws the hiring pipeline as a
Sankey diagram. Tap a stage or a ribbon to see the applications behind it, tap an application to see
the job description, the people I talked to, and the email trail.

**Live page:** https://rlangel90.github.io/job-pipeline/

The page opens with demo data so anyone can see how it works. My real pipeline is in the same repo
as an encrypted file; the browser decrypts it with a passphrase I type on my phone, and nothing
leaves the device.

## How it works

```
Gmail (IMAP)  ──►  scanner (Python, GitHub Actions, every 6h)  ──►  docs/data/pipeline.enc
                       ▲                                                     │
      overrides.yml (private repo)                              GitHub Pages serves docs/
      Gmail labels  (jobs/…)                                                 ▼
                                                          browser decrypts, renders the Sankey
```

1. **Scan.** A scheduled workflow logs into the mailbox over IMAP with an app password and reads
   everything since the search started (`SCAN_SINCE`, default 2026-10-01), inbox and sent.
2. **Infer.** Rules classify each thread: is it about a job, which company, which role, and which
   stage the email signals (applied, recruiter screen, hiring manager, technical, onsite, offer,
   accepted, rejected, withdrew). Known applicant-tracking systems are recognized by sender;
   everything else is inferred from the text. Calendar invites become interview events and their
   attendees become contacts. Signature blocks give people their titles. Links to Greenhouse, Lever,
   Ashby and most career sites are followed once to snapshot the job description before it
   disappears. Anything quiet for 21 days with no interview on the calendar is marked ghosted.
3. **Correct.** Two channels, both optional: a Gmail label on any message in a thread
   (`jobs/rejected`, `jobs/screen`, `jobs/ignore`, ...) or an `overrides.yml` in a private companion
   repository for renames, forced stages, notes, pasted job descriptions, and applications that
   have no email trail. Overrides beat labels, labels beat inference.
4. **Publish.** The result is validated against `scanner/schema.json`, gzipped, encrypted with
   AES-256-GCM under a key derived from the passphrase (PBKDF2, 600k rounds), and committed as
   `docs/data/pipeline.enc`. GitHub Pages serves the static site; the page fetches the blob and
   decrypts it with WebCrypto.

The data contract between the two halves is in [CONTRACT.md](CONTRACT.md). Owner setup is in
[SETUP.md](SETUP.md).

## Layout

| Path | What |
|---|---|
| `scanner/` | Python package: IMAP/mbox reading, classification, overrides, description capture, crypto, demo generator |
| `tests/` | pytest suite with synthetic `.eml` fixtures for every email shape the rules handle |
| `docs/` | the static site (`index.html`, `app.js`, `styles.css`) and `docs/data/` |
| `.github/workflows/scan.yml` | the six-hourly scan |
| `.github/workflows/ci.yml` | tests, schema check, and the guarded-terms check on every push |
| `tools/guard_terms.py` + `.githooks/` | commit, message and push hooks that keep a short list of terms out of this public repo |

## Running locally

```
python -m venv .venv && . .venv/bin/activate        # or .venv\Scripts\activate on Windows
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q
python -m scanner demo --out docs/data/demo.json
python -m http.server --directory docs 8000         # then open http://localhost:8000
```

To run a real scan locally, export `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` and `DATA_PASSPHRASE` and
run `python -m scanner build --since 2026-10-01 --out docs/data/pipeline.enc`. A Google Takeout
mbox works too: `python -m scanner build --mbox export.mbox ...`, handy for tuning the rules
without touching the live mailbox.

## Privacy

- The public repository holds code, demo data, and ciphertext. Company names, recruiters, and email
  text exist in plaintext only inside the Actions job and inside an unlocked browser tab.
- The scanner logs counts, never subjects or names, because Actions logs on a public repository are
  public. `--verbose` exists for local debugging and refuses to run in CI.
- The passphrase can be remembered per device; it is stored in that browser's local storage only.

## License

MIT, see [LICENSE](LICENSE).
