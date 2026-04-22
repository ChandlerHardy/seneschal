"""Tests for review_store: on-disk persistence of posted reviews."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review_store  # noqa: E402
from review_store import (  # noqa: E402
    ReviewRecord,
    get_repo_memory,
    get_review,
    last_review,
    list_reviews,
    mark_merged,
    save_review,
)


# --------------------------------------------------------------------------
# save_review / get_review roundtrip
# --------------------------------------------------------------------------


def test_save_and_get_review_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    path = save_review(
        "owner/repo",
        42,
        "APPROVE",
        "https://github.com/owner/repo/pull/42#pullrequestreview-1",
        "## Review\nLooks good.",
    )
    assert path.exists()
    rec = get_review("owner/repo", 42)
    assert rec is not None
    assert rec.repo == "owner/repo"
    assert rec.pr_number == 42
    assert rec.verdict == "APPROVE"
    assert rec.url.startswith("https://github.com")
    assert "Looks good" in rec.body


def test_save_overwrites_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("a/b", 1, "APPROVE", "", "first")
    save_review("a/b", 1, "REQUEST_CHANGES", "", "second")
    rec = get_review("a/b", 1)
    assert rec.body.strip() == "second"
    assert rec.verdict == "REQUEST_CHANGES"


def test_save_includes_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("a/b", 1, "APPROVE", "", "x", timestamp="2026-04-18T12:00:00Z")
    rec = get_review("a/b", 1)
    assert rec.timestamp == "2026-04-18T12:00:00Z"


def test_get_review_returns_none_for_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    assert get_review("a/b", 99) is None


def test_get_review_handles_corrupt_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    d = tmp_path / "a" / "b"
    d.mkdir(parents=True)
    (d / "5.md").write_text("no frontmatter here, just body")
    assert get_review("a/b", 5) is None


# --------------------------------------------------------------------------
# list_reviews
# --------------------------------------------------------------------------


def test_list_reviews_empty_when_no_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    assert list_reviews("unknown/repo") == []


def test_list_reviews_returns_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    for pr in [1, 5, 10, 3]:
        save_review("a/b", pr, "COMMENT", "", f"review {pr}")
    recs = list_reviews("a/b")
    assert [r.pr_number for r in recs] == [10, 5, 3, 1]


def test_list_reviews_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    for pr in range(20):
        save_review("a/b", pr + 1, "APPROVE", "", f"r{pr}")
    recs = list_reviews("a/b", limit=5)
    assert len(recs) == 5
    assert recs[0].pr_number == 20


def test_list_reviews_skips_non_pr_files(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    d = tmp_path / "a" / "b"
    d.mkdir(parents=True)
    (d / "README.md").write_text("not a review")
    (d / "notes.txt").write_text("also not")
    save_review("a/b", 7, "APPROVE", "", "real")
    recs = list_reviews("a/b")
    assert len(recs) == 1
    assert recs[0].pr_number == 7


# --------------------------------------------------------------------------
# last_review
# --------------------------------------------------------------------------


def test_last_review_returns_highest_pr(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("a/b", 3, "APPROVE", "", "three")
    save_review("a/b", 1, "COMMENT", "", "one")
    save_review("a/b", 7, "REQUEST_CHANGES", "", "seven")
    rec = last_review("a/b")
    assert rec.pr_number == 7


def test_last_review_none_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    assert last_review("a/b") is None


# --------------------------------------------------------------------------
# Repo slug validation (path-traversal defense)
# --------------------------------------------------------------------------


def test_save_rejects_path_traversal_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    import pytest
    with pytest.raises(ValueError):
        save_review("../etc/passwd", 1, "APPROVE", "", "hacked")


def test_save_rejects_nonstandard_slug(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    import pytest
    with pytest.raises(ValueError):
        save_review("no-slash", 1, "APPROVE", "", "x")
    with pytest.raises(ValueError):
        save_review("too/many/slashes", 1, "APPROVE", "", "x")


def test_save_rejects_invalid_pr_number(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    import pytest
    with pytest.raises(ValueError):
        save_review("a/b", 0, "APPROVE", "", "x")
    with pytest.raises(ValueError):
        save_review("a/b", -5, "APPROVE", "", "x")


# --------------------------------------------------------------------------
# get_repo_memory
# --------------------------------------------------------------------------


def test_get_repo_memory_reads_seneschal_memory(tmp_path):
    (tmp_path / ".seneschal-memory.md").write_text("# Rules\nUse tabs")
    result = get_repo_memory("a/b", str(tmp_path))
    assert "Use tabs" in result


def test_get_repo_memory_falls_back_to_legacy_name(tmp_path):
    (tmp_path / ".ch-code-reviewer-memory.md").write_text("# Legacy\nOld rules")
    result = get_repo_memory("a/b", str(tmp_path))
    assert "Old rules" in result


def test_get_repo_memory_prefers_new_name(tmp_path):
    (tmp_path / ".seneschal-memory.md").write_text("NEW")
    (tmp_path / ".ch-code-reviewer-memory.md").write_text("LEGACY")
    result = get_repo_memory("a/b", str(tmp_path))
    assert result == "NEW"


def test_get_repo_memory_empty_when_missing(tmp_path):
    assert get_repo_memory("a/b", str(tmp_path)) == ""


# --------------------------------------------------------------------------
# Frontmatter v2: head_sha / merged_at / followups_filed (P1)
# --------------------------------------------------------------------------


def test_save_review_with_head_sha_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review(
        "a/b",
        7,
        "APPROVE",
        "https://x/7",
        "body",
        head_sha="abc123def",
    )
    rec = get_review("a/b", 7)
    assert rec is not None
    assert rec.head_sha == "abc123def"


def test_save_review_default_head_sha_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("a/b", 1, "APPROVE", "", "body")
    rec = get_review("a/b", 1)
    assert rec.head_sha == ""
    assert rec.merged_at is None
    assert rec.followups_filed == []


def test_v1_frontmatter_still_parses(tmp_path, monkeypatch):
    """A review file without the new v2 fields must still parse with defaults."""
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    d = tmp_path / "a" / "b"
    d.mkdir(parents=True)
    # Hand-craft a v1-shaped file (no head_sha / merged_at / followups_filed).
    (d / "12.md").write_text(
        '---\n'
        '{\n'
        '  "pr_number": 12,\n'
        '  "verdict": "APPROVE",\n'
        '  "timestamp": "2026-04-18T12:00:00Z",\n'
        '  "url": "https://x/12"\n'
        '}\n'
        '---\n'
        'old body'
    )
    rec = get_review("a/b", 12)
    assert rec is not None
    assert rec.pr_number == 12
    assert rec.verdict == "APPROVE"
    assert rec.head_sha == ""
    assert rec.merged_at is None
    assert rec.followups_filed == []


def test_mark_merged_updates_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("a/b", 5, "APPROVE", "https://x/5", "review body")
    out = mark_merged("a/b", 5, "2026-04-21T10:00:00Z", [101, 102])
    assert out is not None
    assert out.exists()
    rec = get_review("a/b", 5)
    assert rec.merged_at == "2026-04-21T10:00:00Z"
    assert sorted(rec.followups_filed) == [101, 102]
    # Body preserved.
    assert "review body" in rec.body


def test_mark_merged_dedupes_followup_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("a/b", 5, "APPROVE", "", "body")
    mark_merged("a/b", 5, "2026-04-21T10:00:00Z", [101, 102])
    mark_merged("a/b", 5, "2026-04-21T10:00:00Z", [102, 103])
    rec = get_review("a/b", 5)
    assert sorted(rec.followups_filed) == [101, 102, 103]


def test_mark_merged_returns_none_for_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    assert mark_merged("a/b", 999, "2026-04-21T10:00:00Z", []) is None


def test_mark_merged_preserves_body(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    body = "## Review\n\n- finding 1\n- [FOLLOWUP] do later\n"
    save_review("a/b", 11, "APPROVE", "", body)
    mark_merged("a/b", 11, "2026-04-21T10:00:00Z", [501])
    rec = get_review("a/b", 11)
    assert "## Review" in rec.body
    assert "[FOLLOWUP]" in rec.body


def test_mark_merged_persists_followup_titles(tmp_path, monkeypatch):
    """The orchestrator hands mark_merged the sanitized titles of the
    issues it just filed. Those must round-trip through the frontmatter
    so the next webhook delivery dedupes correctly by title, not count."""
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("a/b", 20, "APPROVE", "", "body")
    mark_merged(
        "a/b",
        20,
        "2026-04-21T10:00:00Z",
        [601],
        followup_titles=["[seneschal followup] do the thing"],
    )
    rec = get_review("a/b", 20)
    assert rec.followups_filed_titles == ["[seneschal followup] do the thing"]


def test_mark_merged_dedupes_titles_across_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("a/b", 21, "APPROVE", "", "body")
    mark_merged("a/b", 21, "2026-04-21T10:00:00Z", [601], followup_titles=["Alpha"])
    # Second call with the same title (different casing/whitespace).
    mark_merged("a/b", 21, "2026-04-21T10:00:00Z", [602], followup_titles=["  alpha  ", "Beta"])
    rec = get_review("a/b", 21)
    # Only one Alpha (casefold + whitespace collapse dedupe), Beta added.
    assert len(rec.followups_filed_titles) == 2
    assert rec.followups_filed_titles[0] == "Alpha"
    assert "Beta" in rec.followups_filed_titles


def test_review_record_defaults_followups_titles_to_empty_list():
    """v2 records without the new titles field must round-trip with []."""
    rec = ReviewRecord(
        repo="a/b",
        pr_number=1,
        verdict="APPROVE",
        timestamp="2026-04-21T10:00:00Z",
        url="https://x",
        body="",
    )
    assert rec.followups_filed_titles == []


# --------------------------------------------------------------------------
# B4: atomic save_review — a crash mid-write must not corrupt the file.
# --------------------------------------------------------------------------


def test_save_review_is_atomic_via_tempfile(tmp_path, monkeypatch):
    """`save_review` must use a sibling tempfile + `os.replace` pattern,
    not `path.write_text(content)` directly. The guarantee: if the write
    fails mid-flight, the original file (if any) is untouched — a
    half-written frontmatter would cause `get_review` to return None and
    silently lose the review.

    Simulates a crash by patching the internal atomic-write helper to
    raise after creating the tempfile but before os.replace. The
    existing review file on disk must be unchanged."""
    import review_store as rs
    monkeypatch.setattr(rs, "STORE_ROOT", str(tmp_path))

    # Seed: a known-good existing review.
    save_review("a/b", 7, "APPROVE", "https://x/7", "ORIGINAL BODY")
    orig_rec = get_review("a/b", 7)
    assert orig_rec is not None
    assert "ORIGINAL" in orig_rec.body

    # Force _atomic_write to raise by patching os.replace to fail.
    orig_replace = os.replace

    def _fail_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _fail_replace)

    import pytest
    with pytest.raises(OSError):
        save_review("a/b", 7, "REQUEST_CHANGES", "https://x/7", "CORRUPT ATTEMPT")

    # Restore replace so get_review / teardown works.
    monkeypatch.setattr(os, "replace", orig_replace)

    # Original file must still be intact.
    rec = get_review("a/b", 7)
    assert rec is not None
    assert rec.verdict == "APPROVE"
    assert "ORIGINAL" in rec.body
    assert "CORRUPT" not in rec.body

    # No tempfile leakage in the review dir.
    review_dir = tmp_path / "a" / "b"
    leftovers = [p for p in review_dir.iterdir() if p.name.startswith(".") and p.name.endswith(".tmp")]
    assert leftovers == []


def test_save_review_cleans_up_tempfile_on_error(tmp_path, monkeypatch):
    """If the atomic-write raises, the tempfile must be cleaned up so
    the reviews directory doesn't accumulate `.N.md.*.tmp` cruft."""
    import review_store as rs
    monkeypatch.setattr(rs, "STORE_ROOT", str(tmp_path))

    # Force os.replace to fail.
    def _fail_replace(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(os, "replace", _fail_replace)

    import pytest
    with pytest.raises(OSError):
        save_review("a/b", 99, "APPROVE", "", "body")

    review_dir = tmp_path / "a" / "b"
    if review_dir.exists():
        leftovers = list(review_dir.iterdir())
        # Only possibly nothing OR the tempfile was cleaned up.
        for p in leftovers:
            assert not p.name.endswith(".tmp"), (
                f"Tempfile {p} not cleaned up after atomic-write failure"
            )
