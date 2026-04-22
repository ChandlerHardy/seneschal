"""Tests for review_index: SQLite cache over the canonical markdown review store.

The index is always rebuildable from `~/.seneschal/reviews/<owner>/<repo>/<N>.md`,
so these tests exercise sync-from-markdown round-trips, FTS and LIKE search paths,
mtime-based skip, purge of removed files, schema-version migration, and
secrets redaction on snippet output.
"""

import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review_index  # noqa: E402
import review_store  # noqa: E402


# --------------------------------------------------------------------------
# Helpers — write synthetic markdown frontmatter into a store-root dir.
# --------------------------------------------------------------------------


def _write_review(
    store_root,
    slug: str,
    pr_number: int,
    body: str,
    *,
    verdict: str = "APPROVE",
    timestamp: str = "2026-04-18T12:00:00Z",
    head_sha: str = "",
    merged_at=None,
    extra_fields=None,
):
    owner, repo = slug.split("/")
    d = os.path.join(str(store_root), owner, repo)
    os.makedirs(d, exist_ok=True)
    meta = {
        "pr_number": pr_number,
        "verdict": verdict,
        "timestamp": timestamp,
        "url": f"https://github.com/{slug}/pull/{pr_number}",
    }
    if head_sha:
        meta["head_sha"] = head_sha
    if merged_at:
        meta["merged_at"] = merged_at
    if extra_fields:
        meta.update(extra_fields)
    p = os.path.join(d, f"{pr_number}.md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("---\n")
        fh.write(json.dumps(meta, indent=2))
        fh.write("\n---\n")
        fh.write(body)
    return p


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A fresh on-disk review store root, patched onto review_store."""
    root = tmp_path / "reviews"
    root.mkdir()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(root))
    return root


@pytest.fixture
def idx(tmp_path):
    """A fresh review_index.Index on a tempfile DB."""
    db_path = tmp_path / "index.db"
    ix = review_index.open_index(str(db_path))
    yield ix
    ix.close()


# --------------------------------------------------------------------------
# sync_from_markdown round-trip + mtime skip + purge
# --------------------------------------------------------------------------


def test_sync_ingests_v1_frontmatter(store, idx):
    _write_review(
        store,
        "a/b",
        1,
        "body mentions migration strategy",
        verdict="APPROVE",
    )
    n = idx.sync_from_markdown(str(store))
    assert n >= 1
    results = idx.search_reviews("migration")
    assert len(results) == 1
    assert results[0]["repo"] == "a/b"
    assert results[0]["pr_number"] == 1
    assert results[0]["verdict"] == "APPROVE"
    assert "migration" in results[0]["snippet"].lower()


def test_sync_ingests_v2_and_v2_1_frontmatter(store, idx):
    _write_review(
        store,
        "a/b",
        2,
        "body2 authentication middleware rework",
        head_sha="abc123",
        merged_at="2026-04-20T00:00:00Z",
        extra_fields={
            "followups_filed": [101],
            "followups_filed_titles": ["[followup] fix retry"],
        },
    )
    idx.sync_from_markdown(str(store))
    results = idx.search_reviews("authentication")
    assert len(results) == 1
    row = results[0]
    assert row["head_sha"] == "abc123"
    assert row["merged_at"] == "2026-04-20T00:00:00Z"


def test_sync_skips_unchanged_files(store, idx, monkeypatch):
    """Second sync on untouched files must not re-parse."""
    _write_review(store, "a/b", 1, "first body")
    idx.sync_from_markdown(str(store))

    # Patch _parse_review_file to count calls on the second sync.
    calls = {"n": 0}
    orig = review_store._parse_review_file

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(review_store, "_parse_review_file", _counting)
    n = idx.sync_from_markdown(str(store))
    assert n == 0, "unchanged files should not be re-parsed"
    assert calls["n"] == 0


def test_sync_reparses_on_mtime_bump(store, idx):
    """When a file's mtime advances, its body must be re-read."""
    p = _write_review(store, "a/b", 1, "original body")
    idx.sync_from_markdown(str(store))

    # Overwrite with new content + advance mtime.
    _write_review(store, "a/b", 1, "replaced body with kubernetes rollout")
    new_mtime = os.stat(p).st_mtime + 2
    os.utime(p, (new_mtime, new_mtime))

    n = idx.sync_from_markdown(str(store))
    assert n == 1
    results = idx.search_reviews("kubernetes")
    assert len(results) == 1
    # Old body no longer indexed.
    assert idx.search_reviews("original") == []


def test_sync_purges_deleted_files(store, idx):
    p1 = _write_review(store, "a/b", 1, "body one alpha")
    _write_review(store, "a/b", 2, "body two beta")
    idx.sync_from_markdown(str(store))
    assert len(idx.search_reviews("alpha")) == 1

    os.unlink(p1)
    idx.sync_from_markdown(str(store))
    assert idx.search_reviews("alpha") == []
    # PR 2 still present.
    assert len(idx.search_reviews("beta")) == 1


# --------------------------------------------------------------------------
# Search filters + limit
# --------------------------------------------------------------------------


def test_search_reviews_filters_by_repo(store, idx):
    _write_review(store, "a/b", 1, "nginx config drift")
    _write_review(store, "c/d", 7, "nginx sidecar rollout")
    idx.sync_from_markdown(str(store))

    all_hits = idx.search_reviews("nginx")
    assert len(all_hits) == 2
    scoped = idx.search_reviews("nginx", repo="a/b")
    assert len(scoped) == 1
    assert scoped[0]["repo"] == "a/b"


def test_search_reviews_rejects_malformed_repo(store, idx):
    _write_review(store, "a/b", 1, "hello")
    idx.sync_from_markdown(str(store))
    with pytest.raises(ValueError):
        idx.search_reviews("hello", repo="../etc/passwd")


def test_search_reviews_respects_limit(store, idx):
    for pr in range(1, 6):
        _write_review(store, "a/b", pr, f"widget body {pr}")
    idx.sync_from_markdown(str(store))
    hits = idx.search_reviews("widget", limit=2)
    assert len(hits) == 2


# --------------------------------------------------------------------------
# Snippet redaction — secrets_scan._PATTERNS must scrub tokens.
# --------------------------------------------------------------------------


def test_snippet_redacts_secrets(store, idx):
    # Embed a fake GitHub PAT-shaped token in a review body.
    fake_token = "ghp_" + ("A" * 40)
    _write_review(store, "a/b", 1, f"found token {fake_token} in code")
    idx.sync_from_markdown(str(store))
    hits = idx.search_reviews("token")
    assert len(hits) == 1
    assert fake_token not in hits[0]["snippet"]


def test_snippet_is_bounded(store, idx):
    long_body = "review body " + ("x" * 5000) + " needle here " + ("y" * 5000)
    _write_review(store, "a/b", 1, long_body)
    idx.sync_from_markdown(str(store))
    hits = idx.search_reviews("needle")
    assert len(hits) == 1
    # snippet should be bounded (not the entire 10 KB body)
    assert len(hits[0]["snippet"]) < 1000


# --------------------------------------------------------------------------
# FTS5-vs-LIKE fallback
# --------------------------------------------------------------------------


def test_like_fallback_still_searches(store, tmp_path):
    """Force LIKE path and confirm search still works end-to-end."""
    _write_review(store, "a/b", 1, "body with deliverable word")
    db_path = tmp_path / "like.db"
    ix = review_index.open_index(str(db_path))
    try:
        ix._fts5 = False  # force fallback
        # Rebuild without FTS.
        ix._drop_and_recreate()
        ix.sync_from_markdown(str(store))
        hits = ix.search_reviews("deliverable")
        assert len(hits) == 1
        assert hits[0]["pr_number"] == 1
    finally:
        ix.close()


def test_fts_query_handles_special_chars(store, idx):
    """User-supplied FTS queries with quotes / dashes must not crash."""
    _write_review(store, "a/b", 1, "foo-bar baz review")
    idx.sync_from_markdown(str(store))
    # These would be invalid raw FTS syntax; wrapper must sanitize.
    for q in ['foo"bar', "foo-bar", 'x AND "unterminated', "NEAR(x y"]:
        idx.search_reviews(q)  # must not raise


# --------------------------------------------------------------------------
# ADR indexing + search.
# --------------------------------------------------------------------------


def test_sync_indexes_adrs_from_known_repos(tmp_path, store, idx, monkeypatch):
    """ADR sync walks SENESCHAL_REPOS_ROOT for `known_repo` dirs and
    runs history_context.find_adrs. Each top-level child of the repos
    root that carries a github-origin `.git/config` gets scanned."""
    cross_repo = pytest.importorskip("cross_repo")

    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    # Flat layout: ~/repos/<name>/ not ~/repos/<owner>/<name>/.
    repo_dir = repos_root / "b"
    adr_dir = repo_dir / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-use-postgres.md").write_text(
        "# Use Postgres\n\nStatus: accepted\n\nWe picked postgres over mongo."
    )
    # Mark the repo as known by writing a .git/config with github origin.
    gitconf_dir = repo_dir / ".git"
    gitconf_dir.mkdir()
    (gitconf_dir / "config").write_text(
        '[remote "origin"]\n'
        "\turl = git@github.com:a/b.git\n"
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(repos_root))
    # Cache is process-lifetime; reset so our env override lands.
    cross_repo._clear_cache()

    idx.sync_from_markdown(str(store))
    hits = idx.search_adrs("postgres")
    assert len(hits) == 1
    assert hits[0]["repo"] == "a/b"
    assert "Postgres" in hits[0]["title"]


# --------------------------------------------------------------------------
# Schema version mismatch → drop + rebuild.
# --------------------------------------------------------------------------


def test_schema_mismatch_drops_and_rebuilds(tmp_path):
    """A pre-existing DB with a different user_version is silently
    dropped and recreated at open_index time."""
    db_path = tmp_path / "index.db"
    # Manually create a DB with a bogus schema_version.
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA user_version = 99999;")
    con.execute("CREATE TABLE reviews (foo TEXT);")
    con.commit()
    con.close()

    ix = review_index.open_index(str(db_path))
    try:
        # If drop+rebuild worked, the schema has the correct columns.
        con = sqlite3.connect(str(db_path))
        cols = [row[1] for row in con.execute("PRAGMA table_info(reviews)")]
        con.close()
        assert "pr_number" in cols
        assert "verdict" in cols
    finally:
        ix.close()


def test_wal_mode_is_enabled(tmp_path):
    db_path = tmp_path / "index.db"
    ix = review_index.open_index(str(db_path))
    try:
        con = sqlite3.connect(str(db_path))
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        con.close()
        assert mode.lower() == "wal"
    finally:
        ix.close()


# --------------------------------------------------------------------------
# open_index default path honors SENESCHAL_INDEX_PATH env var.
# --------------------------------------------------------------------------


def test_open_index_uses_env_var(tmp_path, monkeypatch):
    db_path = tmp_path / "custom.db"
    monkeypatch.setenv("SENESCHAL_INDEX_PATH", str(db_path))
    ix = review_index.open_index()
    try:
        assert db_path.exists()
    finally:
        ix.close()
