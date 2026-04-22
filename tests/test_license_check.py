"""Tests for license-header scan on newly-added files."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from license_check import scan_license_headers, LicenseViolation  # noqa: E402
from repo_config import StandardsConfig  # noqa: E402


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
    pr_files = [{"filename": "src/existing.go", "status": "modified"}]
    assert scan_license_headers(diff, pr_files=pr_files, config=config) == []


def test_added_file_flagged_via_pr_files_status():
    diff = _mk_diff("src/new.go", ["package new"])
    config = StandardsConfig(license_header=REQUIRED)
    pr_files = [{"filename": "src/new.go", "status": "added"}]
    violations = scan_license_headers(diff, pr_files=pr_files, config=config)
    assert len(violations) == 1
    assert violations[0].file == "src/new.go"


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
