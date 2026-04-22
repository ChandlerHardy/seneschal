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

    # Patch parse_review_file to count calls on the second sync.
    # Round-3: promoted from the private `_parse_review_file` name since
    # review_index now imports it as a public symbol.
    calls = {"n": 0}
    orig = review_store.parse_review_file

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(review_store, "parse_review_file", _counting)
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


def test_adr_sync_failure_does_not_purge_existing_rows(tmp_path, store, idx, monkeypatch):
    """Blocker #4: when `find_adrs` raises for a repo during sync, the
    bare `except Exception: continue` used to fall through and leave
    `seen` without any entries for that repo. The purge step then
    DELETE'd every previously-indexed ADR for that repo, and the
    outer BEGIN IMMEDIATE committed the purge. The fix tracks failed
    repos and skips them in the purge pass."""
    cross_repo = pytest.importorskip("cross_repo")
    import history_context

    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    repo_dir = repos_root / "b"
    adr_dir = repo_dir / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-use-postgres.md").write_text(
        "# Use Postgres\n\nStatus: accepted\n\nMongo migration notes here."
    )
    (repo_dir / ".git").mkdir()
    (repo_dir / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:a/b.git\n'
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(repos_root))
    cross_repo._clear_cache()

    # First sync: the ADR lands in the index.
    idx.sync_from_markdown(str(store))
    assert len(idx.search_adrs("postgres")) == 1

    # Second sync: force find_adrs to raise for this repo.
    def _boom(path):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

    monkeypatch.setattr(history_context, "find_adrs", _boom)
    idx.sync_from_markdown(str(store))

    # The ADR row must still be present — the failed sync must NOT
    # have purged previously-indexed rows for that repo.
    hits = idx.search_adrs("postgres")
    assert len(hits) == 1, (
        "failed find_adrs caused purge of previously-indexed ADRs — "
        "transient failure must not corrupt the index"
    )

    # Third sync: recovery. find_adrs works again and the ADR stays.
    monkeypatch.setattr(history_context, "find_adrs", history_context.find_adrs.__wrapped__ if hasattr(history_context.find_adrs, "__wrapped__") else None)
    # `monkeypatch.setattr` + a sentinel above isn't strictly needed —
    # the fixture teardown undoes the setattr. Just re-run to confirm.
    monkeypatch.undo()
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(repos_root))
    cross_repo._clear_cache()
    idx.sync_from_markdown(str(store))
    assert len(idx.search_adrs("postgres")) == 1


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


def test_fts_probe_ignores_orphan_tables_in_real_db(tmp_path):
    """Blocker #5: the FTS5 probe used to run CREATE+DROP against the
    real index.db. A SIGKILL between CREATE and DROP left an orphan
    `_probe_fts` table committed on disk; the next startup's probe
    saw "table already exists", caught the error, returned False, and
    silently degraded every search to unindexed LIKE scans.

    Test: construct the race directly. The probe no longer takes a
    connection argument — it runs against `:memory:` unconditionally.
    Invoking it twice in a row (without manual cleanup) must still
    return True on both calls, proving the probe is isolated from any
    persistent state (real DB OR a prior probe's in-memory state).
    """
    # Skip if the local sqlite build lacks FTS5 — nothing to test.
    try:
        sample = sqlite3.connect(":memory:")
        sample.execute("CREATE VIRTUAL TABLE t USING fts5(x);")
        sample.close()
    except sqlite3.OperationalError:
        pytest.skip("sqlite build lacks FTS5")

    # Two back-to-back probes — if the probe leaked state between
    # invocations, the second call would hit 'table already exists'.
    assert review_index._probe_fts5() is True
    assert review_index._probe_fts5() is True
    # And even when invoked after Index has been built against a real
    # DB, the probe stays correct.
    db_path = tmp_path / "index.db"
    ix = review_index.open_index(str(db_path))
    try:
        assert review_index._probe_fts5() is True
        assert ix._fts5 is True
    finally:
        ix.close()


def test_fts_probe_tables_never_written_to_real_db(tmp_path):
    """Complementary check: after Index construction, neither
    `_probe_fts` nor `_probe_cd` exist in the on-disk DB. This is the
    structural invariant that prevents the orphan-after-SIGKILL bug."""
    db_path = tmp_path / "index.db"
    ix = review_index.open_index(str(db_path))
    try:
        con = sqlite3.connect(str(db_path))
        names = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        con.close()
        assert "_probe_fts" not in names
        assert "_probe_cd" not in names
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


def test_list_merged_prs_rejects_invalid_since(tmp_path, store, idx):
    """`since` is used in a lexicographic SQL comparison against the
    stored `merged_at` (ISO-8601). A caller typo like `"yesterday"` or
    a date-only string would silently return an empty window — we now
    raise ValueError so the MCP layer can return an error payload."""
    import pytest
    _write_review(store, "a/b", 1, "body", merged_at="2026-04-20T00:00:00Z")
    idx.sync_from_markdown(str(store))
    # Date-only is valid (ISO-8601 allows bare dates).
    assert idx.list_merged_prs(since="2026-01-01") != []
    # Full ISO datetime works.
    assert idx.list_merged_prs(since="2026-01-01T00:00:00Z") != []
    # Bogus strings raise.
    with pytest.raises(ValueError):
        idx.list_merged_prs(since="yesterday")
    with pytest.raises(ValueError):
        idx.list_merged_prs(since="not-a-date")


def test_list_merged_prs_normalizes_offset_since_to_utc(tmp_path, store, idx):
    """Round-3 warning #6: stored `merged_at` is always `Z`-suffixed
    (UTC), and the SQL comparison is lexicographic. An offset-carrying
    input like `2026-04-22T17:00:00+05:00` sorts BEFORE the equivalent
    UTC instant (`+` < `Z` lexicographically), so the same instant
    could land inside or outside the same window depending on the
    suffix used. `_parse_iso_or_raise` now normalizes every tz-aware
    input to UTC-Z BEFORE the SQL compare so callers can supply any
    offset and get consistent semantics."""
    # Stored merged_at is 12:00 UTC.
    _write_review(store, "a/b", 1, "body", merged_at="2026-04-22T12:00:00Z")
    idx.sync_from_markdown(str(store))

    # `+05:00` input representing the same instant (12:00 UTC) should
    # INCLUDE the stored row. Before the fix, the raw `17:00:00+05:00`
    # string would sort after `12:00:00Z` on lex compare and exclude it.
    rows = idx.list_merged_prs(since="2026-04-22T17:00:00+05:00")
    assert len(rows) == 1, (
        "offset-input equivalent to stored UTC must be normalized before "
        "lex compare — same instant must not exclude the stored row"
    )
    # A slightly-LATER UTC instant (12:01) must exclude the stored row.
    rows = idx.list_merged_prs(since="2026-04-22T17:01:00+05:00")
    assert len(rows) == 0

    # Naive input (no tz) is treated as UTC — earlier naive window
    # includes the row.
    rows = idx.list_merged_prs(since="2026-04-22T11:59:59")
    assert len(rows) == 1


def test_search_adrs_returns_snippet_key_not_excerpt(tmp_path, store, idx, monkeypatch):
    """Key unification: search_reviews returns `snippet`, search_adrs
    used to return `excerpt`. Same concept → same key."""
    cross_repo = pytest.importorskip("cross_repo")

    repos_root = tmp_path / "repos"
    repos_root.mkdir()
    repo_dir = repos_root / "b"
    adr_dir = repo_dir / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-snippet-key.md").write_text("# Key unification\n\nbody")
    (repo_dir / ".git").mkdir()
    (repo_dir / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:a/b.git\n'
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(repos_root))
    cross_repo._clear_cache()

    idx.sync_from_markdown(str(store))
    hits = idx.search_adrs("unification")
    assert len(hits) >= 1
    assert "snippet" in hits[0]
    assert "excerpt" not in hits[0]


def test_open_index_uses_env_var(tmp_path, monkeypatch):
    db_path = tmp_path / "custom.db"
    monkeypatch.setenv("SENESCHAL_INDEX_PATH", str(db_path))
    ix = review_index.open_index()
    try:
        assert db_path.exists()
    finally:
        ix.close()
