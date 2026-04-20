"""Risk scoring for pull requests.

Pure functions: given PR file metadata, return a structured risk score.
Designed to be deterministic and unit-testable without hitting GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Sequence


@dataclass(frozen=True)
class PRFile:
    """A single file in a pull request diff."""
    filename: str
    additions: int = 0
    deletions: int = 0
    status: str = "modified"  # added, removed, modified, renamed


@dataclass
class RiskScore:
    level: str  # "low", "medium", "high"
    score: int
    reasons: List[str] = field(default_factory=list)
    secret_files: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"risk:{self.level}"

    def summary(self) -> str:
        bullets = "\n".join(f"- {r}" for r in self.reasons) if self.reasons else "- No elevated risk signals."
        return f"**Risk: {self.level.upper()}** (score {self.score})\n\n{bullets}"


# Path fragments that indicate sensitive code. Case-insensitive substring match.
SENSITIVE_FRAGMENTS: Sequence[str] = (
    "auth",
    "login",
    "password",
    "secret",
    "token",
    "session",
    "permission",
    "credential",
    "oauth",
    "jwt",
)

# Paths that indicate infrastructure or CI.
INFRA_FRAGMENTS: Sequence[str] = (
    ".github/workflows/",
    "dockerfile",
    "docker-compose",
    "nginx",
    "systemd/",
    "terraform",
    "kubernetes",
    "k8s/",
    "helm/",
    ".circleci/",
    ".gitlab-ci",
)

# Dependency manifests — changes mean package churn.
DEP_FILES: Sequence[str] = (
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements.txt",
    "pyproject.toml",
    "poetry.lock",
    "go.mod",
    "go.sum",
    "gemfile",
    "gemfile.lock",
    "cargo.toml",
    "cargo.lock",
    "podfile",
    "podfile.lock",
    "package.resolved",
)

# Schema / migration paths.
MIGRATION_FRAGMENTS: Sequence[str] = (
    "/migrations/",
    "/migration/",
    "alembic/",
    "schema.sql",
    "schema.prisma",
    "db/migrate/",
)

# Obvious secrets that should never be in a PR.
SECRET_FILES: Sequence[str] = (
    ".env",
    "credentials.json",
    "secrets.yaml",
    "secrets.yml",
    "id_rsa",
    "private.key",
)


def _is_match(filename: str, fragments: Iterable[str]) -> bool:
    lower = filename.lower()
    return any(f in lower for f in fragments)


def _basename(filename: str) -> str:
    return filename.lower().rsplit("/", 1)[-1]


def _is_exact(filename: str, names: Iterable[str]) -> bool:
    base = _basename(filename)
    return any(base == n for n in names)


def _is_secret(filename: str) -> bool:
    base = _basename(filename)
    if base.startswith(".env"):
        return True
    return base in SECRET_FILES


def _total_lines(files: Sequence[PRFile]) -> int:
    return sum(f.additions + f.deletions for f in files)


def _score_file_count(n: int) -> int:
    if n >= 20:
        return 4
    if n >= 10:
        return 3
    if n >= 5:
        return 2
    if n >= 3:
        return 1
    return 0


def _score_line_count(n: int) -> int:
    if n >= 500:
        return 4
    if n >= 200:
        return 3
    if n >= 100:
        return 2
    if n >= 50:
        return 1
    return 0


def score_risk(files: Sequence[PRFile]) -> RiskScore:
    """Compute a RiskScore from PR files.

    Scoring thresholds (tuned for solo/small-team repos):
        0-3    low
        4-9    medium
        10+    high

    The HIGH threshold is set deliberately above the combined signal of
    "~400-line feature PR + a lockfile update" (which scores 2+3+2 = 7).
    That kind of PR is normal feature work, not actually high-risk, and
    BLOCKER severity on it was a false positive that contradicted the
    Claude-level review verdict. Truly high-risk PRs — large (500+ lines)
    AND touch sensitive paths, migrations, infra, or secrets — still
    easily clear 10.
    """
    score = 0
    reasons: List[str] = []
    secret_files: List[str] = []

    n_files = len(files)
    total_lines = _total_lines(files)

    # 1. Size signals.
    file_score = _score_file_count(n_files)
    if file_score > 0:
        score += file_score
        if file_score >= 3:
            reasons.append(f"Wide surface area ({n_files} files changed)")
        if file_score >= 4:
            reasons[-1] = f"Large surface area ({n_files} files changed)"

    line_score = _score_line_count(total_lines)
    if line_score > 0:
        score += line_score
        if line_score >= 3:
            label = "Substantial diff" if line_score == 3 else "Large diff"
            reasons.append(f"{label} ({total_lines} lines)")

    # 2. Path-based signals (each family counted once).
    # Raised single-hit from 3→4 so touching auth/security alone escalates
    # to MEDIUM (risk score ≥4), matching the intent that sensitive paths
    # deserve reviewer attention even in a tiny diff.
    def touched(fragments, label):
        nonlocal score
        hits = [f.filename for f in files if _is_match(f.filename, fragments)]
        if hits:
            score += 5 if len(hits) >= 2 else 4
            reasons.append(f"{label}: {', '.join(hits[:3])}")

    touched(SENSITIVE_FRAGMENTS, "Touches auth/security surface")
    touched(INFRA_FRAGMENTS, "Touches infra/CI")
    touched(MIGRATION_FRAGMENTS, "Touches DB migrations")

    # 3. Dependency manifest churn.
    dep_hits = [f.filename for f in files if _is_exact(f.filename, DEP_FILES)]
    if dep_hits:
        score += 2
        reasons.append(f"Dependency manifest changed: {', '.join(dep_hits[:3])}")

    # 4. Secret files — huge red flag even if small.
    # Bonus must exceed the HIGH threshold on its own (≥10) so a 5-line
    # diff containing .env still trips HIGH regardless of other signals.
    secret_hits = [f.filename for f in files if _is_secret(f.filename)]
    if secret_hits:
        score += 10
        secret_files = list(secret_hits)
        reasons.append(f"Potential secret file: {', '.join(secret_hits[:3])}")

    # 5. Removed files carry asymmetric risk.
    removals = [f.filename for f in files if f.status == "removed"]
    if len(removals) >= 3:
        score += 2
        reasons.append(f"Multiple files removed ({len(removals)})")
    elif removals:
        score += 1

    # Classify.
    if score >= 10:
        level = "high"
    elif score >= 4:
        level = "medium"
    else:
        level = "low"

    return RiskScore(level=level, score=score, reasons=reasons, secret_files=secret_files)
