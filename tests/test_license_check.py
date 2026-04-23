"""Tests for license-header scan on newly-added files."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from license_check import scan_license_headers, LicenseViolation  # noqa: E402
from repo_config import StandardsConfig  # noqa: E402
from risk import PRFile  # noqa: E402


def _mk_diff(path: str, lines: list) -> str:
    """Build a minimal unified diff that adds `path` with `lines`."""
    body = "\n".join(f"+{ln}" for ln in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


REQUIRED = "// Copyright {YEAR} Acme Corp. All rights reserved."


def test_added_file_with_matching_header_passes():
    diff = _mk_diff("src/foo.go", [
        "// Copyright 2026 Acme Corp. All rights reserved.",
        "",
        "package foo",
    ])
    config = StandardsConfig(license_header=REQUIRED)
    violations = scan_license_headers(diff, pr_files=None, config=config)
    assert violations == []


def test_added_file_missing_header_flagged():
    diff = _mk_diff("src/foo.go", [
        "package foo",
        "",
        "func Foo() {}",
    ])
    config = StandardsConfig(license_header=REQUIRED)
    violations = scan_license_headers(diff, pr_files=None, config=config)
    assert len(violations) == 1
    assert isinstance(violations[0], LicenseViolation)
    assert violations[0].file == "src/foo.go"
    # Uniform `.subject` across P3 violation dataclasses — lets the
    # analyzer's finding renderers stay table-driven.
    assert violations[0].subject == violations[0].file


def test_year_placeholder_any_4_digit_year_accepted():
    for year in ("2024", "2025", "2026", "9999"):
        diff = _mk_diff(f"src/{year}.go", [
            f"// Copyright {year} Acme Corp. All rights reserved.",
            "package foo",
        ])
        config = StandardsConfig(license_header=REQUIRED)
        assert scan_license_headers(diff, pr_files=None, config=config) == [], year


def test_applies_to_filter_skips_non_matching_paths():
    diff = _mk_diff("docs/README.md", [
        "# My project",
        "No header here.",
    ])
    config = StandardsConfig(
        license_header=REQUIRED,
        license_applies_to=["**/*.go", "**/*.py"],
    )
    # docs/README.md doesn't match either glob → skipped
    assert scan_license_headers(diff, pr_files=None, config=config) == []


def test_applies_to_filter_includes_matching_paths():
    diff = _mk_diff("src/foo.go", [
        "package foo",
    ])
    config = StandardsConfig(
        license_header=REQUIRED,
        license_applies_to=["**/*.go"],
    )
    violations = scan_license_headers(diff, pr_files=None, config=config)
    assert len(violations) == 1


def test_exemptions_filter_skips_exempted_paths():
    diff = _mk_diff("vendor/third_party/foo.go", [
        "package third",
    ])
    config = StandardsConfig(
        license_header=REQUIRED,
        license_exemptions=["vendor/**"],
    )
    assert scan_license_headers(diff, pr_files=None, config=config) == []


def test_binary_file_skipped():
    # Binary content = lines that contain NUL bytes. The diff parser will
    # include them as added lines; the scanner should detect and skip.
    binary_line = "\x00\x01\x02 binary junk"
    diff = _mk_diff("src/image.png", [binary_line])
    config = StandardsConfig(license_header=REQUIRED)
    assert scan_license_headers(diff, pr_files=None, config=config) == []


def test_empty_config_no_header_required():
    diff = _mk_diff("src/foo.go", ["package foo"])
    config = StandardsConfig()  # license_header is empty by default
    assert scan_license_headers(diff, pr_files=None, config=config) == []


def test_multi_file_diff_each_file_checked():
    diff = (
        _mk_diff("src/a.go", ["// Copyright 2026 Acme Corp. All rights reserved.", "package a"])
        + _mk_diff("src/b.go", ["package b"])  # missing header
        + _mk_diff("src/c.go", ["// Copyright 2020 Acme Corp. All rights reserved.", "package c"])
    )
    config = StandardsConfig(license_header=REQUIRED)
    violations = scan_license_headers(diff, pr_files=None, config=config)
    assert [v.file for v in violations] == ["src/b.go"]


def test_modified_file_not_flagged_via_pr_files():
    # PR-files metadata says "modified", so even if the added lines don't
    # contain the header, we shouldn't flag — only NEW files get checked.
    diff = (
        "diff --git a/src/existing.go b/src/existing.go\n"
        "--- a/src/existing.go\n"
        "+++ b/src/existing.go\n"
        "@@ -1,2 +1,3 @@\n"
        " package foo\n"
        " \n"
        "+func Bar() {}\n"
    )
    config = StandardsConfig(license_header=REQUIRED)
    pr_files = [PRFile(filename="src/existing.go", status="modified")]
    assert scan_license_headers(diff, pr_files=pr_files, config=config) == []


def test_added_file_flagged_via_pr_files_status():
    diff = _mk_diff("src/new.go", ["package new"])
    config = StandardsConfig(license_header=REQUIRED)
    pr_files = [PRFile(filename="src/new.go", status="added")]
    violations = scan_license_headers(diff, pr_files=pr_files, config=config)
    assert len(violations) == 1
    assert violations[0].file == "src/new.go"


def test_renamed_file_not_flagged_via_pr_files():
    # A renamed file is not "newly added" — it was already in the tree.
    # Even if its added lines lack the header (e.g. rewrite during rename),
    # the license scan must not flag it.
    diff = _mk_diff("src/renamed.go", ["package renamed"])
    config = StandardsConfig(license_header=REQUIRED)
    pr_files = [PRFile(filename="src/renamed.go", status="renamed")]
    assert scan_license_headers(diff, pr_files=pr_files, config=config) == []


def test_pr_files_missing_entry_treated_as_not_new():
    # If pr_files is supplied but the filename isn't in it, the scanner
    # should not flag — the diff might include a file the caller has
    # intentionally scoped out.
    diff = _mk_diff("src/unknown.go", ["package unknown"])
    config = StandardsConfig(license_header=REQUIRED)
    pr_files = [PRFile(filename="src/other.go", status="added")]
    assert scan_license_headers(diff, pr_files=pr_files, config=config) == []


def test_pr_files_none_uses_new_file_mode_marker_fallback():
    # When pr_files is None, the diff-text `new file mode` heuristic is
    # the fallback. This is the current primary path from analyze_pr prior
    # to fix A; keep covered so we don't regress it after signature change.
    diff = _mk_diff("src/new.go", ["package new"])
    config = StandardsConfig(license_header=REQUIRED)
    violations = scan_license_headers(diff, pr_files=None, config=config)
    assert len(violations) == 1


def test_header_longer_than_2kb_truncated_but_still_works():
    # Header text should be truncated to 2KB at _sanitize-time, but the
    # scan itself should still work on long added content.
    long_header = "// " + "x" * 3000  # 3KB of content
    diff = _mk_diff("src/foo.go", [long_header, "package foo"])
    config = StandardsConfig(license_header="// expected header")
    # Missing "expected header" → flagged
    violations = scan_license_headers(diff, pr_files=None, config=config)
    assert len(violations) == 1


def test_exemptions_take_priority_over_applies_to():
    # File matches applies_to but ALSO matches exemptions → skipped.
    diff = _mk_diff("src/generated/auto.go", ["package auto"])
    config = StandardsConfig(
        license_header=REQUIRED,
        license_applies_to=["**/*.go"],
        license_exemptions=["**/generated/**"],
    )
    assert scan_license_headers(diff, pr_files=None, config=config) == []


def test_year_placeholder_survives_literal_nul_in_header():
    """Round-3 FIX 4: `_build_header_regex` previously used a NUL-wrapped
    sentinel (`\\x00YEAR\\x00`) and relied on the CPython impl detail that
    `re.escape` does not escape NUL. The sentinel is now an ASCII-alnum
    token. Confirm that a header carrying literal NUL bytes doesn't
    confuse the `{YEAR}` substitution.

    In practice, `_sanitize_header_text` strips NUL via `_CONTROL_CHARS`
    before any header ever reaches the regex builder — but this test
    exercises the builder directly to lock in the invariant that the
    substitution is independent of what else is in the header text."""
    from license_check import _build_header_regex

    header_with_nul = "// Copyright {YEAR} Acme \x00 Corp."
    pattern = _build_header_regex(header_with_nul)

    # A real copyright line with literal NUL at the matching position
    # must still satisfy the pattern.
    assert pattern.match("// Copyright 2026 Acme \x00 Corp.") is not None
    # And the `{YEAR}` slot still only accepts 4 digits — if our sentinel
    # collision allowed non-digit content to sneak through, this would fail.
    assert pattern.match("// Copyright YEAR Acme \x00 Corp.") is None


def test_year_placeholder_rejects_non_numeric():
    diff = _mk_diff("src/foo.go", [
        "// Copyright YEAR Acme Corp. All rights reserved.",
        "package foo",
    ])
    config = StandardsConfig(license_header=REQUIRED)
    # Literal "YEAR" (not 4 digits) should NOT satisfy the {YEAR} placeholder.
    violations = scan_license_headers(diff, pr_files=None, config=config)
    assert len(violations) == 1


def test_multi_line_header_matched():
    multi_line_header = (
        "// Copyright {YEAR} Acme Corp.\n"
        "// Licensed under MIT."
    )
    diff = _mk_diff("src/foo.go", [
        "// Copyright 2026 Acme Corp.",
        "// Licensed under MIT.",
        "",
        "package foo",
    ])
    config = StandardsConfig(license_header=multi_line_header)
    assert scan_license_headers(diff, pr_files=None, config=config) == []


# --------------------------------------------------------------------------
# Fix E — trailing newline in header text
# --------------------------------------------------------------------------


def test_trailing_newline_in_header_does_not_add_phantom_line():
    # YAML block-scalar `license_header: |\n    // Copyright\n` would carry
    # a trailing `\n` that creates a phantom empty required line. The scan
    # must tolerate trailing newlines gracefully.
    header_with_trailing_nl = "// Copyright {YEAR} Acme Corp.\n"
    diff = _mk_diff("src/foo.go", [
        "// Copyright 2026 Acme Corp.",
        "package foo",
    ])
    config = StandardsConfig(license_header=header_with_trailing_nl)
    # The single-line header with trailing nl should match a file whose
    # first line matches — no phantom empty required line.
    assert scan_license_headers(diff, pr_files=None, config=config) == []


# --------------------------------------------------------------------------
# Fix F — BOM + lone-CR stripping in header match
# --------------------------------------------------------------------------


def test_file_with_utf8_bom_on_first_line_matches_plain_header():
    # Files from Windows editors sometimes include a UTF-8 BOM (`﻿`)
    # on the first line. The header match should strip it before comparing.
    diff = _mk_diff("src/foo.go", [
        "﻿// Copyright 2026 Acme Corp.",
        "package foo",
    ])
    config = StandardsConfig(license_header="// Copyright {YEAR} Acme Corp.")
    assert scan_license_headers(diff, pr_files=None, config=config) == []


def test_bom_only_stripped_from_first_line_not_subsequent():
    # A BOM mid-file is bizarre and should not pass — only the first line
    # gets the BOM-strip treatment.
    diff = _mk_diff("src/foo.go", [
        "// Copyright 2026 Acme Corp.",
        "﻿// Licensed under MIT.",  # BOM on line 2, should NOT match
        "",
    ])
    config = StandardsConfig(license_header=(
        "// Copyright {YEAR} Acme Corp.\n"
        "// Licensed under MIT."
    ))
    violations = scan_license_headers(diff, pr_files=None, config=config)
    assert len(violations) == 1


def test_file_with_lone_cr_in_header_lines_still_matches():
    # `git diff` normally strips trailing CR but belt-and-suspenders: a
    # line ending with `\r` (`\r\n` preserved somehow) should still match
    # a plain header line.
    diff = (
        "diff --git a/src/foo.go b/src/foo.go\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/foo.go\n"
        "@@ -0,0 +1,2 @@\n"
        "+// Copyright 2026 Acme Corp.\r\n"
        "+package foo\n"
    )
    config = StandardsConfig(license_header="// Copyright {YEAR} Acme Corp.")
    assert scan_license_headers(diff, pr_files=None, config=config) == []


# --------------------------------------------------------------------------
# Fix P — _looks_binary scans all added lines for NUL, not just first 40
# --------------------------------------------------------------------------


def test_binary_detection_scans_beyond_first_40_lines():
    # A file with 50 plain lines followed by a NUL-containing line is still
    # binary and must be skipped. Previous behavior (first-40-only scan)
    # would flag it as "missing header".
    lines = [f"line{i}" for i in range(50)] + ["\x00 binary junk"]
    diff = _mk_diff("src/image.png", lines)
    config = StandardsConfig(license_header=REQUIRED)
    assert scan_license_headers(diff, pr_files=None, config=config) == []


# --------------------------------------------------------------------------
# Fix I — license_header_file symlink traversal refusal
# --------------------------------------------------------------------------


def test_license_header_file_symlink_refused(tmp_path, capsys):
    # A symlink inside the repo pointing at a host-sensitive path must be
    # refused by _resolve_license_header_file (via safe_open_in_repo).
    import repo_config

    # Create a sensitive target outside the repo
    sensitive = tmp_path / "outside_secret.txt"
    sensitive.write_text("outside-host-secret\n")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    # Inside the repo, create a symlink named HEADER.txt -> sensitive
    symlink_path = repo_dir / "HEADER.txt"
    symlink_path.symlink_to(sensitive)

    # Call the resolver directly
    result = repo_config._resolve_license_header_file(str(repo_dir), "HEADER.txt")
    assert result == ""
    captured = capsys.readouterr()
    # Should log refusal or safe-open failure.
    assert "[seneschal]" in captured.err or "[post_merge]" in captured.err
