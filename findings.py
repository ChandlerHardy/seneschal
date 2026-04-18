"""Severity-tagged findings for PR reviews.

Every analysis module produces raw data (RiskScore, ScopeReport, etc.).
The analyzer translates those into a unified `Finding` list sorted by
severity so the most important issues surface first in the review body
and can be posted as inline line-level comments when applicable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional, Sequence


class Severity(IntEnum):
    """Severity levels. Lower value = more important. Sorts blockers first."""
    BLOCKER = 0
    WARNING = 1
    NIT = 2
    INFO = 3

    @property
    def label(self) -> str:
        return {
            Severity.BLOCKER: "BLOCKER",
            Severity.WARNING: "WARNING",
            Severity.NIT: "NIT",
            Severity.INFO: "INFO",
        }[self]


@dataclass
class Finding:
    severity: Severity
    category: str  # "risk", "scope", "tests", "related", "blast", "title", "secret"
    title: str
    detail: str = ""
    file: Optional[str] = None  # for inline comments
    line: Optional[int] = None

    def render(self) -> str:
        head = f"- **{self.severity.label}** [{self.category}] {self.title}"
        if self.detail:
            return f"{head}\n    {self.detail}"
        return head


@dataclass
class FindingSet:
    findings: List[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def extend(self, others: Sequence[Finding]) -> None:
        self.findings.extend(others)

    def sorted(self) -> List[Finding]:
        return sorted(self.findings, key=lambda f: (int(f.severity), f.category, f.title))

    def by_severity(self) -> dict:
        out: dict = {}
        for f in self.sorted():
            out.setdefault(f.severity, []).append(f)
        return out

    def _severity_counts(self) -> dict:
        counts = {Severity.BLOCKER: 0, Severity.WARNING: 0, Severity.NIT: 0, Severity.INFO: 0}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts

    @property
    def blocker_count(self) -> int:
        return self._severity_counts()[Severity.BLOCKER]

    @property
    def warning_count(self) -> int:
        return self._severity_counts()[Severity.WARNING]

    @property
    def nit_count(self) -> int:
        return self._severity_counts()[Severity.NIT]

    def render_grouped(self) -> str:
        if not self.findings:
            return "_(no automated findings)_"
        parts: List[str] = []
        grouped = self.by_severity()
        for severity in (Severity.BLOCKER, Severity.WARNING, Severity.NIT, Severity.INFO):
            if severity not in grouped:
                continue
            items = grouped[severity]
            parts.append(f"### {severity.label} ({len(items)})")
            parts.append("")
            for f in items:
                parts.append(f.render())
            parts.append("")
        return "\n".join(parts).rstrip()

    def headline(self) -> str:
        counts = self._severity_counts()
        blockers = counts[Severity.BLOCKER]
        warnings = counts[Severity.WARNING]
        nits = counts[Severity.NIT]
        if blockers:
            return f"{blockers} blocker(s), {warnings} warning(s), {nits} nit(s)"
        if warnings:
            return f"{warnings} warning(s), {nits} nit(s)"
        if nits:
            return f"{nits} nit(s)"
        return "clean"

    def has_blockers(self) -> bool:
        return self.blocker_count > 0
