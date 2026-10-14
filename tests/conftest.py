"""Shared pytest setup: make the package importable and pin the clock."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OWNER = "owner@example.com"
NOW = datetime(2026, 10, 14, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def owner():
    return OWNER


@pytest.fixture
def now():
    return NOW


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any accidental HTTP request or IMAP connection fails fast instead of reaching the internet."""
    import imaplib
    import requests

    def refuse(*args, **kwargs):
        raise requests.ConnectionError("network access is disabled in tests")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse)
    monkeypatch.setattr(imaplib.IMAP4_SSL, "__init__", lambda self, *a, **k: (_ for _ in ()).throw(OSError("network disabled")))
