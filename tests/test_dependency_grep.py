"""Tests for dependency_grep: scan known-repo manifests for a package reference.

Build synthetic mini-repos under tmp_path with realistic package.json,
requirements.txt, go.mod, etc. content, then assert that scan_all finds the
expected hits and honors the limit parameter.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cross_repo  # noqa: E402
import dependency_grep  # noqa: E402


def _make_repo(root, name: str, files: dict) -> str:
    p = os.path.join(str(root), name)
    os.makedirs(os.path.join(p, ".git"), exist_ok=True)
    with open(os.path.join(p, ".git", "config"), "w") as fh:
        fh.write(f'[remote "origin"]\n\turl = git@github.com:chandler/{name}.git\n')
    for rel, content in files.items():
        abs_p = os.path.join(p, rel)
        os.makedirs(os.path.dirname(abs_p) or ".", exist_ok=True)
        with open(abs_p, "w") as fh:
            fh.write(content)
    return p


@pytest.fixture(autouse=True)
def _clear_cache():
    cross_repo._clear_cache()
    yield
    cross_repo._clear_cache()


def test_scan_finds_in_package_json(tmp_path, monkeypatch):
    _make_repo(
        tmp_path,
        "proj-a",
        {
            "package.json": """{
  "name": "proj-a",
  "dependencies": {
    "@anthropic-ai/sdk": "^0.33.0",
    "lodash": "^4.17.0"
  }
}""",
        },
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    hits = dependency_grep.scan_all("@anthropic-ai/sdk")
    assert len(hits) == 1
    assert hits[0].repo == "chandler/proj-a"
    assert "package.json" in hits[0].path


def test_scan_finds_in_requirements_txt(tmp_path, monkeypatch):
    _make_repo(
        tmp_path,
        "proj-b",
        {"requirements.txt": "flask==2.0\nrequests==2.31.0\nsqlalchemy\n"},
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    hits = dependency_grep.scan_all("requests")
    assert len(hits) == 1
    assert "requirements.txt" in hits[0].path


def test_scan_finds_in_go_mod(tmp_path, monkeypatch):
    _make_repo(
        tmp_path,
        "proj-c",
        {
            "go.mod": """module example.com/foo

go 1.21

require (
    github.com/foo/bar v1.2.3
    github.com/other/pkg v0.1.0
)
""",
        },
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    hits = dependency_grep.scan_all("github.com/foo/bar")
    assert len(hits) == 1
    assert "go.mod" in hits[0].path


def test_scan_finds_in_pyproject_toml(tmp_path, monkeypatch):
    _make_repo(
        tmp_path,
        "proj-d",
        {
            "pyproject.toml": """[project]
dependencies = [
    "httpx>=0.27",
    "pydantic~=2.5",
]
""",
        },
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    hits = dependency_grep.scan_all("httpx")
    assert len(hits) == 1


def test_scan_finds_in_cargo_toml(tmp_path, monkeypatch):
    _make_repo(
        tmp_path,
        "proj-e",
        {
            "Cargo.toml": """[package]
name = "demo"

[dependencies]
serde = "1.0"
tokio = { version = "1", features = ["full"] }
""",
        },
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    hits = dependency_grep.scan_all("serde")
    assert len(hits) == 1


def test_scan_finds_in_package_swift(tmp_path, monkeypatch):
    _make_repo(
        tmp_path,
        "proj-f",
        {
            "Package.swift": """// swift-tools-version:5.9
let package = Package(
    name: "demo",
    dependencies: [
        .package(url: "https://github.com/apple/swift-nio.git", from: "2.0.0"),
    ]
)
""",
        },
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    hits = dependency_grep.scan_all("swift-nio")
    assert len(hits) == 1


def test_scan_aggregates_across_repos(tmp_path, monkeypatch):
    _make_repo(
        tmp_path,
        "alpha",
        {"package.json": '{"dependencies":{"axios":"^1.0"}}'},
    )
    _make_repo(
        tmp_path,
        "beta",
        {"package.json": '{"dependencies":{"axios":"^0.27"}}'},
    )
    _make_repo(
        tmp_path,
        "gamma",
        {"package.json": '{"dependencies":{"lodash":"^4"}}'},
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    hits = dependency_grep.scan_all("axios")
    slugs = sorted(h.repo for h in hits)
    assert slugs == ["chandler/alpha", "chandler/beta"]


def test_scan_respects_limit(tmp_path, monkeypatch):
    for i in range(10):
        _make_repo(
            tmp_path,
            f"r{i}",
            {"package.json": '{"dependencies":{"axios":"1.0"}}'},
        )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    hits = dependency_grep.scan_all("axios", limit=3)
    assert len(hits) == 3


def test_scan_no_hits_returns_empty(tmp_path, monkeypatch):
    _make_repo(
        tmp_path,
        "alpha",
        {"package.json": '{"dependencies":{"lodash":"^4"}}'},
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    assert dependency_grep.scan_all("nonexistent-pkg") == []


def test_scan_skips_repos_without_manifests(tmp_path, monkeypatch):
    _make_repo(tmp_path, "alpha", {"README.md": "hi"})
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", str(tmp_path))
    assert dependency_grep.scan_all("anything") == []


def test_hit_dataclass_fields():
    """Hit exposes repo, path, line, matched."""
    h = dependency_grep.Hit(
        repo="owner/name", path="package.json", line=3, matched='"axios": "1.0"'
    )
    assert h.repo == "owner/name"
    assert h.path == "package.json"
    assert h.line == 3
    assert "axios" in h.matched


def test_scan_uses_explicit_root(tmp_path, monkeypatch):
    """Explicit root overrides env var."""
    _make_repo(
        tmp_path,
        "alpha",
        {"package.json": '{"dependencies":{"axios":"1.0"}}'},
    )
    monkeypatch.setenv("SENESCHAL_REPOS_ROOT", "/nonexistent")
    hits = dependency_grep.scan_all("axios", root=str(tmp_path))
    assert len(hits) == 1
