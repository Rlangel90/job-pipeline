"""Passphrase encryption for the pipeline blob.

The browser decrypts this with WebCrypto, so every parameter here is fixed by CONTRACT.md:
PBKDF2-HMAC-SHA256 (600k rounds) -> AES-256-GCM, plaintext gzipped first.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ITERATIONS = 600_000
SALT_BYTES = 16
IV_BYTES = 12
KEY_BYTES = 32


class DecryptionError(ValueError):
    """Raised for a wrong passphrase, a damaged envelope, or an unsupported format."""


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, KEY_BYTES)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _plain_facts(plaintext_json: str) -> tuple[str, int]:
    """The only two facts allowed to stay readable: when it was made and how many applications."""
    try:
        doc = json.loads(plaintext_json)
        generated_at = str(doc.get("generated_at") or "")
        count = len(doc.get("applications") or [])
    except (ValueError, AttributeError, TypeError):
        generated_at, count = "", 0
    if not generated_at:
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return generated_at, count


def encrypt_blob(plaintext_json: str, passphrase: str) -> dict:
    salt = os.urandom(SALT_BYTES)
    iv = os.urandom(IV_BYTES)
    key = _derive_key(passphrase, salt, ITERATIONS)
    packed = gzip.compress(plaintext_json.encode("utf-8"), mtime=0)
    ct = AESGCM(key).encrypt(iv, packed, None)
    generated_at, count = _plain_facts(plaintext_json)
    return {
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "iterations": ITERATIONS,
        "salt": _b64(salt),
        "cipher": "AES-256-GCM",
        "iv": _b64(iv),
        "compression": "gzip",
        "ct": _b64(ct),
        "generated_at": generated_at,
        "applications": count,
    }


def decrypt_blob(envelope: dict, passphrase: str) -> str:
    try:
        if envelope.get("v") != 1 or envelope.get("kdf") != "PBKDF2-SHA256" or envelope.get("cipher") != "AES-256-GCM":
            raise DecryptionError("unsupported envelope")
        if envelope.get("compression") != "gzip":
            raise DecryptionError("unsupported compression")
        salt = base64.b64decode(envelope["salt"])
        iv = base64.b64decode(envelope["iv"])
        ct = base64.b64decode(envelope["ct"])
        key = _derive_key(passphrase, salt, int(envelope["iterations"]))
        packed = AESGCM(key).decrypt(iv, ct, None)
        return gzip.decompress(packed).decode("utf-8")
    except DecryptionError:
        raise
    except (InvalidTag, KeyError, ValueError, TypeError, OSError, EOFError) as exc:
        raise DecryptionError("could not decrypt envelope") from exc


def write_envelope(path: str | Path, envelope: dict) -> None:
    Path(path).write_text(json.dumps(envelope, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def read_envelope(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
