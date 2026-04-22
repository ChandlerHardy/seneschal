"""Tests for post_merge.changelog: PURE conventional-commit classification + Keep-a-Changelog formatting."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from post_merge.changelog import (  # noqa: E402
    _KEEP_A_CHANGELOG_HEADER,
    classify_prefix,
    format_unreleased_entry,
    insert_unreleased_entry,
)


# --------------------------------------------------------------------------
# classify_prefix
# --------------------------------------------------------------------------


def test_classify_prefix_feat():
    assert classify_prefix("feat: add new endpoint") == "feat"


def test_classify_prefix_fix():
    assert classify_prefix("fix: handle nil pointer") == "fix"


def test_classify_prefix_with_scope():
    assert classify_prefix("feat(api): add endpoint") == "feat"
    assert classify_prefix("fix(parser): handle empty") == "fix"


def test_classify_prefix_perf():
    assert classify_prefix("perf: speed up parsing") == "perf"


def test_classify_prefix_refactor():
    assert classify_prefix("refactor: extract helper") == "refactor"


def test_classify_prefix_chore():
    assert classify_prefix("chore: bump deps") == "chore"


def test_classify_prefix_docs():
    assert classify_prefix("docs: update readme") == "docs"


def test_classify_prefix_test():
    assert classify_prefix("test: add edge cases") == "test"


def test_classify_prefix_build():
    assert classify_prefix("build: bump go version") == "build"


def test_classify_prefix_ci():
    assert classify_prefix("ci: tweak workflow") == "ci"


def test_classify_prefix_style():
    assert classify_prefix("style: gofmt") == "style"


def test_classify_prefix_unprefixed_returns_none():
    assert classify_prefix("Add new endpoint") is None


def test_classify_prefix_garbage_returns_none():
    assert classify_prefix("") is None
    assert classify_prefix("   ") is None
    assert classify_prefix("???") is None


def test_classify_prefix_handles_breaking_marker():
    # "feat!: ..." still classifies as feat
    assert classify_prefix("feat!: drop python 3.8") == "feat"


def test_classify_prefix_unknown_prefix_returns_none():
    assert classify_prefix("notatype: stuff") is None


# --------------------------------------------------------------------------
# format_unreleased_entry
# --------------------------------------------------------------------------


def test_format_unreleased_entry_strips_prefix():
    entry = format_unreleased_entry(42, "feat: add new endpoint", "https://github.com/o/r/pull/42")
    assert entry == "- add new endpoint ([#42](https://github.com/o/r/pull/42))"


def test_format_unreleased_entry_strips_scope_prefix():
    entry = format_unreleased_entry(7, "fix(api): broken", "https://example.com/7")
    assert "broken" in entry
    assert "fix(" not in entry
    assert "[#7]" in entry


def test_format_unreleased_entry_unprefixed():
    entry = format_unreleased_entry(3, "Just a regular title", "https://x/3")
    assert entry == "- Just a regular title ([#3](https://x/3))"


def test_format_unreleased_entry_strips_breaking_marker():
    entry = format_unreleased_entry(9, "feat!: drop python 3.8", "https://x/9")
    assert "drop python 3.8" in entry
    assert "feat" not in entry


# --------------------------------------------------------------------------
# insert_unreleased_entry — basics
# --------------------------------------------------------------------------


def test_insert_into_empty_changelog_creates_header():
    entry = "- new thing ([#1](https://x/1))"
    out = insert_unreleased_entry("", entry, "feat")
    assert _KEEP_A_CHANGELOG_HEADER.strip() in out
    assert "## [Unreleased]" in out
    assert "### Added" in out
    assert entry in out


def test_insert_creates_unreleased_when_missing():
    existing = _KEEP_A_CHANGELOG_HEADER + "\n## [0.1.0] - 2026-01-01\n\n### Added\n- old thing\n"
    entry = "- new ([#2](https://x/2))"
    out = insert_unreleased_entry(existing, entry, "feat")
    assert "## [Unreleased]" in out
    # Unreleased must come before the existing version block
    assert out.index("## [Unreleased]") < out.index("## [0.1.0]")
    assert entry in out


def test_insert_feat_goes_under_added():
    existing = _KEEP_A_CHANGELOG_HEADER + "\n## [Unreleased]\n"
    entry = "- shiny ([#5](https://x/5))"
    out = insert_unreleased_entry(existing, entry, "feat")
    added_idx = out.index("### Added")
    entry_idx = out.index(entry)
    assert added_idx < entry_idx


def test_insert_fix_goes_under_fixed():
    existing = _KEEP_A_CHANGELOG_HEADER + "\n## [Unreleased]\n"
    entry = "- bug ([#5](https://x/5))"
    out = insert_unreleased_entry(existing, entry, "fix")
    assert "### Fixed" in out
    fixed_idx = out.index("### Fixed")
    entry_idx = out.index(entry)
    assert fixed_idx < entry_idx


def test_insert_refactor_goes_under_changed():
    existing = _KEEP_A_CHANGELOG_HEADER + "\n## [Unreleased]\n"
    entry = "- ref ([#6](https://x/6))"
    out = insert_unreleased_entry(existing, entry, "refactor")
    assert "### Changed" in out
    assert entry in out


def test_insert_perf_goes_under_changed():
    existing = _KEEP_A_CHANGELOG_HEADER + "\n## [Unreleased]\n"
    entry = "- perf ([#7](https://x/7))"
    out = insert_unreleased_entry(existing, entry, "perf")
    assert "### Changed" in out
    assert entry in out


def test_insert_breaking_goes_under_removed():
    existing = _KEEP_A_CHANGELOG_HEADER + "\n## [Unreleased]\n"
    entry = "- removed thing ([#8](https://x/8))"
    out = insert_unreleased_entry(existing, entry, "BREAKING")
    assert "### Removed" in out
    assert entry in out


def test_insert_preserves_existing_entries():
    existing = (
        _KEEP_A_CHANGELOG_HEADER
        + "\n## [Unreleased]\n\n### Added\n- earlier feature ([#1](https://x/1))\n"
    )
    entry = "- new feature ([#2](https://x/2))"
    out = insert_unreleased_entry(existing, entry, "feat")
    assert "earlier feature" in out
    assert "new feature" in out


def test_insert_creates_subsection_when_other_subsections_exist():
    existing = (
        _KEEP_A_CHANGELOG_HEADER
        + "\n## [Unreleased]\n\n### Added\n- thing ([#1](https://x/1))\n"
    )
    entry = "- bug ([#2](https://x/2))"
    out = insert_unreleased_entry(existing, entry, "fix")
    assert "### Added" in out
    assert "### Fixed" in out
    assert "thing" in out
    assert "bug" in out


def test_insert_chore_goes_under_changed_default():
    existing = _KEEP_A_CHANGELOG_HEADER + "\n## [Unreleased]\n"
    entry = "- chore ([#9](https://x/9))"
    out = insert_unreleased_entry(existing, entry, "chore")
    # chore/docs/test/build/ci/style fall back to Changed (or are filtered out)
    assert entry in out
