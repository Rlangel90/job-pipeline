"""The envelope format is a contract with the browser, so these tests pin it literally."""
import base64
import gzip
import json

import pytest

from scanner import crypto

PLAIN = json.dumps({"version": 1, "generated_at": "2026-10-14T18:00:00Z", "applications": [{"id": "a"}, {"id": "b"}]})


def test_round_trip():
    env = crypto.encrypt_blob(PLAIN, "correct horse")
    assert crypto.decrypt_blob(env, "correct horse") == PLAIN


def test_envelope_shape_matches_contract():
    env = crypto.encrypt_blob(PLAIN, "pw")
    assert set(env) == {"v", "kdf", "iterations", "salt", "cipher", "iv", "compression", "ct", "generated_at", "applications"}
    assert env["v"] == 1
    assert env["kdf"] == "PBKDF2-SHA256"
    assert env["iterations"] == 600000
    assert env["cipher"] == "AES-256-GCM"
    assert env["compression"] == "gzip"
    assert len(base64.b64decode(env["salt"])) == 16
    assert len(base64.b64decode(env["iv"])) == 12
    assert env["generated_at"] == "2026-10-14T18:00:00Z"
    assert env["applications"] == 2


def test_ciphertext_includes_gcm_tag_and_gzip_payload():
    env = crypto.encrypt_blob(PLAIN, "pw")
    ct = base64.b64decode(env["ct"])
    assert len(ct) > 16
    # Independent decryption with the primitives the browser would use.
    import hashlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = hashlib.pbkdf2_hmac("sha256", b"pw", base64.b64decode(env["salt"]), 600000, 32)
    raw = AESGCM(key).decrypt(base64.b64decode(env["iv"]), ct, None)
    assert gzip.decompress(raw).decode("utf-8") == PLAIN


def test_fresh_salt_and_iv_every_run():
    a = crypto.encrypt_blob(PLAIN, "pw")
    b = crypto.encrypt_blob(PLAIN, "pw")
    assert a["salt"] != b["salt"]
    assert a["iv"] != b["iv"]
    assert a["ct"] != b["ct"]


def test_wrong_passphrase_raises():
    env = crypto.encrypt_blob(PLAIN, "right")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_blob(env, "wrong")


def test_unicode_passphrase_and_payload():
    text = json.dumps({"applications": [], "note": "caf\u00e9 \u2014 r\u00e9sum\u00e9"}, ensure_ascii=False)
    env = crypto.encrypt_blob(text, "p\u00e4ss\u00f6")
    assert crypto.decrypt_blob(env, "p\u00e4ss\u00f6") == text


def test_unsupported_envelope_version_is_rejected():
    env = crypto.encrypt_blob(PLAIN, "pw")
    env["v"] = 2
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_blob(env, "pw")


def test_envelope_file_is_pretty_and_sorted(tmp_path):
    env = crypto.encrypt_blob(PLAIN, "pw")
    path = tmp_path / "x.enc"
    crypto.write_envelope(path, env)
    text = path.read_text(encoding="utf-8")
    assert text.startswith('{\n "applications": 2,')
    assert json.loads(text) == env
    assert crypto.read_envelope(path) == env
