"""Fetch CI test results for the PR's head commit and render them
as reviewer context.

Most AI code review bots only see the diff. They don't know which
tests are passing, failing, or still running against the change. A
reviewer-with-test-context can correlate "this diff touches
`orders/handler.py`" with "test_concurrent_orders is failing" and
flag the likely relationship.

Scope v1:
    - GitHub Actions (via GitHub Checks API)
    - Captures high-level pass/fail counts + failing check names + URLs
    - Correlates failing checks with touched files by path-token overlap
    - NO test-framework-specific parsing (pytest/jest/etc.) — that's v2

Failure modes (all non-fatal — skip the section, log, continue):
    - No check runs on the SHA yet
    - GitHub API rate-limited
    - Network error
    - Checks still in progress (conclusion=null)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence
import os
import re

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

_MAX_FAILING_CHECKS = 10
_MAX_SUMMARY_LEN = 500


@dataclass(frozen=True)
class CheckRun:
    """A single GitHub Checks API result (one CI job)."""

    name: str
    conclusion: str  # "success", "failure", "cancelled", "skipped", "timed_out", "neutral", "" if in progress
    status: str  # "completed", "in_progress", "queued"
    summary: str  # output.summary, truncated
    html_url: str


@dataclass
class CIResult:
    """Aggregated CI status for a SHA."""

    fetched: bool = False  # Did we successfully hit the API?
    total: int = 0
    passing: int = 0
    failing: int = 0
    in_progress: int = 0
    checks: List[CheckRun] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return self.failing > 0

    def failing_checks(self) -> List[CheckRun]:
        return [c for c in self.checks if c.conclusion == "failure"]


def _github_session():
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_ci_results(
    token: str,
    owner: str,
    repo: str,
    sha: str,
    *,
    timeout: int = 10,
) -> CIResult:
    """Hit the GitHub Checks API for `sha` and return a CIResult.

    Never raises — all errors result in an empty CIResult with
    `fetched=False`. The caller decides what to log.
    """
    if requests is None:  # pragma: no cover
        return CIResult()
    session = _github_session()
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/check-runs"
    try:
        resp = session.get(
            url,
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            return CIResult()
        data = resp.json()
    except Exception:  # noqa: BLE001
        return CIResult()

    raw_runs = data.get("check_runs") if isinstance(data, dict) else None
    if not isinstance(raw_runs, list):
        return CIResult(fetched=True)

    checks: List[CheckRun] = []
    passing = failing = in_progress = 0
    for raw in raw_runs:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", ""))[:100]
        status = str(raw.get("status", "")).lower()
        conclusion = str(raw.get("conclusion", "") or "").lower()
        output = raw.get("output") or {}
        summary_raw = str(output.get("summary") or "")[:_MAX_SUMMARY_LEN]
        # Strip control chars that slip through API responses
        summary = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", summary_raw)
        html_url = str(raw.get("html_url", ""))

        checks.append(
            CheckRun(
                name=name,
                conclusion=conclusion,
                status=status,
                summary=summary,
                html_url=html_url,
            )
        )

        if status != "completed":
            in_progress += 1
        elif conclusion == "success":
            passing += 1
        elif conclusion == "failure":
            failing += 1
        # skipped / cancelled / neutral / timed_out all drop out of pass/fail counts

    return CIResult(
        fetched=True,
        total=len(checks),
        passing=passing,
        failing=failing,
        in_progress=in_progress,
        checks=checks,
    )


# --------------------------------------------------------------------------
# Correlation heuristic
# --------------------------------------------------------------------------


def _split_tokens(text: str) -> set:
    """Extract word-tokens ≥3 chars, splitting on /, -, _, ., space, and
    CamelCase boundaries. Used both for touched-file paths and check
    names/summaries so the two sides tokenize the same way."""
    # Insert space at CamelCase boundaries so FooBar → "Foo Bar"
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    parts = re.split(r"[\/\-_.\s]+", text.lower())
    return {p for p in parts if len(p) >= 3}


_MIN_PREFIX = 4  # how many leading chars must match for a loose overlap


def _loose_overlap(a: set, b: set, min_prefix: int = _MIN_PREFIX) -> bool:
    """Return True if any token in `a` shares its first `min_prefix` chars
    with any token in `b`. Catches singular/plural and stem variants
    (order / orders / ordered) without a real stemmer."""
    a_prefixes = {t[:min_prefix] for t in a if len(t) >= min_prefix}
    b_prefixes = {t[:min_prefix] for t in b if len(t) >= min_prefix}
    return bool(a_prefixes & b_prefixes)


def correlate_failing_checks(
    ci: CIResult, touched_files: Sequence[str]
) -> List[CheckRun]:
    """Return the subset of failing checks that look related to touched files.

    Match criterion: the check's name or summary shares a token-prefix
    with any touched file path. Conservative — a check that doesn't
    advertise file paths in its name/summary won't match, and that's OK:
    we'd rather miss a correlation than surface a noisy false signal.
    """
    if not ci.fetched or not ci.has_failures or not touched_files:
        return []
    touched_tokens: set = set()
    for f in touched_files:
        touched_tokens |= _split_tokens(os.path.splitext(f)[0])

    matched: List[CheckRun] = []
    for check in ci.failing_checks():
        check_tokens = _split_tokens(f"{check.name} {check.summary}")
        if _loose_overlap(check_tokens, touched_tokens):
            matched.append(check)
    return matched[:_MAX_FAILING_CHECKS]


# --------------------------------------------------------------------------
# Prompt rendering
# --------------------------------------------------------------------------


def render_ci_addendum(
    ci: CIResult, correlated_failures: Optional[Sequence[CheckRun]] = None
) -> str:
    """Format CI results for inclusion in the reviewer's system prompt."""
    if not ci.fetched:
        return ""
    if ci.total == 0:
        return ""
    parts = ["## CI test results for this PR\n"]
    parts.append(
        f"- Total checks: {ci.total}"
    )
    parts.append(f"- Passing: {ci.passing}")
    parts.append(f"- Failing: {ci.failing}")
    if ci.in_progress > 0:
        parts.append(f"- In progress: {ci.in_progress}")
    parts.append("")

    failing = ci.failing_checks()
    if failing:
        parts.append("### Failing checks\n")
        for check in failing[:_MAX_FAILING_CHECKS]:
            line = f"- **{check.name}**"
            if check.html_url:
                line += f" ([log]({check.html_url}))"
            parts.append(line)
            if check.summary:
                # Inline the summary (trim whitespace, no blank-line separator)
                snippet = " ".join(check.summary.split())[:200]
                parts.append(f"  > {snippet}")
        parts.append("")

    if correlated_failures:
        parts.append("### Failing checks likely related to touched files\n")
        for check in correlated_failures:
            parts.append(f"- **{check.name}**: path tokens overlap with this PR's changes")
        parts.append("")
        parts.append(
            "Consider calling out any PR change that looks likely to have caused "
            "these failures, or ask the author to rerun / investigate before merging.\n"
        )

    return "\n".join(parts)
