"""Command line entry point: `python -m scanner build | demo | decrypt`.

Exit codes follow CONTRACT.md: 0 success, 1 fatal error, 2 when the scan finished but encrypting
or writing the result failed. This runs in CI on a public repo, so output is counts only unless
--verbose, and --verbose is refused when CI=true.
"""
from __future__ import annotations

import argparse
import imaplib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import build, crypto, demo, mail
from . import overrides as ovrmod

log = logging.getLogger("scanner")


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse exits 2 by default, which the contract reserves for write failures
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(1)


def _make_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m scanner", description="Job application scanner")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    b = sub.add_parser("build", help="scan the mailbox and write the encrypted pipeline blob")
    b.add_argument("--since", required=True, help="first day to scan, YYYY-MM-DD")
    b.add_argument("--overrides", help="path to overrides.yml")
    b.add_argument("--out", required=True, help="where to write the encrypted blob")
    b.add_argument("--previous", help="previous blob, used to carry description snapshots forward")
    b.add_argument("--plain-out", help="also write the plaintext JSON here (local debugging only)")
    b.add_argument("--verbose", action="store_true", help="print names and subjects (never in CI)")
    b.add_argument("--mbox", help="read an mbox export instead of IMAP")
    b.add_argument("--now", help=argparse.SUPPRESS)

    d = sub.add_parser("demo", help="write a fake dataset for the site's demo mode")
    d.add_argument("--out", required=True)
    d.add_argument("--seed", type=int, default=7)

    x = sub.add_parser("decrypt", help="decrypt a blob to plaintext JSON (owner-side debugging)")
    x.add_argument("--in", dest="infile", required=True)
    x.add_argument("--out", required=True)
    return parser


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _in_ci() -> bool:
    return os.environ.get("CI") == "true"


def _load_previous(path: str | None, passphrase: str) -> tuple[dict | None, str]:
    if not path:
        return None, "none"
    try:
        envelope = crypto.read_envelope(path)
        return json.loads(crypto.decrypt_blob(envelope, passphrase)), "used"
    except Exception:  # noqa: BLE001 - a missing or unreadable previous blob only costs us the reuse
        return None, "skipped"


def _cmd_build(args) -> int:
    if args.verbose and _in_ci():
        _err("--verbose is refused when CI=true (it prints subjects and names)")
        return 1
    passphrase = os.environ.get("DATA_PASSPHRASE", "")
    if not passphrase:
        _err("DATA_PASSPHRASE is not set")
        return 1
    address = os.environ.get("GMAIL_ADDRESS", "")
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not args.mbox and not (address and app_password):
        _err("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set (or pass --mbox)")
        return 1
    try:
        since = build.parse_since(args.since)
        now = build.parse_now(args.now) if args.now else datetime.now(timezone.utc).replace(microsecond=0)
    except ValueError:
        _err("--since must be YYYY-MM-DD")
        return 1
    try:
        ov = ovrmod.load_overrides(args.overrides)
    except ovrmod.OverridesError as exc:
        _err(f"overrides error: {exc}")
        return 1

    previous, previous_state = _load_previous(args.previous, passphrase)
    if args.previous:
        log.info("previous blob: %s", previous_state)

    try:
        if args.mbox:
            if not Path(args.mbox).is_file():
                _err("mbox file not found")
                return 1
            raws = mail.read_mbox(args.mbox, since)
        else:
            raws = mail.fetch_imap(address, app_password, since, log)
    except (mail.MailError, imaplib.IMAP4.error, OSError) as exc:
        _err(f"could not read mail ({type(exc).__name__})")
        return 1
    log.info("messages fetched: %d", len(raws))

    try:
        result = build.run_scan(raws, address, since, ov, previous, now, logger=log)
    except ovrmod.OverridesError as exc:
        _err(f"overrides error: {exc}")
        return 1
    log.info("candidate threads: %d", result.counts["candidate_threads"])
    log.info("applications: %d", result.counts["applications"])
    log.info("contacts: %d", result.counts["contacts"])
    log.info("descriptions: fetched %d, reused %d, failed %d", result.counts["descriptions_fetched"],
             result.counts["descriptions_reused"], result.counts["descriptions_failed"])
    if result.counts["overrides_unmatched"]:
        log.info("override entries without a match: %d", result.counts["overrides_unmatched"])

    paths = build.validate(result.data)
    if paths:
        _err("output failed validation at: " + ", ".join(paths[:10]))
        return 1

    if args.verbose:
        for app in result.data["applications"]:
            log.info("  %s | %s | %s", app["company"], app["role"] or "(role unknown)", app["current_stage"])

    try:
        plaintext = json.dumps(result.data, ensure_ascii=False, separators=(",", ":"))
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        crypto.write_envelope(out, crypto.encrypt_blob(plaintext, passphrase))
        if args.plain_out:
            Path(args.plain_out).write_text(plaintext + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _err(f"could not write output ({type(exc).__name__})")
        return 2
    log.info("wrote encrypted blob with %d applications", result.counts["applications"])
    return 0


def _cmd_demo(args) -> int:
    demo.write_demo(args.out, args.seed)
    log.info("wrote demo data")
    return 0


def _cmd_decrypt(args) -> int:
    passphrase = os.environ.get("DATA_PASSPHRASE", "")
    if not passphrase:
        _err("DATA_PASSPHRASE is not set")
        return 1
    try:
        text = crypto.decrypt_blob(crypto.read_envelope(args.infile), passphrase)
    except (crypto.DecryptionError, OSError, ValueError):
        _err("could not decrypt the input")
        return 1
    try:
        Path(args.out).write_text(text, encoding="utf-8")
    except OSError:
        _err("could not write the output")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    previous_propagate = log.propagate
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    try:
        try:
            args = _make_parser().parse_args(argv)
        except SystemExit as exc:
            return int(exc.code or 0)
        try:
            return {"build": _cmd_build, "demo": _cmd_demo, "decrypt": _cmd_decrypt}[args.command](args)
        except Exception as exc:  # noqa: BLE001 - last resort; tracebacks could carry subjects
            _err(type(exc).__name__ if _in_ci() else f"{type(exc).__name__}: {exc}")
            return 1
    finally:
        log.removeHandler(handler)
        log.propagate = previous_propagate


if __name__ == "__main__":
    sys.exit(main())
