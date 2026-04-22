"""SQLite cache over the canonical markdown review store + ADR discovery.

The markdown store at `~/.seneschal/reviews/<owner>/<repo>/<N>.md` is
authoritative. This module's only job is to keep a searchable mirror at
`~/.seneschal/index.db` so the MCP server can answer cross-repo queries
("show me every review mentioning 'migration'") without walking the
filesystem for every call.

Design rules:

- The DB is always rebuildable from markdown — any schema-version mismatch
  drops + recreates the file. Cheap: <10k reviews expected in year 1.
- `PRAGMA journal_mode=WAL` + short connection timeouts protect against
  the MCP server and a future webhook-side writer stepping on each other.
- FTS5 is used when the sqlite build has it; otherwise we fall back to
  `LIKE '%...%'` (still parameterized — no string interpolation). The
  probe runs once per `Index` instance against a throwaway temp table.
- ADRs are discovered per-known-repo via `history_context.find_adrs` by
  walking `SENESCHAL_REPOS_ROOT` (default `~/repos`) for directories
  that `cross_repo.known_repos` recognizes.
- Review-body snippets pass through `secrets_scan._PATTERNS` before we
  ever return them — an otherwise-clean review can have a leaked token
  in a code block and the MCP tool is a new egress channel. Redact.

This module is stdlib-only apart from the sibling Seneschal modules it
imports; no Flask / anthropic / fastmcp coupling so it can run in the
MCP process.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import threading
from typing import List, Optional

from fs_safety import validate_repo_slug
from log import log as _neutral_log
from secrets_scan import redact as _secrets_redact


# Schema version. Bump when the SQL schema changes in a way that would make
# an existing DB unreadable. On mismatch, open_index drops + recreates —
# the markdown store is canonical so rebuild is always safe.
_SCHEMA_VERSION = 1

# Default index path — overridable via env so operators can point the MCP
# server at an alternate location (tests pass an explicit path).
_DEFAULT_INDEX_PATH = os.path.expanduser("~/.seneschal/index.db")

# Snippet sizing. We want enough context around a match to be useful but
# not so much that a dozen matches blow past the MCP stdio buffer.
_SNIPPET_WINDOW = 200    # chars on either side of the match
_SNIPPET_MAX = 500       # hard cap regardless of window

# FTS tokens we must sanitize before handing to MATCH. FTS5 treats
# `"`, `-`, `AND`/`OR`/`NOT`, `NEAR(` and parentheses as syntax; a raw
# user query that happens to contain any of these crashes the query.
# Wrapping the sanitized token in quotes forces literal-phrase matching.
_FTS_UNSAFE_RE = re.compile(r'["\\]')


def _log(msg: str) -> None:
    """Stderr logger with the `[review_index]` prefix. Delegates to the
    neutral `log.log` so every module shares one formatter; the module
    tag stays local so stderr output remains grep-able."""
    _neutral_log(f"[review_index] {msg}")


def _sanitize_fts_query(q: str) -> str:
    """Coerce a user string into a safe FTS5 phrase match.

    We don't try to preserve FTS operators from the user; the input is a
    human query, not a query language. Strip backslashes + quotes, then
    wrap in `"..."` so FTS5 treats it as a single phrase. Empty or
    punctuation-only queries return an empty string — callers should
    fall back to LIKE (or just return [])."""
    if not q:
        return ""
    cleaned = _FTS_UNSAFE_RE.sub(" ", q).strip()
    if not cleaned:
        return ""
    return f'"{cleaned}"'


# Backward-compat shim: keep the old private name so tests and any
# out-of-tree callers that imported `_redact_snippet` still work. The
# canonical implementation now lives in `secrets_scan.redact`.
_redact_snippet = _secrets_redact


def _make_snippet(body: str, query: str) -> str:
    """Return a ~200-char window around the first case-insensitive
    occurrence of `query` in `body`, falling back to the prefix if the
    query isn't directly present (FTS matches work on tokens, not
    substrings, so the raw query may not appear verbatim even when FTS
    matched). Always redact secrets."""
    if not body:
        return ""
    lower = body.lower()
    q = (query or "").strip().lower()
    idx = lower.find(q) if q else -1
    if idx < 0:
        snippet = body[:_SNIPPET_MAX]
    else:
        start = max(0, idx - _SNIPPET_WINDOW)
        end = min(len(body), idx + len(q) + _SNIPPET_WINDOW)
        snippet = body[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(body):
            snippet = snippet + "..."
    snippet = snippet[:_SNIPPET_MAX]
    return _redact_snippet(snippet)


def _probe_fts5() -> bool:
    """Check whether this sqlite build supports FTS5 by attempting to
    create a throwaway virtual table on an in-memory connection.

    Runs against `:memory:` (not the real index DB) so a SIGKILL between
    CREATE and DROP can never leave an orphan `_probe_fts` table in the
    on-disk file. The orphan bug was reproducible: a crashed probe left
    the table committed; the next startup's probe saw "table already
    exists", caught the OperationalError, returned False, and silently
    degraded every future search query to unindexed LIKE scans until an
    operator noticed that searches were slow and ran a manual DROP.

    The in-memory connection is thrown away at function return; the
    probe's cost is ~1ms and runs once per Index open.
    """
    try:
        probe_con = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False
    try:
        probe_con.execute("CREATE VIRTUAL TABLE _probe_fts USING fts5(x);")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        try:
            probe_con.close()
        except sqlite3.Error:
            pass


def _probe_fts5_contentless_delete() -> bool:
    """Secondary probe: does FTS5 accept the `contentless_delete=1` option?

    Added in SQLite 3.43; older builds reject it at CREATE time. If
    missing, we keep the DELETE-less contentless table and route purges
    through the 'delete' special-insert protocol instead.

    Same :memory: isolation as `_probe_fts5` — a crashed probe must not
    orphan a `_probe_cd` table in the real index DB.
    """
    try:
        probe_con = sqlite3.connect(":memory:")
    except sqlite3.Error:
        return False
    try:
        probe_con.execute(
            "CREATE VIRTUAL TABLE _probe_cd USING fts5(x, content='', contentless_delete=1);"
        )
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        try:
            probe_con.close()
        except sqlite3.Error:
            pass


def open_index(path: Optional[str] = None) -> "Index":
    """Open (or create) a review_index.Index.

    Resolution order for `path`:
      1. Explicit argument.
      2. `SENESCHAL_INDEX_PATH` env var.
      3. `~/.seneschal/index.db` (default).

    On schema_version mismatch the DB file is deleted + recreated. The
    markdown store is authoritative, so a cold index rebuild is cheap."""
    if path is None:
        path = os.environ.get("SENESCHAL_INDEX_PATH", _DEFAULT_INDEX_PATH)
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    # Check user_version. If it's a fresh file, the PRAGMA returns 0.
    # If it's ours at the right version, keep it. Otherwise wipe + recreate.
    if os.path.exists(path):
        try:
            probe = sqlite3.connect(path, timeout=5.0)
            row = probe.execute("PRAGMA user_version").fetchone()
            current = int(row[0]) if row else 0
            probe.close()
        except sqlite3.DatabaseError:
            current = -1
        if current != _SCHEMA_VERSION:
            _log(
                f"schema_version mismatch (db={current}, expected={_SCHEMA_VERSION}); "
                f"dropping + recreating {path}"
            )
            try:
                os.unlink(path)
            except OSError as e:
                _log(f"failed to unlink {path}: {e}; continuing in-place")
    return Index(path)


class Index:
    """Thread-safe wrapper around a single sqlite3 connection.

    Shared across FastMCP tool dispatches. Sync tools in FastMCP run
    on an asyncio thread-pool executor (`asyncio.to_thread`), so two
    concurrent tool invocations can land on different threads. SQLite's
    default `check_same_thread=True` raises ProgrammingError in that
    case, and even with `check_same_thread=False` we need a lock to
    prevent two simultaneous `BEGIN IMMEDIATE` statements from
    colliding. Every public method acquires `self._lock` before
    touching the connection.

    Cross-process writes are still serialized by WAL + a 5s busy
    timeout — the lock only handles in-process concurrency.
    """

    def __init__(self, path: str):
        self._path = path
        # check_same_thread=False: safe because every public method
        # holds self._lock for the duration of its SQL work, so the
        # connection is never touched by two threads at once.
        self._con = sqlite3.connect(
            path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        # RLock (not Lock) so `ensure_synced` can call
        # `sync_from_markdown` without re-entering the lock and
        # deadlocking. Every public method acquires the lock; some
        # call other public methods — RLock makes that safe.
        self._lock = threading.RLock()
        # `_synced` + `_lock` together implement a lazy-once sync for
        # the MCP server's first tool call per process. The global
        # `_INDEX_SYNCED` sentinel that used to live in server.py had
        # a check-then-set race: two concurrent callers both saw False
        # and both ran `sync_from_markdown`, which collided on a
        # second BEGIN IMMEDIATE inside the first one's transaction.
        # Moving the state here + using the shared lock closes that
        # race without needing a second primitive in server.py.
        self._synced = False
        self._con.execute("PRAGMA journal_mode=WAL;")
        self._con.execute("PRAGMA synchronous=NORMAL;")
        self._con.execute("PRAGMA foreign_keys=ON;")
        # Probes run against throwaway :memory: connections so a
        # SIGKILL between CREATE and DROP can't leave an orphan probe
        # table in the real DB (which would cause the NEXT startup's
        # probe to see "table already exists" and silently degrade
        # every search to unindexed LIKE scans).
        self._fts5 = _probe_fts5()
        self._fts5_contentless_delete = (
            _probe_fts5_contentless_delete() if self._fts5 else False
        )
        self._ensure_schema()

    def ensure_synced(self, store_root: Optional[str] = None) -> None:
        """Call `sync_from_markdown` exactly once per Index lifetime.

        Thread-safe: uses the shared `self._lock` (RLock) so two
        concurrent callers race once for the sync, then every
        subsequent caller no-ops. On sync failure `_synced` stays
        False so the next call retries (same semantics the old
        module-level sentinel had, minus the race).

        Replaces the `_INDEX_SYNCED` global in mcp_server.server —
        that sentinel was read-then-set without a lock, so two
        concurrent first-calls both saw False and both ran
        `sync_from_markdown`, colliding on a second BEGIN IMMEDIATE
        inside the first's transaction.
        """
        with self._lock:
            if self._synced:
                return
            self.sync_from_markdown(store_root)
            self._synced = True

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create tables if missing. Idempotent."""
        cur = self._con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reviews (
              repo TEXT NOT NULL,
              pr_number INTEGER NOT NULL,
              verdict TEXT,
              timestamp TEXT,
              merged_at TEXT,
              head_sha TEXT,
              url TEXT,
              body TEXT,
              mtime REAL NOT NULL,
              PRIMARY KEY (repo, pr_number)
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_reviews_merged_at ON reviews(merged_at);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_reviews_verdict ON reviews(verdict);"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS adrs (
              repo TEXT NOT NULL,
              path TEXT NOT NULL,
              id TEXT,
              title TEXT,
              status TEXT,
              body TEXT,
              mtime REAL NOT NULL,
              PRIMARY KEY (repo, path)
            );
            """
        )
        if self._fts5:
            # Contentless FTS tables with `contentless_delete=1` (SQLite
            # 3.43+) let us hand-manage the FTS index via rowid. External-
            # content tables bind the FTS rowid to the parent's rowid,
            # which collides with sqlite's rowid reuse on conflict-replace
            # and explodes with "database disk image is malformed" on the
            # next DELETE. Contentless-delete gives us a DELETE primitive
            # without forcing us to remember the OLD body text.
            #
            # Older sqlite builds reject `contentless_delete=1` at CREATE
            # time; we probe for it and fall back to external-content in
            # that case. The probe happens in _probe_fts5_features.
            if self._fts5_contentless_delete:
                cur.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS reviews_fts "
                    "USING fts5(body, content='', contentless_delete=1);"
                )
                cur.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS adrs_fts "
                    "USING fts5(title, body, content='', contentless_delete=1);"
                )
            else:
                # Fallback: vanilla contentless. DELETE isn't supported
                # on these, so we instead emit the 'delete' command via
                # the special-insert protocol when purging rows.
                cur.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS reviews_fts "
                    "USING fts5(body, content='');"
                )
                cur.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS adrs_fts "
                    "USING fts5(title, body, content='');"
                )
        cur.execute(f"PRAGMA user_version = {_SCHEMA_VERSION};")

    def _drop_and_recreate(self) -> None:
        """Nuke all rows + tables and recreate the schema from scratch.

        Exposed for tests that want to force the LIKE path after flipping
        `_fts5` to False. Not called in production — schema-version
        mismatch at open_index time handles that case by deleting the
        file entirely, which is more thorough."""
        cur = self._con.cursor()
        for tbl in ("reviews_fts", "adrs_fts", "reviews", "adrs"):
            try:
                cur.execute(f"DROP TABLE IF EXISTS {tbl};")
            except sqlite3.OperationalError:
                pass
        self._ensure_schema()

    def close(self) -> None:
        with self._lock:
            try:
                self._con.close()
            except sqlite3.Error:
                pass

    # ------------------------------------------------------------------
    # FTS helpers
    # ------------------------------------------------------------------

    def _fts_delete(self, fts_table: str, rowid: int) -> None:
        """Remove a row from a contentless FTS5 table by rowid.

        SQLite 3.43+ with `contentless_delete=1`: vanilla `DELETE FROM`
        works. We rely on that path in production (Mac + modern Linux ship
        3.43+). On older builds (`contentless_delete` absent) we skip the
        delete — the FTS index will carry stale entries, which is
        annoying but not incorrect because every search JOINs back to the
        `reviews` table. The periodic `sync_from_markdown` already purges
        reviews rows whose files vanished, so FTS hits with no matching
        review are silently dropped at JOIN time."""
        if not self._fts5_contentless_delete:
            return
        try:
            self._con.execute(
                f"DELETE FROM {fts_table} WHERE rowid=?;", (rowid,)
            )
        except sqlite3.OperationalError:
            # Defensive: if the probe was optimistic, don't crash the
            # sync — the search-time JOIN will filter stale rowids.
            self._fts5_contentless_delete = False

    # ------------------------------------------------------------------
    # Sync from markdown
    # ------------------------------------------------------------------

    def sync_from_markdown(self, store_root: Optional[str] = None) -> int:
        """Reconcile DB state with the markdown review store.

        Walks `<store_root>/<owner>/<repo>/<N>.md`. For each file: if its
        mtime is newer than the cached row (or there is no row), re-parse
        via `review_store._parse_review_file` and upsert. Any DB row whose
        file no longer exists on disk is purged.

        Returns the number of rows inserted/updated (not counting purges).
        Deferred import of `review_store` avoids a circular import — this
        module is imported by the MCP server, and `review_store` imports
        `fs_safety` which the review-memory path re-enters.

        Thread-safe: acquires `self._lock` for the duration of the sync
        so a second thread can't start its own BEGIN IMMEDIATE while
        this one is mid-transaction.
        """
        import review_store

        if store_root is None:
            store_root = getattr(review_store, "STORE_ROOT", None) or os.path.expanduser(
                "~/.seneschal/reviews"
            )
        with self._lock:
            n_updated = self._sync_reviews(store_root)
            # ADRs: walk known repos. Failure here is non-fatal —
            # reviews sync is the main show, ADRs are gravy. Log and
            # move on.
            try:
                self._sync_adrs()
            except Exception as e:  # noqa: BLE001 — defensive boundary
                _log(f"ADR sync failed: {e}")
        return n_updated

    def _sync_reviews(self, store_root: str) -> int:
        import review_store

        if not os.path.isdir(store_root):
            return 0
        # Wrap the sync in an explicit transaction so an insert failure
        # mid-run doesn't leave reviews + reviews_fts inconsistent. With
        # isolation_level=None (autocommit) every statement is its own
        # transaction; `BEGIN IMMEDIATE` forces all subsequent writes to
        # commit together or not at all. Any exception triggers ROLLBACK.
        self._con.execute("BEGIN IMMEDIATE;")
        try:
            n = self._sync_reviews_inner(store_root)
            self._con.execute("COMMIT;")
            return n
        except Exception:
            try:
                self._con.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise

    def _sync_reviews_inner(self, store_root: str) -> int:
        import review_store

        cur = self._con.cursor()
        # Build the set of on-disk files first so we can diff against the DB.
        on_disk: dict = {}  # (repo, pr_number) -> (path, mtime)
        for owner in sorted(os.listdir(store_root)):
            owner_path = os.path.join(store_root, owner)
            if not os.path.isdir(owner_path):
                continue
            for repo in sorted(os.listdir(owner_path)):
                repo_path = os.path.join(owner_path, repo)
                if not os.path.isdir(repo_path):
                    continue
                slug = f"{owner}/{repo}"
                try:
                    validate_repo_slug(slug)
                except ValueError:
                    # Defense against weird dirs that snuck into the store.
                    _log(f"skipping non-conforming slug {slug!r}")
                    continue
                for name in os.listdir(repo_path):
                    if not name.endswith(".md"):
                        continue
                    stem = name[:-3]
                    if not stem.isdigit():
                        continue
                    pr_number = int(stem)
                    fpath = os.path.join(repo_path, name)
                    try:
                        st = os.stat(fpath)
                    except OSError:
                        continue
                    on_disk[(slug, pr_number)] = (fpath, st.st_mtime)

        # Load existing (repo, pr_number) -> mtime from DB.
        existing: dict = {}
        for row in cur.execute("SELECT repo, pr_number, mtime FROM reviews"):
            existing[(row[0], int(row[1]))] = float(row[2])

        n_updated = 0
        for key, (fpath, mtime) in on_disk.items():
            slug, pr_number = key
            cached = existing.get(key)
            if cached is not None and cached >= mtime:
                continue  # mtime skip — file unchanged since last sync
            # (Re)parse via the canonical review-store parser so schema
            # drift (new frontmatter keys) is handled in one place.
            from pathlib import Path
            rec = review_store._parse_review_file(Path(fpath), slug)
            if rec is None:
                _log(f"skipped unparseable review file: {fpath}")
                continue
            cur.execute(
                """
                INSERT INTO reviews (repo, pr_number, verdict, timestamp,
                                     merged_at, head_sha, url, body, mtime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (repo, pr_number) DO UPDATE SET
                  verdict=excluded.verdict,
                  timestamp=excluded.timestamp,
                  merged_at=excluded.merged_at,
                  head_sha=excluded.head_sha,
                  url=excluded.url,
                  body=excluded.body,
                  mtime=excluded.mtime;
                """,
                (
                    slug,
                    int(pr_number),
                    rec.verdict,
                    rec.timestamp,
                    rec.merged_at,
                    rec.head_sha,
                    rec.url,
                    rec.body,
                    mtime,
                ),
            )
            if self._fts5:
                rowid = cur.execute(
                    "SELECT rowid FROM reviews WHERE repo=? AND pr_number=?",
                    (slug, int(pr_number)),
                ).fetchone()[0]
                self._fts_delete("reviews_fts", rowid)
                cur.execute(
                    "INSERT INTO reviews_fts(rowid, body) VALUES (?, ?);",
                    (rowid, rec.body or ""),
                )
            n_updated += 1

        # Purge DB rows whose files vanished.
        to_purge = [k for k in existing.keys() if k not in on_disk]
        for slug, pr_number in to_purge:
            if self._fts5:
                row = cur.execute(
                    "SELECT rowid FROM reviews WHERE repo=? AND pr_number=?",
                    (slug, int(pr_number)),
                ).fetchone()
                if row:
                    self._fts_delete("reviews_fts", row[0])
            cur.execute(
                "DELETE FROM reviews WHERE repo=? AND pr_number=?",
                (slug, int(pr_number)),
            )
        return n_updated

    def _sync_adrs(self) -> None:
        """Walk `SENESCHAL_REPOS_ROOT` and index ADRs from every known
        GitHub-origin repo. Deferred import of `cross_repo` +
        `history_context` for the same reason as review_store above."""
        import cross_repo
        import history_context

        repos = cross_repo.known_repos()
        # Same transaction wrapping as _sync_reviews — atomic across all
        # repos so an I/O failure halfway through doesn't leave half-
        # indexed state.
        self._con.execute("BEGIN IMMEDIATE;")
        try:
            self._sync_adrs_inner(repos, history_context)
            self._con.execute("COMMIT;")
        except Exception:
            try:
                self._con.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise

    def _sync_adrs_inner(self, repos, history_context) -> None:
        cur = self._con.cursor()
        # Track (repo, path) pairs we see on this pass so we can purge stale rows.
        seen: set = set()
        # Track repos whose `find_adrs` raised. We must NOT purge their
        # existing `adrs` rows — a transient failure (UnicodeDecodeError on
        # a single ADR, temporary EIO from a flaky FS) would otherwise
        # wipe every indexed ADR for that repo and the outer BEGIN
        # IMMEDIATE would persist the purge. The next successful sync
        # restores the correct set.
        failed_repos: set = set()
        for kr in repos:
            try:
                adrs = history_context.find_adrs(kr.path)
            except Exception as e:  # noqa: BLE001
                _log(f"find_adrs failed for {kr.slug}: {e}")
                failed_repos.add(kr.slug)
                continue
            for adr in adrs:
                abs_path = os.path.join(kr.path, adr.path)
                try:
                    mtime = os.stat(abs_path).st_mtime
                except OSError:
                    continue
                seen.add((kr.slug, adr.path))
                cur.execute(
                    """
                    INSERT INTO adrs (repo, path, id, title, status, body, mtime)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (repo, path) DO UPDATE SET
                      id=excluded.id,
                      title=excluded.title,
                      status=excluded.status,
                      body=excluded.body,
                      mtime=excluded.mtime;
                    """,
                    (kr.slug, adr.path, adr.id, adr.title, adr.status, adr.body, mtime),
                )
                if self._fts5:
                    rowid = cur.execute(
                        "SELECT rowid FROM adrs WHERE repo=? AND path=?",
                        (kr.slug, adr.path),
                    ).fetchone()[0]
                    self._fts_delete("adrs_fts", rowid)
                    cur.execute(
                        "INSERT INTO adrs_fts(rowid, title, body) VALUES (?, ?, ?);",
                        (rowid, adr.title or "", adr.body or ""),
                    )
        # Purge ADRs that disappeared (file deleted or repo removed).
        # Skip purge for repos whose find_adrs raised this pass — we
        # have no ground truth to diff against, so deleting would be
        # data loss. Their rows stay intact until the next successful
        # sync reconciles them.
        existing_rows = list(cur.execute("SELECT repo, path FROM adrs"))
        for slug, path in existing_rows:
            if slug in failed_repos:
                continue
            if (slug, path) not in seen:
                if self._fts5:
                    row = cur.execute(
                        "SELECT rowid FROM adrs WHERE repo=? AND path=?",
                        (slug, path),
                    ).fetchone()
                    if row:
                        self._fts_delete("adrs_fts", row[0])
                cur.execute(
                    "DELETE FROM adrs WHERE repo=? AND path=?", (slug, path)
                )

    # ------------------------------------------------------------------
    # Searches
    # ------------------------------------------------------------------

    def search_reviews(
        self,
        query: str,
        repo: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        """Full-text search across indexed reviews.

        `query` is a free-text phrase — we quote-escape it for FTS. `repo`
        optionally narrows to a single owner/name slug (validated to
        defeat path-traversal in SQL construction). `limit` is clamped to
        a sane upper bound to bound MCP response size."""
        if repo is not None:
            validate_repo_slug(repo)
        limit = max(1, min(int(limit), 200))

        with self._lock:
            cur = self._con.cursor()

            rows: List[tuple] = []
            if self._fts5:
                q = _sanitize_fts_query(query)
                if q:
                    sql = (
                        "SELECT r.repo, r.pr_number, r.verdict, r.timestamp, "
                        "r.merged_at, r.head_sha, r.url, r.body "
                        "FROM reviews_fts f JOIN reviews r ON r.rowid = f.rowid "
                        "WHERE reviews_fts MATCH ? "
                    )
                    args: list = [q]
                    if repo:
                        sql += "AND r.repo = ? "
                        args.append(repo)
                    sql += "ORDER BY r.timestamp DESC LIMIT ?"
                    args.append(limit)
                    try:
                        rows = list(cur.execute(sql, args))
                    except sqlite3.OperationalError as e:
                        # Any residual FTS parse error → fall through to LIKE.
                        _log(f"FTS MATCH failed ({e}); falling back to LIKE")
                        rows = []

            if not rows:
                # LIKE fallback (also the only path when FTS5 isn't available).
                like_pat = f"%{query}%" if query else "%"
                sql = (
                    "SELECT repo, pr_number, verdict, timestamp, merged_at, "
                    "head_sha, url, body FROM reviews WHERE body LIKE ? "
                )
                args = [like_pat]
                if repo:
                    sql += "AND repo = ? "
                    args.append(repo)
                sql += "ORDER BY timestamp DESC LIMIT ?"
                args.append(limit)
                rows = list(cur.execute(sql, args))

        out: List[dict] = []
        for row in rows:
            body = row[7] or ""
            out.append(
                {
                    "repo": row[0],
                    "pr_number": int(row[1]),
                    "verdict": row[2] or "",
                    "timestamp": row[3] or "",
                    "merged_at": row[4],
                    "head_sha": row[5] or "",
                    "url": row[6] or "",
                    "snippet": _make_snippet(body, query),
                }
            )
        return out

    def search_adrs(
        self,
        query: str,
        repo: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        """Full-text search across indexed ADRs from every known repo."""
        if repo is not None:
            validate_repo_slug(repo)
        limit = max(1, min(int(limit), 100))

        with self._lock:
            cur = self._con.cursor()

            rows: List[tuple] = []
            if self._fts5:
                q = _sanitize_fts_query(query)
                if q:
                    sql = (
                        "SELECT a.repo, a.path, a.id, a.title, a.status, a.body "
                        "FROM adrs_fts f JOIN adrs a ON a.rowid = f.rowid "
                        "WHERE adrs_fts MATCH ? "
                    )
                    args: list = [q]
                    if repo:
                        sql += "AND a.repo = ? "
                        args.append(repo)
                    sql += "LIMIT ?"
                    args.append(limit)
                    try:
                        rows = list(cur.execute(sql, args))
                    except sqlite3.OperationalError as e:
                        _log(f"ADR FTS MATCH failed ({e}); falling back to LIKE")
                        rows = []

            if not rows:
                like_pat = f"%{query}%" if query else "%"
                sql = (
                    "SELECT repo, path, id, title, status, body "
                    "FROM adrs WHERE (title LIKE ? OR body LIKE ?) "
                )
                args = [like_pat, like_pat]
                if repo:
                    sql += "AND repo = ? "
                    args.append(repo)
                sql += "LIMIT ?"
                args.append(limit)
                rows = list(cur.execute(sql, args))

        out: List[dict] = []
        for row in rows:
            body = row[5] or ""
            # `_make_snippet` already runs the snippet through the
            # secrets-scan redaction pipeline — ADR content comes from
            # third-party cloned repos (the operator's `~/repos`), so a
            # leaked key in an ADR body must be scrubbed before it flows
            # out through this MCP tool. Same contract as search_reviews.
            excerpt = _make_snippet(body, query)
            # Titles can also carry injection content if a cloned repo's
            # ADR has a deliberately crafted H1. Redact uniformly.
            title = _redact_snippet(row[3] or "")
            out.append(
                {
                    "repo": row[0],
                    "path": row[1],
                    "id": row[2] or "",
                    "title": title,
                    "status": row[4] or "",
                    "excerpt": excerpt,
                }
            )
        return out

    def list_merged_prs(
        self,
        repo: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        """Return reviews whose `merged_at` is set, newest-first.

        Sits on the same DB as search_reviews — exists here so the MCP
        tool doesn't have to reach into private SQL itself."""
        if repo is not None:
            validate_repo_slug(repo)
        limit = max(1, min(int(limit), 200))
        sql = (
            "SELECT repo, pr_number, verdict, timestamp, merged_at, head_sha, url "
            "FROM reviews WHERE merged_at IS NOT NULL "
        )
        args: list = []
        if repo:
            sql += "AND repo = ? "
            args.append(repo)
        if since:
            sql += "AND merged_at >= ? "
            args.append(since)
        sql += "ORDER BY merged_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            cur = self._con.cursor()
            rows = list(cur.execute(sql, args))
        return [
            {
                "repo": row[0],
                "pr_number": int(row[1]),
                "verdict": row[2] or "",
                "timestamp": row[3] or "",
                "merged_at": row[4],
                "head_sha": row[5] or "",
                "url": row[6] or "",
            }
            for row in rows
        ]
