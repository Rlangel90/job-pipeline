# Owner setup

Everything the scan needs lives in repository secrets. Nothing is stored on a laptop.

## 1. Gmail app password

1. Google Account → Security → 2-Step Verification must be on.
2. Google Account → Security → App passwords (https://myaccount.google.com/apppasswords) → create one
   named `job-pipeline`. Copy the 16 characters, spaces do not matter.
3. Gmail → Settings → See all settings → Forwarding and POP/IMAP → IMAP enabled (older accounts only).

Changing the Google account password revokes every app password; make a new one if the scan starts
failing with an authentication error.

## 2. Secrets on the public repository

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `GMAIL_ADDRESS` | the Gmail address being scanned |
| `GMAIL_APP_PASSWORD` | the app password from step 1 |
| `DATA_PASSPHRASE` | any long passphrase; the same one gets typed into the page to unlock it |
| `PRIVATE_DATA_TOKEN` | optional, see step 3 |

Optional repository **variable** `SCAN_SINCE` (`YYYY-MM-DD`) moves the start of the search window.
Default 2026-10-01.

## 3. Private companion repository (optional, for corrections and notes)

Create a private repository named `job-pipeline-private` under the same account containing:

```
overrides.yml
descriptions/            # pasted job descriptions referenced from overrides.yml
```

Then create a fine-grained personal access token (Settings → Developer settings → Personal access
tokens → Fine-grained): repository access limited to `job-pipeline-private`, permission
**Contents: Read-only**, expiry up to a year. Store it as `PRIVATE_DATA_TOKEN` on the public repo.
When the token is absent the scan simply runs without overrides.

`overrides.yml` format is in [CONTRACT.md](CONTRACT.md). The GitHub mobile app can edit it.

## 4. Gmail labels (optional, the other correction channel)

Create labels in Gmail: `jobs`, `jobs/ignore`, `jobs/applied`, `jobs/screen`, `jobs/hm`,
`jobs/technical`, `jobs/onsite`, `jobs/offer`, `jobs/rejected`, `jobs/withdrew`, `jobs/accepted`.
Apply one to any message in a thread from the Gmail app. The next scan picks it up.

## 5. GitHub Pages

Settings → Pages → Source: Deploy from a branch → `main` / `/docs`. The site is
`https://<user>.github.io/job-pipeline/`. Every scan that changes the data pushes a commit, which
redeploys the page within a minute or two.

## 6. First run

Actions → "Scan mailbox" → Run workflow. Check the log: it prints counts only. If it is green, open
the page, tap Unlock, type the passphrase, optionally tick "Remember on this device".

## Routine

- Something misclassified: label the thread in Gmail or add an override, wait for the next scan
  (or run the workflow by hand).
- Rotating the passphrase: change `DATA_PASSPHRASE`, run the workflow, unlock with the new one.
  The previous blob cannot be read with the new passphrase, so description snapshots are refetched
  once.
- Job search over: disable the workflow (Actions → Scan mailbox → ... → Disable workflow) and
  delete the app password in the Google account.
