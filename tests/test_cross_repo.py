"""Tests for cross_repo: enumerate GitHub-origin repos under SENESCHAL_REPOS_ROOT.

Builds synthetic repo trees with hand-rolled `.git/config` files and asserts
that only GitHub origins are recognized, both SSH and HTTPS forms parse, and
a non-GitHub remote is silently skipped.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cross_repo  # noqa: E402


def _make_repo(root, name: str, origin_url: str) -> str:
    p = os.path.join(str(root), name)
    os.makedirs(os.path.join(p, ".git"), exist_ok=True)
    with open(os.path.join(p, ".git", "config"), "w") as fh:
        fh.write('[remote "origin"]\n')
        fh.write(f"\turl = {origin_url}\n")
    return p


@pytest.fixture(autouse=True)
def _clear_cache():
    """Each test gets a fresh cache."""
    cross_repo._clear_cache()
    yield
    cross_repo._clear_cache()


def test_known_repos_parses_ssh_origin(tmp_path, monkeypatch):
    _make_repo(tmp_path, "alpha", "git@github.com:owner/alpha.git")
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    repos = cross_repo.known_repos()
    slugs = [r.slug for r in repos]
    assert "owner/alpha" in slugs


def test_known_repos_parses_https_origin(tmp_path, monkeypatch):
    _make_repo(tmp_path, "beta", "https://github.com/owner/beta.git")
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    repos = cross_repo.known_repos()
    slugs = [r.slug for r in repos]
    assert "owner/beta" in slugs


def test_known_repos_handles_no_dot_git_suffix(tmp_path, monkeypatch):
    _make_repo(tmp_path, "gamma", "https://github.com/owner/gamma")
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    repos = cross_repo.known_repos()
    slugs = [r.slug for r in repos]
    assert "owner/gamma" in slugs


def test_known_repos_skips_non_github(tmp_path, monkeypatch):
    _make_repo(tmp_path, "alpha", "git@github.com:owner/alpha.git")
    _make_repo(tmp_path, "bitbucket", "git@bitbucket.org:owner/bb.git")
    _make_repo(tmp_path, "gitlab", "https://gitlab.com/owner/gl.git")
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    repos = cross_repo.known_repos()
    slugs = [r.slug for r in repos]
    assert slugs == ["owner/alpha"]


def test_known_repos_skips_missing_dot_git(tmp_path, monkeypatch):
    # Create a plain dir with no .git subdir.
    os.makedirs(os.path.join(str(tmp_path), "loose"))
    _make_repo(tmp_path, "alpha", "git@github.com:owner/alpha.git")
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    repos = cross_repo.known_repos()
    assert [r.slug for r in repos] == ["owner/alpha"]


def test_known_repos_caches_by_root(tmp_path, monkeypatch):
    """Repeated calls with the same root don't re-walk the filesystem."""
    _make_repo(tmp_path, "alpha", "git@github.com:owner/alpha.git")
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    first = cross_repo.known_repos()
    # Add a new repo under the same root.
    _make_repo(tmp_path, "beta", "git@github.com:owner/beta.git")
    second = cross_repo.known_repos()
    # Cached — doesn't see beta until cleared.
    assert [r.slug for r in second] == [r.slug for r in first]
    cross_repo._clear_cache()
    third = cross_repo.known_repos()
    assert len(third) == 2


def test_known_repos_uses_explicit_root(tmp_path, monkeypatch):
    """Explicit root arg overrides env var."""
    _make_repo(tmp_path, "alpha", "git@github.com:owner/alpha.git")
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", "/nonexistent")
    repos = cross_repo.known_repos(root=str(tmp_path))
    assert [r.slug for r in repos] == ["owner/alpha"]


def test_known_repos_returns_empty_for_missing_root(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(missing))
    assert cross_repo.known_repos() == []


def test_known_repos_skips_symlinked_subdir(tmp_path, monkeypatch):
    """A symlink to outside the root must not escalate enumeration."""
    outside = tmp_path / "outside"
    _make_repo(outside, "evil", "git@github.com:evil/repo.git")
    inside = tmp_path / "inside"
    inside.mkdir()
    link = inside / "link"
    try:
        os.symlink(str(outside / "evil"), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("no symlink support")
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(inside))
    repos = cross_repo.known_repos()
    # The symlink target's repo should not surface via enumeration.
    assert repos == []
