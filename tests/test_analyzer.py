"""Tests for the analyzer coordinator."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyzer import analyze_pr, PRAnalysis  # noqa: E402
from related_prs import OtherPR  # noqa: E402
from repo_config import RepoConfig  # noqa: E402
from risk import PRFile  # noqa: E402


def f(name, adds=10, dels=5, status="modified"):
    return PRFile(filename=name, additions=adds, deletions=dels, status=status)


SMALL_DIFF = """diff --git a/src/foo.py b/src/foo.py
+++ b/src/foo.py
@@ -0,0 +1,2 @@
+def my_func():
+    return 1
"""


def test_analyze_low_risk_focused_pr():
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="fix: small fix",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    assert result.risk.level == "low"
    assert result.scope.drifted is False
    assert "risk:low" in result.labels()


def test_analyze_drifted_scope_adds_label():
    files = [f("a/x.py"), f("b/y.go"), f("c/z.ts"), f("d/w.py")]
    result = analyze_pr(
        files=files,
        pr_title="fix: one thing",
        diff_text="",
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    assert "scope:drifted" in result.labels()


def test_analyze_test_gap_adds_label():
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="feat: add my_func",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    assert "tests:missing" in result.labels()


def test_analyze_related_prs_surfaced():
    files = [f("src/foo.py")]
    others = [
        OtherPR(number=7, title="Other PR", files=frozenset({"src/foo.py"})),
    ]
    result = analyze_pr(
        files=files,
        pr_title="fix: foo",
        diff_text=SMALL_DIFF,
        other_open_prs=others,
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    assert len(result.related) == 1
    assert result.related[0].number == 7


def test_analyze_body_contains_all_sections():
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="feat: add my_func",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    body = result.body()
    assert "Pre-review analysis" in body
    assert "Risk:" in body
    assert "Scope:" in body
    assert "Test coverage" in body
    assert "Related PRs" in body
    assert "ch-code-reviewer" in body


def test_analyze_respects_ignore_paths():
    config = RepoConfig(ignore_paths=["docs/"])
    files = [f("docs/readme.md"), f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="fix: both",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=config,
        run_blast_radius=False,
    )
    # docs/readme.md should be filtered out, so scope only sees src
    assert result.scope.top_level_dirs == ["src"]


def test_prompt_addendum_includes_config_rules():
    config = RepoConfig(rules=["Use Realm for storage"])
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="fix: foo",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=config,
        run_blast_radius=False,
    )
    addendum = result.prompt_addendum()
    assert "Realm" in addendum


def test_prompt_addendum_notes_drift():
    files = [f("a/x.py"), f("b/y.py"), f("c/z.py")]
    result = analyze_pr(
        files=files,
        pr_title="fix: one",
        diff_text="",
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    addendum = result.prompt_addendum()
    assert "drift" in addendum.lower() or "scope" in addendum.lower()


def test_empty_analysis_labels_only_risk():
    result = PRAnalysis(
        risk=__import__("risk").score_risk([f("README.md", 1, 0)]),
        scope=__import__("scope").detect_scope_drift("docs", [f("README.md", 1, 0)]),
    )
    assert result.labels() == ["risk:low"]


def test_findings_produced_for_medium_risk():
    files = [f(f"src/file{i}.py", 60, 20) for i in range(6)]
    result = analyze_pr(
        files=files,
        pr_title="fix: something",
        diff_text="",
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    # Medium risk from size alone
    assert any(find.category == "risk" for find in result.findings.findings)


def test_findings_blocker_for_secret_file():
    files = [f(".env", 2, 0, status="added")]
    result = analyze_pr(
        files=files,
        pr_title="feat: add env config",
        diff_text="",
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    assert result.findings.has_blockers()
    assert "review:blocker" in result.labels()


def test_title_finding_warning_for_vague():
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="wip",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    assert any(find.category == "title" for find in result.findings.findings)


def test_body_includes_diff_summary_and_headline():
    files = [f("src/foo.py", 20, 10)]
    result = analyze_pr(
        files=files,
        pr_title="feat: add my_func",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    body = result.body()
    assert "Summary" in body
    assert "Headline" in body
    assert "Touches" in body


def test_prompt_addendum_includes_memory():
    from review_memory import ReviewMemory
    memory = ReviewMemory(rules=["Always use context managers for file I/O"])
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="fix: something small",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
        memory=memory,
    )
    addendum = result.prompt_addendum()
    assert "context managers" in addendum


def test_inline_comments_built_from_test_gaps():
    diff_with_lines = """diff --git a/src/foo.py b/src/foo.py
+++ b/src/foo.py
@@ -1,2 +1,4 @@
 existing
 existing2
+def new_thing(x):
+    return x * 2
"""
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="feat: add new_thing",
        diff_text=diff_with_lines,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    comments = result.inline_comments()
    assert len(comments) >= 1
    # Should have one for the new_thing symbol
    test_gap_comments = [c for c in comments if "new_thing" in c["body"]]
    assert len(test_gap_comments) == 1
    assert test_gap_comments[0]["path"] == "src/foo.py"
    assert test_gap_comments[0]["line"] == 3  # "def new_thing" is at line 3
    assert test_gap_comments[0]["side"] == "RIGHT"


def test_inline_comments_empty_when_no_line_info():
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="fix: something",
        diff_text="",  # no diff → no gaps → no inline comments
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    assert result.inline_comments() == []


# --------------------------------------------------------------------------
# StandardsConfig wiring (P3)
# --------------------------------------------------------------------------


def test_standards_default_off_produces_no_new_findings():
    """With StandardsConfig at all defaults, none of the new categories appear."""
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="feat: add thing",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),  # standards defaults to off
        run_blast_radius=False,
        head_ref="some-branch",
    )
    categories = {find.category for find in result.findings.findings}
    assert "license" not in categories
    assert "commit-convention" not in categories
    assert "branch-name" not in categories


def test_license_finding_emitted_for_missing_header():
    from repo_config import StandardsConfig
    config = RepoConfig(standards=StandardsConfig(
        license_header="// Copyright {YEAR} Acme Corp.",
    ))
    diff = (
        "diff --git a/src/new.go b/src/new.go\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/new.go\n"
        "@@ -0,0 +1,2 @@\n"
        "+package new\n"
        "+// no header\n"
    )
    files = [f("src/new.go", status="added")]
    result = analyze_pr(
        files=files,
        pr_title="feat: add new.go",
        diff_text=diff,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=config,
        run_blast_radius=False,
    )
    license_findings = [x for x in result.findings.findings if x.category == "license"]
    assert len(license_findings) == 1
    assert license_findings[0].file == "src/new.go"


def test_commit_convention_strict_mode_emits_warning():
    from repo_config import StandardsConfig
    config = RepoConfig(standards=StandardsConfig(
        commit_convention_strict=True,
    ))
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="update stuff",  # non-conventional
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=config,
        run_blast_radius=False,
    )
    convention_findings = [x for x in result.findings.findings if x.category == "commit-convention"]
    assert len(convention_findings) == 1


def test_commit_convention_strict_suppresses_soft_title_finding():
    """When strict mode fires, the soft title_check nudge must NOT double-up."""
    from repo_config import StandardsConfig
    config = RepoConfig(standards=StandardsConfig(
        commit_convention_strict=True,
    ))
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="wip",  # both title_check (vague) and strict mode would flag
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=config,
        run_blast_radius=False,
    )
    title_findings = [x for x in result.findings.findings if x.category == "title"]
    convention_findings = [x for x in result.findings.findings if x.category == "commit-convention"]
    assert len(convention_findings) == 1
    assert len(title_findings) == 0  # soft title suppressed


def test_strict_mode_off_preserves_soft_title_finding():
    """When strict mode is OFF, title_check's soft nudge still fires."""
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="wip",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
    )
    title_findings = [x for x in result.findings.findings if x.category == "title"]
    assert len(title_findings) == 1


def test_branch_name_nit_emitted_on_non_matching_ref():
    from repo_config import StandardsConfig
    config = RepoConfig(standards=StandardsConfig(
        branch_name_patterns=[r"^feat/.*", r"^fix/.*"],
    ))
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="fix: something",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=config,
        run_blast_radius=False,
        head_ref="my-weird-branch",
    )
    branch_findings = [x for x in result.findings.findings if x.category == "branch-name"]
    assert len(branch_findings) == 1
    from findings import Severity
    assert branch_findings[0].severity == Severity.NIT


def test_branch_name_matching_ref_no_finding():
    from repo_config import StandardsConfig
    config = RepoConfig(standards=StandardsConfig(
        branch_name_patterns=[r"^feat/.*", r"^fix/.*"],
    ))
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="feat: thing",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=config,
        run_blast_radius=False,
        head_ref="feat/new-thing",
    )
    branch_findings = [x for x in result.findings.findings if x.category == "branch-name"]
    assert len(branch_findings) == 0


def test_analyze_pr_head_ref_kwarg_optional():
    """Callers that don't pass head_ref still work (backward compat)."""
    files = [f("src/foo.py")]
    result = analyze_pr(
        files=files,
        pr_title="fix: something",
        diff_text=SMALL_DIFF,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=RepoConfig(),
        run_blast_radius=False,
        # no head_ref
    )
    # Should not crash, should not emit branch-name finding
    branch_findings = [x for x in result.findings.findings if x.category == "branch-name"]
    assert len(branch_findings) == 0


def test_build_findings_standards_is_required():
    """Fix J: build_findings should not fall back to a default StandardsConfig.

    Callers always pass `config.standards`; the Optional[] sentinel was
    dead code.
    """
    import inspect
    from analyzer import build_findings
    sig = inspect.signature(build_findings)
    # standards parameter should have no default (or a non-None default
    # representing "required caller input"). The point is removing the
    # silent `None -> StandardsConfig()` fallback.
    std_param = sig.parameters["standards"]
    assert std_param.annotation is not type(None)
    # Cannot be Optional[StandardsConfig] — the whole point of fix J.
    import typing
    ann_str = str(std_param.annotation)
    assert "Optional" not in ann_str, (
        f"build_findings(standards=...) should no longer accept None; "
        f"annotation={ann_str!r}"
    )


def test_resolve_severity_logs_fallback_on_invalid_label(capsys):
    """Fix O: invalid severity label should log to stderr before falling back."""
    from analyzer import _resolve_severity
    from findings import Severity
    result = _resolve_severity("definitely-not-a-severity", Severity.WARNING)
    assert result == Severity.WARNING
    captured = capsys.readouterr()
    assert "[seneschal]" in captured.err
    assert "definitely-not-a-severity" in captured.err


def test_resolve_severity_no_log_on_valid_label(capsys):
    from analyzer import _resolve_severity
    from findings import Severity
    result = _resolve_severity("blocker", Severity.WARNING)
    assert result == Severity.BLOCKER
    captured = capsys.readouterr()
    assert captured.err == ""


def test_resolve_severity_no_log_on_none_override(capsys):
    from analyzer import _resolve_severity
    from findings import Severity
    result = _resolve_severity(None, Severity.NIT)
    assert result == Severity.NIT
    captured = capsys.readouterr()
    assert captured.err == ""


def test_severity_override_license_upgrade_to_blocker():
    from repo_config import StandardsConfig
    from findings import Severity
    config = RepoConfig(standards=StandardsConfig(
        license_header="// REQUIRED HEADER",
        license_severity="blocker",
    ))
    diff = (
        "diff --git a/src/new.go b/src/new.go\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/new.go\n"
        "@@ -0,0 +1,1 @@\n"
        "+package new\n"
    )
    files = [f("src/new.go", status="added")]
    result = analyze_pr(
        files=files,
        pr_title="feat: new.go",
        diff_text=diff,
        other_open_prs=[],
        repo_dir="/nonexistent",
        config=config,
        run_blast_radius=False,
    )
    license_findings = [x for x in result.findings.findings if x.category == "license"]
    assert len(license_findings) == 1
    assert license_findings[0].severity == Severity.BLOCKER
