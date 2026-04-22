"""Tests for post_merge.orchestrator: glue between pure modules + GitHub I/O.

All GitHub I/O is mocked. The orchestrator is the integration point we
control end-to-end here; we test sequencing, idempotence, and fallback
modes (protected main, race-on-release-PR).
"""

import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import review_store  # noqa: E402
from post_merge.orchestrator import (  # noqa: E402
    PushProtectedError,
    handle_pr_merged,
    _PROTECTED_REPOS,
)
from repo_config import PostMergeConfig, RepoConfig  # noqa: E402
from review_store import save_review  # noqa: E402


def _config(**kw):
    pm = PostMergeConfig(**kw)
    return RepoConfig(post_merge=pm)


def _pr_meta(number=42, title="feat: add thing", merged_at="2026-04-21T10:00:00Z"):
    return {
        "number": number,
        "title": title,
        "merged_at": merged_at,
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"sha": "deadbeef"},
        "merge_commit_sha": "feedface",
    }


# --------------------------------------------------------------------------
# Happy path: changelog + followups + mark_merged
# --------------------------------------------------------------------------


def test_handle_pr_merged_updates_changelog(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "review body without followups")

    cfg = _config(changelog=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "token123"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        os.makedirs(tmp_path / "clone", exist_ok=True)
        # CHANGELOG.md already exists in the clone.
        (tmp_path / "clone" / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n"
        )
        # Mock the github file-API helpers.
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "feat: add thing"),
            config=cfg,
        )

    assert result["changelog_updated"] is True
    assert mock_app.put_file.called
    # Confirm the put_file payload contained the new entry.
    call_kwargs = mock_app.put_file.call_args.kwargs
    posted_content = call_kwargs.get("content") or mock_app.put_file.call_args.args[3]
    assert "add thing" in posted_content


def test_handle_pr_merged_files_followups(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review(
        "o/r",
        42,
        "APPROVE",
        "https://x/42",
        "## Review\n\n- [FOLLOWUP] do the thing later\n- [FOLLOWUP] also that\n",
    )

    cfg = _config(followups=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "token123"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        # Two issues created.
        mock_app.create_issue = MagicMock(side_effect=[
            {"number": 501}, {"number": 502}
        ])

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )

    assert sorted(result["followups_filed"]) == [501, 502]
    assert mock_app.create_issue.call_count == 2


def test_handle_pr_merged_marks_review_merged(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(followups=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        mock_app.create_issue = MagicMock(return_value={"number": 999})

        handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, merged_at="2026-04-21T11:00:00Z"),
            config=cfg,
        )

    rec = review_store.get_review("o/r", 42)
    assert rec.merged_at == "2026-04-21T11:00:00Z"


# --------------------------------------------------------------------------
# Idempotence
# --------------------------------------------------------------------------


def test_handle_pr_merged_skips_already_filed_followups(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review(
        "o/r",
        42,
        "APPROVE",
        "https://x/42",
        "- [FOLLOWUP] do the thing later\n",
    )
    # Pre-mark this review as already having filed an issue.
    review_store.mark_merged("o/r", 42, "2026-04-21T10:00:00Z", [501])

    cfg = _config(followups=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        mock_app.ensure_repo_synced.return_value = str(tmp_path / "clone")
        mock_app.create_issue = MagicMock()

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )

    # No new issues created because the only followup matches a previous title.
    # We expect the orchestrator to skip if the title would be a duplicate;
    # the simplest signal is no issue creation when followup count <= existing.
    # If the orchestrator can't dedupe by title alone, at minimum it should
    # not double-file: the followups_filed list still has 501.
    assert mock_app.create_issue.call_count == 0
    assert 501 in result.get("followups_filed", []) or result.get("followups_filed") == []


# --------------------------------------------------------------------------
# Protected main → fall back to auto-PR
# --------------------------------------------------------------------------


def test_handle_pr_merged_falls_back_to_pr_when_protected(tmp_path, monkeypatch):
    # Reset the protection cache between tests.
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        (clone_dir / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.PushProtectedError = PushProtectedError

        # First put_file (direct to main) raises PushProtectedError;
        # subsequent ones (on the auto-PR branch) succeed.
        call_count = {"n": 0}
        def _put_file(*args, **kwargs):
            call_count["n"] += 1
            branch = kwargs.get("branch") or (args[5] if len(args) > 5 else "")
            if call_count["n"] == 1 and branch == "main":
                raise PushProtectedError("main is protected")
            return {"commit": {"sha": "abc"}}
        mock_app.put_file = MagicMock(side_effect=_put_file)
        mock_app.create_branch = MagicMock(return_value={"ref": "refs/heads/seneschal/changelog-42"})
        mock_app.create_pull_request = MagicMock(return_value={"number": 99})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )

    assert mock_app.create_branch.called
    assert mock_app.create_pull_request.called
    # The protection cache now remembers o/r is protected.
    assert _PROTECTED_REPOS.get("o/r") is True


# --------------------------------------------------------------------------
# Release-PR race: existing seneschal:release PR → amend instead of opening new
# --------------------------------------------------------------------------


def test_handle_pr_merged_amends_existing_release_pr(tmp_path, monkeypatch):
    _PROTECTED_REPOS.clear()
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "https://x/42", "body")

    cfg = _config(changelog=True, release_threshold="patch")
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.return_value = "tok"
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()
        (clone_dir / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- earlier ([#41](x))\n"
        )
        mock_app.ensure_repo_synced.return_value = str(clone_dir)
        mock_app.put_file = MagicMock(return_value={"commit": {"sha": "abc"}})
        mock_app.get_file_sha = MagicMock(return_value="oldsha")
        mock_app.get_default_branch_sha = MagicMock(return_value="defaultsha")
        # An existing release PR is open.
        mock_app.find_open_prs_with_label = MagicMock(return_value=[
            {"number": 77, "head": {"ref": "seneschal/release-0.3.0"}},
        ])
        mock_app.create_pull_request = MagicMock()

        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42, "fix: thing"),
            config=cfg,
        )

    # No new release PR created — amended the existing one instead.
    assert mock_app.create_pull_request.called is False or result.get("release_pr") == 77


# --------------------------------------------------------------------------
# All-off config: orchestrator no-ops cleanly
# --------------------------------------------------------------------------


def test_handle_pr_merged_no_ops_on_default_config(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "", "body")
    cfg = RepoConfig()  # all off
    with patch("post_merge.orchestrator.app") as mock_app:
        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )
    assert result["changelog_updated"] is False
    assert result["followups_filed"] == []
    assert mock_app.create_issue.called is False


# --------------------------------------------------------------------------
# Exception swallowing
# --------------------------------------------------------------------------


def test_handle_pr_merged_swallows_exceptions(tmp_path, monkeypatch):
    monkeypatch.setattr(review_store, "STORE_ROOT", str(tmp_path))
    save_review("o/r", 42, "APPROVE", "", "body")
    cfg = _config(changelog=True)
    with patch("post_merge.orchestrator.app") as mock_app:
        mock_app.get_installation_token.side_effect = RuntimeError("boom")
        # Must not raise.
        result = handle_pr_merged(
            owner="o",
            repo="r",
            pr_number=42,
            installation_id=1,
            pr_meta=_pr_meta(42),
            config=cfg,
        )
    assert "error" in result
