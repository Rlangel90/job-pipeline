"""This project is published as a personal repo: keep tool and vendor provenance out of every file."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".pytest_tmp", "__pycache__", ".venv", "venv", "node_modules"}

# Spelled out in pieces so this file does not trip its own check.
FORBIDDEN = [
    re.compile("cl" + "aude", re.I),
    re.compile("anth" + "ropic", re.I),
    re.compile("AI" + r"\s+assistant", re.I),
    re.compile("generated" + r"\s+by", re.I),
]


def project_files():
    for path in ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS):
            yield path


def test_no_provenance_strings_in_file_names_or_contents():
    offenders = []
    for path in project_files():
        rel = str(path.relative_to(ROOT))
        text = path.read_text(encoding="utf-8", errors="ignore")
        for rx in FORBIDDEN:
            if rx.search(rel) or rx.search(text):
                offenders.append((rel, rx.pattern))
    assert offenders == []
