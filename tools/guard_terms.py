"""Block a list of terms from ever landing in this public repository.

Used by the git hooks in .githooks/ and by CI. The terms are assembled from fragments so this
file passes its own check. Usage:

    python tools/guard_terms.py --staged            # pre-commit: staged file contents
    python tools/guard_terms.py --message FILE      # commit-msg: the message being written
    python tools/guard_terms.py --tree [REV]        # every tracked file at REV (default HEAD)
    python tools/guard_terms.py --log RANGE         # commit messages in a rev range
    python tools/guard_terms.py --paths A B ...     # specific files on disk

Exit status 1 and a file:line listing when anything matches.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TERMS = (
    "".join(["cl", "au", "de"]),
    "".join(["anth", "rop", "ic"]),
)
PATTERN = re.compile("|".join(re.escape(t) for t in TERMS), re.IGNORECASE)
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf", ".zip", ".enc"}


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def scan_text(label: str, text: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if PATTERN.search(line):
            hits.append(f"{label}:{lineno}")
    return hits


def scan_blob(label: str, data: bytes) -> list[str]:
    if Path(label).suffix.lower() in SKIP_SUFFIXES:
        return []
    return scan_text(label, data.decode("utf-8", errors="replace"))


def staged() -> list[str]:
    hits = []
    names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0")
    for name in filter(None, names):
        path = name.decode("utf-8", errors="replace")
        hits += scan_blob(path, git("show", f":{path}"))
        hits += scan_text(f"{path} (file name)", path)
    return hits


def tree(rev: str) -> list[str]:
    hits = []
    names = git("ls-tree", "-r", "--name-only", "-z", rev).split(b"\0")
    for name in filter(None, names):
        path = name.decode("utf-8", errors="replace")
        hits += scan_blob(path, git("show", f"{rev}:{path}"))
        hits += scan_text(f"{path} (file name)", path)
    return hits


def log(rev_range: str) -> list[str]:
    out = git("log", "--format=%H%x00%B%x00", rev_range).decode("utf-8", errors="replace")
    hits = []
    parts = out.split("\0")
    for i in range(0, len(parts) - 1, 2):
        sha, body = parts[i].strip(), parts[i + 1]
        if sha:
            hits += scan_text(f"commit {sha[:10]} message", body)
    return hits


def paths(items: list[str]) -> list[str]:
    hits = []
    for item in items:
        p = Path(item)
        if p.is_file():
            hits += scan_blob(str(p), p.read_bytes())
    return hits


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--message", metavar="FILE")
    ap.add_argument("--tree", nargs="?", const="HEAD", metavar="REV")
    ap.add_argument("--log", metavar="RANGE")
    ap.add_argument("--paths", nargs="*", metavar="PATH")
    args = ap.parse_args(argv)

    hits: list[str] = []
    if args.staged:
        hits += staged()
    if args.message:
        hits += scan_text("commit message", Path(args.message).read_text(encoding="utf-8", errors="replace"))
    if args.tree:
        hits += tree(args.tree)
    if args.log:
        hits += log(args.log)
    if args.paths:
        hits += paths(args.paths)

    if hits:
        print("Blocked: a guarded term appears in", file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
