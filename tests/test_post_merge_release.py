"""Tests for post_merge.release: semver bump + release-notes rendering."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from post_merge.release import (  # noqa: E402
    bump_kind,
    next_version,
    render_release_notes,
)


# --------------------------------------------------------------------------
# bump_kind
# --------------------------------------------------------------------------


def test_bump_kind_breaking_change_marker_is_major():
    lines = ["- thing ([#1](x))", "BREAKING CHANGE: drop py3.8"]
    assert bump_kind(lines) == "major"


def test_bump_kind_bang_prefix_is_major():
    lines = ["- feat!: drop py3.8 ([#1](x))"]
    assert bump_kind(lines) == "major"


def test_bump_kind_feat_only_is_minor():
    lines = ["- feat: new feature ([#1](x))"]
    assert bump_kind(lines) == "minor"


def test_bump_kind_fix_only_is_patch():
    lines = ["- fix: bug ([#1](x))"]
    assert bump_kind(lines) == "patch"


def test_bump_kind_mixed_feat_and_fix_is_minor():
    lines = ["- feat: x ([#1](x))", "- fix: y ([#2](y))"]
    assert bump_kind(lines) == "minor"


def test_bump_kind_breaking_beats_feat():
    lines = ["- feat: x ([#1](x))", "- feat!: drop ([#2](y))"]
    assert bump_kind(lines) == "major"


def test_bump_kind_empty_is_patch():
    assert bump_kind([]) == "patch"


def test_bump_kind_no_conventional_prefix_is_patch():
    assert bump_kind(["- some random change ([#1](x))"]) == "patch"


# --------------------------------------------------------------------------
# next_version
# --------------------------------------------------------------------------


def test_next_version_minor_bump():
    assert next_version("0.2.0", "minor") == "0.3.0"


def test_next_version_patch_bump():
    assert next_version("0.2.5", "patch") == "0.2.6"


def test_next_version_major_bump():
    assert next_version("0.2.5", "major") == "1.0.0"


def test_next_version_major_bump_resets_minor_and_patch():
    assert next_version("1.5.7", "major") == "2.0.0"


def test_next_version_minor_resets_patch():
    assert next_version("1.5.7", "minor") == "1.6.0"


def test_next_version_preserves_v_prefix():
    assert next_version("v0.2.0", "minor") == "v0.3.0"
    assert next_version("v1.0.0", "major") == "v2.0.0"
    assert next_version("v0.0.1", "patch") == "v0.0.2"


def test_next_version_invalid_raises():
    import pytest
    with pytest.raises(ValueError):
        next_version("not-a-version", "minor")


# --------------------------------------------------------------------------
# render_release_notes
# --------------------------------------------------------------------------


def test_render_release_notes_replaces_unreleased_header():
    unreleased = "## [Unreleased]\n\n### Added\n- thing ([#1](x))\n"
    out = render_release_notes(unreleased, "0.3.0", "2026-04-21")
    assert "## [0.3.0] - 2026-04-21" in out
    assert "## [Unreleased]" not in out


def test_render_release_notes_preserves_subsections():
    unreleased = (
        "## [Unreleased]\n\n"
        "### Added\n- new thing ([#1](x))\n\n"
        "### Fixed\n- bug ([#2](y))\n"
    )
    out = render_release_notes(unreleased, "1.0.0", "2026-04-21")
    assert "### Added" in out
    assert "### Fixed" in out
    assert "new thing" in out
    assert "bug" in out


def test_render_release_notes_does_not_mutate_input():
    unreleased = "## [Unreleased]\n\n### Added\n- thing\n"
    original = unreleased
    _ = render_release_notes(unreleased, "0.3.0", "2026-04-21")
    assert unreleased == original


def test_render_release_notes_with_v_prefix():
    unreleased = "## [Unreleased]\n\n### Added\n- thing\n"
    out = render_release_notes(unreleased, "v1.2.0", "2026-04-21")
    assert "## [v1.2.0] - 2026-04-21" in out
