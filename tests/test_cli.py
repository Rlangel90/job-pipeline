"""The command line: exit codes, privacy rules, and the three subcommands."""
from __future__ import annotations

import json

import pytest

from scanner import __main__ as cli
from scanner import build, crypto
from tests.helpers import ALL_FIXTURES, FIXTURES, write_mbox

PASS = "test-passphrase"


@pytest.fixture(autouse=True)
def fast_kdf(monkeypatch):
    """Full-strength PBKDF2 is tested in test_crypto; a cheap round count keeps the CLI tests quick."""
    monkeypatch.setattr(crypto, "ITERATIONS", 1000)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DATA_PASSPHRASE", PASS)
    monkeypatch.setenv("GMAIL_ADDRESS", "owner@example.com")
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("CI", raising=False)


@pytest.fixture
def mbox(tmp_path):
    path = tmp_path / "mail.mbox"
    write_mbox(path, ALL_FIXTURES)
    return path


def build_args(mbox, out, *extra):
    return ["build", "--since", "2026-10-01", "--mbox", str(mbox), "--out", str(out), "--now", "2026-10-14T18:00:00Z", *extra]


def test_build_from_mbox_writes_a_decryptable_valid_blob(env, mbox, tmp_path, capsys):
    out = tmp_path / "pipeline.enc"
    assert cli.main(build_args(mbox, out)) == 0
    envelope = json.loads(out.read_text(encoding="utf-8"))
    assert envelope["applications"] == 9 and envelope["generated_at"] == "2026-10-14T18:00:00Z"
    data = json.loads(crypto.decrypt_blob(envelope, PASS))
    assert build.validate(data) == []
    assert len(data["applications"]) == 9


def test_build_logs_counts_only(env, mbox, tmp_path, capsys):
    cli.main(build_args(mbox, tmp_path / "p.enc"))
    err = capsys.readouterr().err
    assert "applications" in err and "9" in err
    for secret in ("Acme", "Brightloom", "Jane", "Withdrawing", "owner@example.com", "Offer letter"):
        assert secret not in err


def test_verbose_is_refused_in_ci(env, mbox, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CI", "true")
    out = tmp_path / "p.enc"
    assert cli.main(build_args(mbox, out, "--verbose")) == 1
    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1 and "verbose" in err.lower()
    assert not out.exists()


def test_verbose_prints_names_outside_ci(env, mbox, tmp_path, capsys):
    assert cli.main(build_args(mbox, tmp_path / "p.enc", "--verbose")) == 0
    err = capsys.readouterr().err
    assert "Acme Corp" in err


def test_missing_passphrase_is_fatal(env, mbox, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DATA_PASSPHRASE")
    assert cli.main(build_args(mbox, tmp_path / "p.enc")) == 1


def test_missing_imap_credentials_are_fatal_without_mbox(env, tmp_path, capsys):
    assert cli.main(["build", "--since", "2026-10-01", "--out", str(tmp_path / "p.enc")]) == 1


def test_missing_mbox_file_is_fatal(env, tmp_path):
    assert cli.main(build_args(tmp_path / "nope.mbox", tmp_path / "p.enc")) == 1


def test_bad_since_is_fatal(env, mbox, tmp_path):
    assert cli.main(["build", "--since", "yesterday", "--mbox", str(mbox), "--out", str(tmp_path / "p.enc")]) == 1


def test_overrides_are_applied_and_a_missing_file_is_fatal(env, mbox, tmp_path):
    out = tmp_path / "p.enc"
    assert cli.main(build_args(mbox, out, "--overrides", str(FIXTURES / "overrides_sample.yml"))) == 0
    data = json.loads(crypto.decrypt_blob(json.loads(out.read_text(encoding="utf-8")), PASS))
    assert "Stealth startup" in {a["company"] for a in data["applications"]}
    assert cli.main(build_args(mbox, out, "--overrides", str(tmp_path / "missing.yml"))) == 1


def test_previous_is_used_and_failures_are_skipped(env, mbox, tmp_path, capsys):
    first = tmp_path / "first.enc"
    cli.main(build_args(mbox, first))
    capsys.readouterr()
    assert cli.main(build_args(mbox, tmp_path / "second.enc", "--previous", str(first))) == 0
    assert "previous blob: used" in capsys.readouterr().err
    assert cli.main(build_args(mbox, tmp_path / "third.enc", "--previous", str(tmp_path / "absent.enc"))) == 0
    assert "previous blob: skipped" in capsys.readouterr().err
    (tmp_path / "broken.enc").write_text("not json", encoding="utf-8")
    assert cli.main(build_args(mbox, tmp_path / "fourth.enc", "--previous", str(tmp_path / "broken.enc"))) == 0
    other = crypto.encrypt_blob(json.dumps({"applications": []}), "different")
    crypto.write_envelope(tmp_path / "wrongpass.enc", other)
    assert cli.main(build_args(mbox, tmp_path / "fifth.enc", "--previous", str(tmp_path / "wrongpass.enc"))) == 0
    assert "previous blob: skipped" in capsys.readouterr().err


def test_plain_out(env, mbox, tmp_path):
    plain = tmp_path / "plain.json"
    assert cli.main(build_args(mbox, tmp_path / "p.enc", "--plain-out", str(plain))) == 0
    assert build.validate(json.loads(plain.read_text(encoding="utf-8"))) == []


def test_encryption_failure_exits_two(env, mbox, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cipher exploded")
    monkeypatch.setattr(crypto, "encrypt_blob", boom)
    assert cli.main(build_args(mbox, tmp_path / "p.enc")) == 2


def test_write_failure_exits_two(env, mbox, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not a directory", encoding="utf-8")
    assert cli.main(build_args(mbox, blocker / "p.enc")) == 2


def test_unexpected_error_prints_only_the_class_name_in_ci(env, mbox, tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("Subject: secret recruiter email")
    monkeypatch.setattr(build, "run_scan", boom)
    monkeypatch.setenv("CI", "true")
    assert cli.main(build_args(mbox, tmp_path / "p.enc")) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "secret" not in err


def test_unexpected_error_has_detail_outside_ci(env, mbox, tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("detail text")
    monkeypatch.setattr(build, "run_scan", boom)
    assert cli.main(build_args(mbox, tmp_path / "p.enc")) == 1
    assert "detail text" in capsys.readouterr().err


def test_validation_failure_prints_a_path_and_exits_one(env, mbox, tmp_path, monkeypatch, capsys):
    real = build.run_scan

    def broken(*a, **k):
        res = real(*a, **k)
        res.data["applications"][0]["source"] = "TOP-SECRET"
        return res
    monkeypatch.setattr(build, "run_scan", broken)
    assert cli.main(build_args(mbox, tmp_path / "p.enc")) == 1
    err = capsys.readouterr().err
    assert "/applications/0/source" in err and "TOP-SECRET" not in err


def test_demo_command(tmp_path, capsys):
    out = tmp_path / "demo.json"
    assert cli.main(["demo", "--out", str(out), "--seed", "7"]) == 0
    assert build.validate(json.loads(out.read_text(encoding="utf-8"))) == []
    again = tmp_path / "again.json"
    cli.main(["demo", "--out", str(again), "--seed", "7"])
    assert out.read_bytes() == again.read_bytes()


def test_decrypt_command(env, mbox, tmp_path):
    enc, plain = tmp_path / "p.enc", tmp_path / "x.json"
    cli.main(build_args(mbox, enc))
    assert cli.main(["decrypt", "--in", str(enc), "--out", str(plain)]) == 0
    assert build.validate(json.loads(plain.read_text(encoding="utf-8"))) == []


def test_decrypt_with_wrong_passphrase_fails(env, mbox, tmp_path, monkeypatch, capsys):
    enc = tmp_path / "p.enc"
    cli.main(build_args(mbox, enc))
    monkeypatch.setenv("DATA_PASSPHRASE", "wrong")
    assert cli.main(["decrypt", "--in", str(enc), "--out", str(tmp_path / "x.json")]) == 1
    assert not (tmp_path / "x.json").exists()


def test_imap_path_uses_credentials_from_the_environment(env, tmp_path, monkeypatch):
    from scanner import mail
    seen = {}

    def fake_fetch(address, password, since, logger):
        seen.update(address=address, password=password, since=since)
        return []
    monkeypatch.setattr(mail, "fetch_imap", fake_fetch)
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw")
    assert cli.main(["build", "--since", "2026-10-01", "--out", str(tmp_path / "p.enc")]) == 0
    assert seen["address"] == "owner@example.com" and seen["password"] == "app-pw" and str(seen["since"]) == "2026-10-01"
    data = json.loads(crypto.decrypt_blob(json.loads((tmp_path / "p.enc").read_text(encoding="utf-8")), PASS))
    assert data["applications"] == [] and data["stats"]["applications"] == 0
