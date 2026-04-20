"""Unit tests for code-reviewer risk scorer."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk import PRFile, score_risk  # noqa: E402


def f(name, adds=0, dels=0, status="modified"):
    return PRFile(filename=name, additions=adds, deletions=dels, status=status)


def test_trivial_change_is_low_risk():
    result = score_risk([f("README.md", 1, 0)])
    assert result.level == "low"
    assert result.score == 0


def test_medium_diff_bumps_to_medium():
    files = [
        f("src/foo.py", 100, 50),
        f("src/bar.py", 60, 20),
        f("src/baz.py", 40, 0),
    ]
    result = score_risk(files)
    assert result.level == "medium"
    assert any("Substantial diff" in r for r in result.reasons)


def test_large_pr_is_medium_risk():
    # Pure size (25 files, 875 lines) now caps at MEDIUM. A PR can be big
    # without being high-risk — "you should review this" (medium) is the
    # honest signal, and HIGH is reserved for size + sensitive/infra/
    # migration/secret signals together.
    files = [f(f"src/file{i}.py", 30, 5) for i in range(25)]
    result = score_risk(files)
    assert result.level == "medium"
    assert any("Large surface area" in r for r in result.reasons)
    assert any("Large diff" in r for r in result.reasons)


def test_large_pr_touching_auth_is_high_risk():
    # Size + sensitive path is the combo that justifies HIGH.
    files = [f(f"src/file{i}.py", 30, 5) for i in range(25)]
    files.append(f("internal/auth/login.go", 20, 5))
    result = score_risk(files)
    assert result.level == "high"


def test_auth_path_escalates():
    files = [f("internal/auth/login.go", 20, 5)]
    result = score_risk(files)
    assert result.level == "medium"
    assert any("auth/security" in r for r in result.reasons)


def test_multiple_auth_files_push_higher():
    files = [
        f("internal/auth/login.go", 10, 5),
        f("internal/auth/session.go", 15, 2),
    ]
    result = score_risk(files)
    assert result.score >= 3


def test_workflow_change_is_infra():
    result = score_risk([f(".github/workflows/ci.yml", 10, 2)])
    assert any("infra/CI" in r for r in result.reasons)


def test_dockerfile_counts_as_infra():
    result = score_risk([f("services/api/Dockerfile", 5, 2)])
    assert any("infra/CI" in r for r in result.reasons)


def test_dep_manifest_change_bumps():
    result = score_risk([f("package.json", 3, 1), f("package-lock.json", 200, 150)])
    assert any("Dependency manifest" in r for r in result.reasons)


def test_migration_file_bumps():
    result = score_risk([f("db/migrations/20260412_add_users.sql", 30, 0)])
    assert any("DB migrations" in r for r in result.reasons)


def test_secret_file_triggers_high_risk():
    result = score_risk([f(".env", 2, 0, status="added")])
    assert result.level == "high"
    assert any("Potential secret" in r for r in result.reasons)


def test_credentials_json_triggers_high():
    result = score_risk([f("config/credentials.json", 5, 0)])
    assert result.level == "high"


def test_multiple_removals_add_risk():
    files = [
        f("src/old_a.py", 0, 30, status="removed"),
        f("src/old_b.py", 0, 40, status="removed"),
        f("src/old_c.py", 0, 20, status="removed"),
    ]
    result = score_risk(files)
    assert any("removed" in r for r in result.reasons)


def test_score_label_matches_level():
    result = score_risk([f("README.md", 1, 0)])
    assert result.label == "risk:low"


def test_summary_contains_level_and_reasons():
    files = [f("auth/login.go", 50, 0), f("auth/session.go", 30, 0)]
    result = score_risk(files)
    text = result.summary()
    assert result.level.upper() in text
    assert "auth/security" in text


def test_empty_pr_is_low():
    result = score_risk([])
    assert result.level == "low"
    assert result.score == 0
