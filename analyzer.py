"""PR analyzer — coordinates all analysis modules into a single structured report.

Pure composition: takes inputs (PR metadata, files, diff, other PRs, repo dir)
and produces a `PRAnalysis` containing labels, a formatted markdown body,
and an optional system-prompt addendum for the downstream Claude review.

I/O (fetching PRs, posting comments) lives in `app.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from breaking_changes import BreakingChange, detect_breaking_changes, summarize_breaking
from context_loader import BlastRadius, compute_blast_radius
from findings import Finding, FindingSet, Severity
from quality_scan import QualityHit, scan_quality, summarize_quality
from related_prs import OtherPR, RelatedPR, find_related_prs, summarize_related
from repo_config import RepoConfig
from review_memory import ReviewMemory
from risk import PRFile, RiskScore, score_risk
from scope import ScopeReport, detect_scope_drift
from secrets_scan import SecretHit, scan_diff, summarize_secrets
from summary import summarize_diff
from test_gaps import TestGap, find_test_gaps, summarize_gaps
from title_check import TitleReport, check_title


@dataclass
class PRAnalysis:
    risk: RiskScore
    scope: ScopeReport
    gaps: List[TestGap] = field(default_factory=list)
    related: List[RelatedPR] = field(default_factory=list)
    blast: BlastRadius = field(default_factory=BlastRadius)
    config: RepoConfig = field(default_factory=RepoConfig)
    findings: FindingSet = field(default_factory=FindingSet)
    diff_summary: str = ""
    title_report: TitleReport = field(default_factory=lambda: TitleReport(level="ok"))
    memory: ReviewMemory = field(default_factory=ReviewMemory)
    breaking: List[BreakingChange] = field(default_factory=list)
    secrets: List[SecretHit] = field(default_factory=list)
    quality: List[QualityHit] = field(default_factory=list)

    def labels(self) -> List[str]:
        out = [self.risk.label]
        if self.scope.drifted:
            out.append("scope:drifted")
        if self.gaps:
            out.append("tests:missing")
        if self.findings.has_blockers():
            out.append("review:blocker")
        if self.breaking:
            out.append("breaking-change")
        if self.secrets:
            out.append("security:secret-leak")
        return out

    def body(self) -> str:
        parts = [
            "## Pre-review analysis",
            "",
            f"**Summary:** {self.diff_summary}" if self.diff_summary else "",
            "",
            f"**Headline:** {self.findings.headline()}",
            "",
            "### Automated findings",
            "",
            self.findings.render_grouped(),
            "",
            "### Details",
            "",
            self.risk.summary(),
            "",
            self.scope.summary(),
            "",
            summarize_gaps(self.gaps),
            "",
            summarize_breaking(self.breaking),
            "",
            summarize_secrets(self.secrets),
            "",
            summarize_quality(self.quality),
            "",
            summarize_related(self.related),
        ]
        if self.blast.symbols:
            parts.append("")
            parts.append("### Blast radius")
            parts.append("")
            parts.append(self.blast.summary())
        parts.append("")
        parts.append("---")
        parts.append("*Automated pre-review by ch-code-reviewer. A full Claude review follows.*")
        return "\n".join(p for p in parts if p != "" or parts[parts.index(p) - 1] != "")

    def inline_comments(self) -> List[dict]:
        """Build GitHub review inline comments from findings that have file+line.

        Returns a list of dicts compatible with the GitHub review API's
        'comments' parameter: [{path, line, body, side}]. Only findings
        with both a file and a line attached become inline comments —
        file-level findings stay in the analysis body.
        """
        out: List[dict] = []
        for finding in self.findings.sorted():
            if not finding.file or not finding.line:
                continue
            body = f"**[{finding.severity.label}] {finding.title}**"
            if finding.detail:
                body += f"\n\n{finding.detail}"
            out.append({
                "path": finding.file,
                "line": finding.line,
                "side": "RIGHT",
                "body": body,
            })
        return out

    def prompt_addendum(self) -> str:
        """Extra context to inject into the Claude review prompt."""
        parts: List[str] = []
        cfg_addendum = self.config.system_prompt_addendum()
        if cfg_addendum:
            parts.append(cfg_addendum)
        memory_block = self.memory.prompt_block()
        if memory_block:
            parts.append(memory_block)
        if self.blast.symbols:
            parts.append(self.blast.as_prompt_section())
        if self.scope.drifted:
            parts.append(
                "\n**Note:** Automated scope-drift detection flagged this PR as "
                f"touching unrelated areas ({', '.join(self.scope.top_level_dirs)}). "
                "Consider recommending the author split it.\n"
            )
        if self.gaps:
            gap_names = ", ".join(f"`{g.symbol}`" for g in self.gaps[:10])
            parts.append(
                f"\n**Note:** New symbols without test references: {gap_names}. "
                "Consider flagging missing tests in your review if they are non-trivial.\n"
            )
        return "\n".join(p for p in parts if p)


def _risk_to_finding(risk: RiskScore) -> Optional[Finding]:
    if risk.level == "high":
        return Finding(
            severity=Severity.BLOCKER,
            category="risk",
            title=f"High-risk change (score {risk.score})",
            detail="; ".join(risk.reasons) if risk.reasons else "",
        )
    if risk.level == "medium":
        return Finding(
            severity=Severity.WARNING,
            category="risk",
            title=f"Medium-risk change (score {risk.score})",
            detail="; ".join(risk.reasons) if risk.reasons else "",
        )
    return None


def _scope_to_finding(scope: ScopeReport) -> Optional[Finding]:
    if not scope.drifted:
        return None
    return Finding(
        severity=Severity.WARNING,
        category="scope",
        title="Scope drift detected",
        detail=scope.reason,
    )


def _title_to_finding(report: TitleReport) -> Optional[Finding]:
    if report.is_ok:
        return None
    sev = Severity.WARNING if report.level == "warning" else Severity.NIT
    return Finding(
        severity=sev,
        category="title",
        title="PR title quality",
        detail=report.reason,
    )


def _gaps_to_findings(gaps: Sequence[TestGap]) -> List[Finding]:
    out: List[Finding] = []
    for gap in gaps[:20]:
        out.append(Finding(
            severity=Severity.WARNING,
            category="tests",
            title=f"Missing test for `{gap.symbol}`",
            detail=f"No test file in this PR references `{gap.symbol}`. Consider adding a test.",
            file=gap.file,
            line=gap.line or None,
        ))
    if len(gaps) > 20:
        out.append(Finding(
            severity=Severity.NIT,
            category="tests",
            title=f"...and {len(gaps) - 20} more untested symbols",
        ))
    return out


def _related_to_finding(related: Sequence[RelatedPR]) -> Optional[Finding]:
    if not related:
        return None
    top = related[0]
    detail = f"#{top.number} overlaps on {top.overlap_count} file(s)"
    if len(related) > 1:
        detail += f" (+{len(related) - 1} more)"
    return Finding(
        severity=Severity.NIT,
        category="related",
        title="Related open PRs share files",
        detail=detail,
    )


def _secret_filenames_to_findings(risk: RiskScore) -> List[Finding]:
    """Promote risk.secret_files (filename-based detection) to BLOCKER findings.

    The diff-content scanner (_secrets_to_findings) catches hardcoded keys
    inside files, but it doesn't catch the case where the file itself is
    a secret artifact (.env, id_rsa, credentials.json). The risk scorer
    populates risk.secret_files via a structured field, and this helper
    turns each one into a BLOCKER. Earlier versions string-matched on
    risk.reasons; that fragile coupling was W13 in the review.
    """
    out: List[Finding] = []
    for filename in risk.secret_files:
        out.append(Finding(
            severity=Severity.BLOCKER,
            category="secret",
            title="Potential secret file in diff",
            detail=filename,
            file=filename,
        ))
    return out


def _breaking_to_findings(changes: Sequence[BreakingChange]) -> List[Finding]:
    out: List[Finding] = []
    for c in changes[:15]:
        out.append(Finding(
            severity=Severity.BLOCKER,
            category="breaking-change",
            title=f"Breaking change: `{c.name}` in {c.file}",
            detail=c.summary(),
            file=c.file,
        ))
    if len(changes) > 15:
        out.append(Finding(
            severity=Severity.WARNING,
            category="breaking-change",
            title=f"...and {len(changes) - 15} more breaking changes",
        ))
    return out


def _secrets_to_findings(hits: Sequence[SecretHit]) -> List[Finding]:
    out: List[Finding] = []
    for h in hits[:10]:
        out.append(Finding(
            severity=Severity.BLOCKER,
            category="secret",
            title=f"Potential {h.kind} in diff",
            detail=f"{h.file}:{h.line} -- `{h.redacted_preview()}`",
            file=h.file,
            line=h.line,
        ))
    if len(hits) > 10:
        out.append(Finding(
            severity=Severity.BLOCKER,
            category="secret",
            title=f"...and {len(hits) - 10} more potential secret leaks",
        ))
    return out


def _quality_to_findings(hits: Sequence[QualityHit]) -> List[Finding]:
    out: List[Finding] = []
    for h in hits[:25]:
        # TODO markers are INFO-level, debug leftovers are NIT.
        if h.kind.startswith("todo:"):
            sev = Severity.INFO
        else:
            sev = Severity.NIT
        out.append(Finding(
            severity=sev,
            category=h.kind.split(":", 1)[0],
            title=f"{h.kind} in {h.file}:{h.line}",
            detail=f"`{h.snippet}`",
            file=h.file,
            line=h.line,
        ))
    if len(hits) > 25:
        out.append(Finding(
            severity=Severity.INFO,
            category="quality",
            title=f"...and {len(hits) - 25} more quality hits",
        ))
    return out


def build_findings(
    risk: RiskScore,
    scope: ScopeReport,
    gaps: Sequence[TestGap],
    related: Sequence[RelatedPR],
    title_report: TitleReport,
    breaking: Sequence[BreakingChange] = (),
    secrets: Sequence[SecretHit] = (),
    quality: Sequence[QualityHit] = (),
) -> FindingSet:
    fs = FindingSet()
    # Secrets go first so they can never be buried.
    fs.extend(_secrets_to_findings(secrets))
    fs.extend(_secret_filenames_to_findings(risk))
    fs.extend(_breaking_to_findings(breaking))
    risk_f = _risk_to_finding(risk)
    # Suppress the generic risk finding when a secret-file BLOCKER already
    # carried the relevant signal — avoids duplicate noise.
    if risk_f and not risk.secret_files:
        fs.add(risk_f)
    scope_f = _scope_to_finding(scope)
    if scope_f:
        fs.add(scope_f)
    title_f = _title_to_finding(title_report)
    if title_f:
        fs.add(title_f)
    fs.extend(_gaps_to_findings(gaps))
    related_f = _related_to_finding(related)
    if related_f:
        fs.add(related_f)
    fs.extend(_quality_to_findings(quality))
    return fs


def analyze_pr(
    files: Sequence[PRFile],
    pr_title: str,
    diff_text: str,
    other_open_prs: Sequence[OtherPR],
    repo_dir: str,
    config: RepoConfig,
    run_blast_radius: bool = False,
    memory: Optional[ReviewMemory] = None,
) -> PRAnalysis:
    """Run all analysis modules and return a combined PRAnalysis.

    `run_blast_radius` defaults to False because `compute_blast_radius`
    shells out to ripgrep once per added symbol and can block the webhook
    handler for ~10 seconds per symbol on large PRs. Opt in per-repo via
    `.ch-code-reviewer.yml` when the caller context is worth the latency.
    """
    relevant_files = [f for f in files if not config.should_skip_file(f.filename)]

    risk = score_risk(relevant_files)
    scope = detect_scope_drift(pr_title, relevant_files)
    gaps = find_test_gaps(diff_text)
    related = find_related_prs([f.filename for f in relevant_files], other_open_prs)
    blast = compute_blast_radius(diff_text, repo_dir) if run_blast_radius else BlastRadius()
    title_report = check_title(pr_title)
    diff_summary = summarize_diff(relevant_files)
    breaking = detect_breaking_changes(diff_text)
    secrets = scan_diff(diff_text)
    quality = scan_quality(diff_text)

    findings = build_findings(risk, scope, gaps, related, title_report, breaking, secrets, quality)

    return PRAnalysis(
        risk=risk,
        scope=scope,
        gaps=gaps,
        related=related,
        blast=blast,
        config=config,
        findings=findings,
        diff_summary=diff_summary,
        title_report=title_report,
        memory=memory or ReviewMemory(),
        breaking=breaking,
        secrets=secrets,
        quality=quality,
    )
