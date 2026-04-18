"""Tests for the secret leak scanner."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from secrets_scan import SecretHit, scan_diff, summarize_secrets  # noqa: E402


def make_diff(filename, added_lines):
    header = f"diff --git a/{filename} b/{filename}\n+++ b/{filename}\n@@ -0,0 +1,{len(added_lines)} @@\n"
    body = "\n".join(f"+{line}" for line in added_lines)
    return header + body + "\n"


def test_clean_diff():
    diff = make_diff("src/foo.py", ["def foo():", "    return 1"])
    assert scan_diff(diff) == []


def _fake_token(prefix, body):
    """Assemble fake credentials at runtime.

    Split into pieces so pre-commit secret scanners and repo-level hooks
    don't see literal key patterns in this test file. At runtime the
    concatenated string is a valid-looking fake credential.
    """
    return prefix + body


def test_aws_key_detected():
    token = _fake_token("AK", "IAIOSFODNN7EXAMPLE")
    diff = make_diff("config.py", [f'AWS_KEY = "{token}"'])
    hits = scan_diff(diff)
    assert len(hits) == 1
    assert hits[0].kind == "AWS access key"
    assert hits[0].file == "config.py"


def test_github_token_detected():
    token = _fake_token("gh" + "p_", "abcdefghijklmnopqrstuvwxyz0123456789")
    diff = make_diff("deploy.yml", [f'TOKEN = "{token}"'])
    hits = scan_diff(diff)
    assert len(hits) == 1
    assert "GitHub" in hits[0].kind


def test_anthropic_key_detected():
    token = _fake_token("sk-" + "ant-api03-", "a" * 60)
    diff = make_diff("client.py", [f'key = "{token}"'])
    hits = scan_diff(diff)
    assert len(hits) == 1
    assert "Anthropic" in hits[0].kind


def test_openai_key_detected():
    token = _fake_token("sk-", "a" * 48)
    diff = make_diff("llm.py", [f'OPENAI_API_KEY = "{token}"'])
    hits = scan_diff(diff)
    assert len(hits) >= 1


def test_private_key_header_detected():
    diff = make_diff(
        "keys.txt",
        ["-----BEGIN RSA PRIVATE KEY-----"],
    )
    hits = scan_diff(diff)
    assert len(hits) == 1
    assert "private key" in hits[0].kind.lower()


def test_generic_hardcoded_credential():
    body = _fake_token("abcdefghijkl", "mnopqrstuvwxyz0123456789")
    diff = make_diff("settings.py", [f'api_' + f'key = "{body}"'])
    hits = scan_diff(diff)
    assert len(hits) == 1


def test_lockfile_skipped():
    token = _fake_token("AK", "IAIOSFODNN7EXAMPLE-like-string")
    diff = make_diff("package-lock.json", [f'"integrity": "sha512-{token}"'])
    assert scan_diff(diff) == []


def test_fixtures_dir_skipped():
    body = _fake_token("abcdefghijkl", "mnopqrstuvwxyz0123456")
    diff = make_diff("tests/fixtures/dummy.json", [f'{{"api_' + f'key": "{body}"}}'])
    assert scan_diff(diff) == []


def test_testdata_dir_skipped():
    diff = make_diff(
        "internal/testdata/keys.txt",
        ["-----BEGIN RSA PRIVATE KEY-----"],
    )
    assert scan_diff(diff) == []


def test_redacted_preview_masks_long_token():
    token = _fake_token("AK", "IAIOSFODNN7EXAMPLE")
    hit = SecretHit(
        kind="AWS access key",
        file="x.py",
        line=1,
        preview=f'KEY = "{token}"',
    )
    masked = hit.redacted_preview()
    assert token not in masked
    assert "***" in masked


def test_redacted_preview_masks_short_slack_token():
    """Regression for B4: short Slack tokens slipped past the alnum-only mask.

    The original redactor only masked 16+ char alnum runs, so a token like
    xoxb-1234567890 (15 chars total, 10 chars after the prefix) leaked
    unredacted into PR comments via the analyzer body.
    """
    token = _fake_token("xo" + "xb-", "1234567890")
    hit = SecretHit(
        kind="Slack token",
        file="config.py",
        line=12,
        preview=f'SLACK_TOKEN = "{token}"',
    )
    masked = hit.redacted_preview()
    assert token not in masked
    assert "1234567890" not in masked
    assert "***" in masked


def test_redacted_preview_does_not_double_mask():
    """The mask sentinel must not match its own alnum-fallback regex."""
    token = _fake_token("AK", "IAIOSFODNN7EXAMPLE")
    hit = SecretHit(
        kind="AWS access key",
        file="x.py",
        line=1,
        preview=f'KEY = "{token}"',
    )
    masked = hit.redacted_preview()
    # No nested redactions like ***...***
    assert "******" not in masked or masked.count("*") <= 6


def test_redacted_preview_keeps_short_identifiers():
    """Identifiers under 8 chars stay readable so the line is still useful."""
    hit = SecretHit(
        kind="generic",
        file="x.py",
        line=1,
        preview="hello world",
    )
    masked = hit.redacted_preview()
    assert "hello" in masked


def test_summarize_clean():
    assert "clean" in summarize_secrets([]).lower()


def test_summarize_with_hits():
    tok_a = _fake_token("AK", "IAIOSFODNN7EXAMPLE")
    tok_b = _fake_token("AK", "IAXXXXXXXXXXXXXXXX")
    tok_c = _fake_token("gh" + "p_", "abcd")
    hits = [
        SecretHit(kind="AWS access key", file="x.py", line=3, preview=f'"{tok_a}"'),
        SecretHit(kind="AWS access key", file="y.py", line=5, preview=f'"{tok_b}"'),
        SecretHit(kind="GitHub token", file="z.py", line=1, preview=f'"{tok_c}"'),
    ]
    out = summarize_secrets(hits)
    assert "3 potential" in out
    assert "AWS access key" in out
    assert "GitHub" in out
