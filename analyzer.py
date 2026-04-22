"""PR analyzer — coordinates all analysis modules into a single structured report.

Pure composition: takes inputs (PR metadata, files, diff, other PRs, repo dir)
and produces a `PRAnalysis` containing labels, a formatted markdown body,
and an optional system-prompt addendum for the downstream Claude review.

I/O (fetching PRs, posting comments) lives in `app.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from branch_naming import BranchNameViolation, check_branch_name
from breaking_changes import BreakingChange, detect_breaking_changes, summarize_breaking
from ci_context import CIResult, correlate_failing_checks, render_ci_addendum
from commit_convention import ConventionViolation, check_pr_title as check_pr_title_strict
from context_loader import BlastRadius, compute_blast_radius
from findings import Finding, FindingSet, Severity
from history_context import ADR, render_adrs_addendum
from license_check import LicenseViolation, scan_license_headers
from quality_scan import QualityHit, scan_quality, summarize_quality
from related_prs import OtherPR, RelatedPR, find_related_prs, summarize_related
from repo_config import RepoConfig, StandardsConfig
from review_memory import ReviewMemory
from risk import PRFile, RiskScore, score_risk
from scope import ScopeReport, detect_scope_drift
from secrets_scan import SecretHit, scan_diff, summarize_secrets
from summary import summarize_diff
from test_gaps import TestGap, find_test_gaps, has_test_framework, summarize_gaps
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
    # Relevant ADRs / decision-log entries selected by history_context.
    # Empty list = no ADR discovery happened or nothing scored relevant.
    relevant_adrs: List[ADR] = field(default_factory=list)
    # CI check-run status for the PR head SHA. Default is an empty
    # non-fetched CIResult — ignored by prompt_addendum unless fetched.
    ci: CIResult = field(default_factory=CIResult)
    # Failing checks that correlated with touched files (pre-computed in
    # analyze_pr while it still has the list of touched filenames).
    _ci_correlated: List = field(default_factory=list)

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
        if self.relevant_adrs:
            parts.append(render_adrs_addendum(self.relevant_adrs))
        if self.ci.fetched and self.ci.total > 0:
            touched = [
                # Pull touched filenames from risk scoring — set during analyze_pr
                # and stored on self.diff_summary isn't structured for this; instead
                # we rely on the render function's own inputs. Since correlation
                # needs touched files but we don't hold them on PRAnalysis, render
                # the CI block with only the correlation we already computed.
            ]
            parts.append(render_ci_addendum(self.ci, self._ci_correlated))
        return "\n".join(p for p in parts if p)


def _risk_to_finding(risk: RiskScore) -> Optional[Finding]:
    # BLOCKER severity is reserved for risk signals that are truly
    # actionable — right now, a detected secret file. Size/scope-based
    # HIGH produces a WARNING so it doesn't contradict Claude's
    # ultimately-APPROVE verdict. The separate secret-scan module also
    # files its own BLOCKER findings; we mirror that here for consistency
    # with risk-level secret detection.
    if risk.secret_files:
        return Finding(
            severity=Severity.BLOCKER,
            category="risk",
            title=f"Potential secret file(s) in diff (risk score {risk.score})",
            detail="; ".join(risk.reasons) if risk.reasons else "",
        )
    if risk.level == "high":
        return Finding(
            severity=Severity.WARNING,
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


# Severity default map for P3 standards findings. Overrides from
# `StandardsConfig.*_severity` swap these out per-category at runtime.
_SEVERITY_LABEL_MAP = {
    "blocker": Severity.BLOCKER,
    "warning": Severity.WARNING,
    "nit": Severity.NIT,
    "info": Severity.INFO,
}


def _resolve_severity(override: Optional[str], default: Severity) -> Severity:
    """Translate an optional `.seneschal.yml` severity label to a Severity.

    None / unrecognized value → use the default.
    """
    if override is None:
        return default
    return _SEVERITY_LABEL_MAP.get(override, default)


def _license_to_findings(
    violations: Sequence[LicenseViolation],
    severity: Severity = Severity.WARNING,
) -> List[Finding]:
    out: List[Finding] = []
    for v in violations[:25]:
        out.append(Finding(
            severity=severity,
            category="license",
            title="Missing license header",
            detail=v.reason,
            file=v.file,
        ))
    if len(violations) > 25:
        out.append(Finding(
            severity=severity,
            category="license",
            title=f"...and {len(violations) - 25} more license violations",
        ))
    return out


def _convention_to_finding(
    violation: Optional[ConventionViolation],
    severity: Severity = Severity.WARNING,
) -> Optional[Finding]:
    if violation is None:
        return None
    return Finding(
        severity=severity,
        category="commit-convention",
        title="PR title does not follow conventional-commit convention",
        detail=violation.reason,
    )


def _branch_name_to_finding(
    violation: Optional[BranchNameViolation],
    severity: Severity = Severity.NIT,
) -> Optional[Finding]:
    if violation is None:
        return None
    return Finding(
        severity=severity,
        category="branch-name",
        title="Branch name does not match repo convention",
        detail=violation.reason,
    )


def build_findings(
    risk: RiskScore,
    scope: ScopeReport,
    gaps: Sequence[TestGap],
    related: Sequence[RelatedPR],
    title_report: TitleReport,
    breaking: Sequence[BreakingChange] = (),
    secrets: Sequence[SecretHit] = (),
    quality: Sequence[QualityHit] = (),
    license_violations: Sequence[LicenseViolation] = (),
    convention_violation: Optional[ConventionViolation] = None,
    branch_violation: Optional[BranchNameViolation] = None,
    suppress_soft_title: bool = False,
    standards: Optional[StandardsConfig] = None,
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
    # Soft title finding is suppressed when the strict commit-convention
    # finding is about to fire — prevents double-reporting the same issue.
    if not suppress_soft_title:
        title_f = _title_to_finding(title_report)
        if title_f:
            fs.add(title_f)
    fs.extend(_gaps_to_findings(gaps))
    related_f = _related_to_finding(related)
    if related_f:
        fs.add(related_f)
    fs.extend(_quality_to_findings(quality))

    # Standards enforcement (P3). Each block is independent and gated on
    # its own presence check — absent standards config = no new findings.
    std = standards or StandardsConfig()
    license_sev = _resolve_severity(std.license_severity, Severity.WARNING)
    fs.extend(_license_to_findings(license_violations, severity=license_sev))

    convention_sev = _resolve_severity(std.commit_convention_severity, Severity.WARNING)
    convention_f = _convention_to_finding(convention_violation, severity=convention_sev)
    if convention_f:
        fs.add(convention_f)

    branch_sev = _resolve_severity(std.branch_name_severity, Severity.NIT)
    branch_f = _branch_name_to_finding(branch_violation, severity=branch_sev)
    if branch_f:
        fs.add(branch_f)

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
    adrs: Optional[Sequence[ADR]] = None,
    ci: Optional[CIResult] = None,
    head_ref: Optional[str] = None,
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
    # Test-gap detection only runs if the repo has a detectable test
    # framework. Flagging "missing test for X" on a repo with zero test
    # infrastructure is noise — the action is "set up a test framework",
    # not "add a test for this specific function".
    gaps = find_test_gaps(diff_text) if has_test_framework(repo_dir) else []
    related = find_related_prs([f.filename for f in relevant_files], other_open_prs)
    blast = compute_blast_radius(diff_text, repo_dir) if run_blast_radius else BlastRadius()
    title_report = check_title(pr_title)
    diff_summary = summarize_diff(relevant_files)
    breaking = detect_breaking_changes(diff_text)
    secrets = scan_diff(diff_text)
    quality = scan_quality(diff_text)

    # Standards enforcement (P3) — all three producers no-op when the
    # corresponding config knob is empty/False.
    # NOTE: pass the raw `files` (pre-ignore_paths filter) so license
    # enforcement applies repo-wide, not just to non-ignored paths.
    # The `license_applies_to` / `license_exemptions` knobs are the
    # right control surface for license scoping, not `ignore_paths`.
    license_violations = scan_license_headers(
        diff_text,
        pr_files=files,
        config=config.standards,
    )
    convention_violation = check_pr_title_strict(
        pr_title,
        strict=config.standards.commit_convention_strict,
    )
    branch_violation = check_branch_name(
        head_ref,
        config.standards.branch_name_patterns,
    )
    # When strict-convention fires, suppress the soft title finding so we
    # don't double-report the same underlying issue.
    suppress_soft_title = (
        config.standards.commit_convention_strict and convention_violation is not None
    )

    findings = build_findings(
        risk,
        scope,
        gaps,
        related,
        title_report,
        breaking,
        secrets,
        quality,
        license_violations=license_violations,
        convention_violation=convention_violation,
        branch_violation=branch_violation,
        suppress_soft_title=suppress_soft_title,
        standards=config.standards,
    )

    # ADR relevance scoring — only runs if caller supplied ADRs.
    # Done here so the scoring heuristic has access to touched filenames.
    relevant_adrs_list: List[ADR] = []
    touched = [f.filename for f in relevant_files]
    if adrs:
        from history_context import relevant_adrs as _score_relevant_adrs
        relevant_adrs_list = list(_score_relevant_adrs(adrs, touched, diff_text))

    # CI correlation — needs touched filenames which aren't on PRAnalysis.
    ci_result = ci or CIResult()
    ci_correlated = (
        list(correlate_failing_checks(ci_result, touched))
        if ci_result.fetched and ci_result.has_failures
        else []
    )

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
        relevant_adrs=relevant_adrs_list,
        ci=ci_result,
        _ci_correlated=ci_correlated,
    )
