"""Tests for history_context: ADR discovery + relevance scoring."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from history_context import (  # noqa: E402
    ADR,
    MAX_ADR_BODY_LEN,
    MAX_ADRS_IN_PROMPT,
    find_adrs,
    relevant_adrs,
    render_adrs_addendum,
)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content)


# --------------------------------------------------------------------------
# find_adrs — discovery
# --------------------------------------------------------------------------


def test_find_adrs_discovers_docs_adr_convention():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "docs/adr/0001-use-postgres.md"), "# Use Postgres\n\nAccepted")
        _write(os.path.join(d, "docs/adr/0002-drop-mongo.md"), "# Drop Mongo\n\nAccepted")
        adrs = find_adrs(d)
        assert len(adrs) == 2
        assert {a.title for a in adrs} == {"Use Postgres", "Drop Mongo"}


def test_find_adrs_discovers_docs_decisions_convention():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "docs/decisions/0003-use-graphql.md"), "# Use GraphQL\n")
        adrs = find_adrs(d)
        assert len(adrs) == 1
        assert adrs[0].title == "Use GraphQL"


def test_find_adrs_discovers_top_level_adr_dir():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "adr/0001-pick-go.md"), "# Pick Go\n")
        adrs = find_adrs(d)
        assert len(adrs) == 1


def test_find_adrs_discovers_single_file_conventions():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "ADR.md"), "# Team decisions\n")
        _write(os.path.join(d, "DECISIONS.md"), "# Our calls\n")
        adrs = find_adrs(d)
        titles = {a.title for a in adrs}
        assert "Team decisions" in titles
        assert "Our calls" in titles


def test_find_adrs_recognizes_filename_patterns():
    with tempfile.TemporaryDirectory() as d:
        # All three accepted patterns in one dir
        _write(os.path.join(d, "docs/adr/adr-use-postgres.md"), "# Use Postgres\n")
        _write(os.path.join(d, "docs/adr/0001-use-redis.md"), "# Use Redis\n")
        _write(os.path.join(d, "docs/adr/feature.adr.md"), "# Feature ADR\n")
        # Not an ADR — should be ignored
        _write(os.path.join(d, "docs/adr/README.md"), "# Readme\n")
        adrs = find_adrs(d)
        titles = {a.title for a in adrs}
        assert "Use Postgres" in titles
        assert "Use Redis" in titles
        assert "Feature ADR" in titles
        assert "Readme" not in titles


def test_find_adrs_returns_empty_when_no_adrs():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "README.md"), "# Project")
        _write(os.path.join(d, "src/main.py"), "print('hi')")
        assert find_adrs(d) == []


def test_find_adrs_bad_repo_root_returns_empty():
    assert find_adrs("/does/not/exist") == []


# --------------------------------------------------------------------------
# ADR parsing
# --------------------------------------------------------------------------


def test_parsed_adr_extracts_title_and_body():
    with tempfile.TemporaryDirectory() as d:
        _write(
            os.path.join(d, "docs/adr/0001-cache.md"),
            "# Use Redis for caching\n\n"
            "We need a cache layer for expensive queries.\n"
            "Decision: use Redis because the team already operates it.",
        )
        adrs = find_adrs(d)
        assert len(adrs) == 1
        a = adrs[0]
        assert a.title == "Use Redis for caching"
        assert "cache layer" in a.body
        assert a.id.startswith("0001")


def test_parsed_adr_extracts_status_from_yaml_field():
    with tempfile.TemporaryDirectory() as d:
        _write(
            os.path.join(d, "docs/adr/0001-x.md"),
            "---\nstatus: accepted\n---\n\n# Title here\n\nBody.",
        )
        adrs = find_adrs(d)
        assert adrs[0].status == "accepted"


def test_parsed_adr_extracts_status_from_heading():
    with tempfile.TemporaryDirectory() as d:
        _write(
            os.path.join(d, "docs/adr/0001-x.md"),
            "# Title here\n\n## Status\n\nSuperseded\n\n## Context\n\nblah",
        )
        adrs = find_adrs(d)
        assert adrs[0].status == "superseded"


# --------------------------------------------------------------------------
# relevant_adrs — scoring
# --------------------------------------------------------------------------


def _fake_adr(title="Use Postgres", body="Decided to use Postgres over MongoDB.", status="accepted", path="docs/adr/0001-postgres.md"):
    return ADR(id="0001-postgres", title=title, status=status, body=body, path=path)


def test_relevant_adrs_returns_empty_for_no_adrs():
    assert relevant_adrs([], ["foo.py"], "diff") == []


def test_relevant_adrs_filters_irrelevant():
    adr = _fake_adr(title="Use Postgres", body="Postgres handles our query workload.")
    # Diff is completely unrelated
    result = relevant_adrs([adr], ["frontend/Button.tsx"], "export const Button = () => <div />")
    # Score should be 0 → filtered out
    assert result == []


def test_relevant_adrs_matches_on_token_overlap():
    adr = _fake_adr(
        title="Use Postgres for transactional workloads",
        body="Postgres handles our query workload better than MongoDB.",
    )
    diff_text = "def run_query():\n    db.postgres.query('SELECT ...')"
    result = relevant_adrs([adr], ["src/db/query.py"], diff_text)
    assert len(result) == 1
    assert result[0].title == "Use Postgres for transactional workloads"


def test_relevant_adrs_bonuses_filename_matching():
    # ADR filename "cache.md" shares token with diff filename "cache_layer.py"
    adr = ADR(id="cache", title="Irrelevant title here", status="", body="unrelated body", path="docs/adr/cache.md")
    # Without filename bonus this would score zero; the filename match should bring it in.
    result = relevant_adrs([adr], ["services/cache_layer.py"], "def foo(): pass")
    assert len(result) == 1


def test_relevant_adrs_caps_at_limit():
    adrs = [
        _fake_adr(title=f"Postgres decision {i}", body="Postgres query workload")
        for i in range(10)
    ]
    diff = "postgres query"
    result = relevant_adrs(adrs, ["x.py"], diff, limit=3)
    assert len(result) == 3


def test_relevant_adrs_ranks_accepted_over_proposed():
    a1 = _fake_adr(title="Postgres now", body="Postgres query", status="accepted", path="0001-a.md")
    a2 = _fake_adr(title="Postgres now", body="Postgres query", status="proposed", path="0002-b.md")
    diff = "postgres query"
    result = relevant_adrs([a1, a2], ["x.py"], diff)
    # a1 (accepted) should outrank a2 (proposed)
    assert result[0].path == "0001-a.md"


# --------------------------------------------------------------------------
# render_adrs_addendum
# --------------------------------------------------------------------------


def test_render_empty_returns_empty_string():
    assert render_adrs_addendum([]) == ""


def test_render_includes_title_and_path():
    a = _fake_adr(
        title="Use Postgres",
        body="We decided to use Postgres because...",
        path="docs/adr/0001-postgres.md",
    )
    out = render_adrs_addendum([a])
    assert "Use Postgres" in out
    assert "docs/adr/0001-postgres.md" in out
    assert "accepted" in out.lower()


def test_render_truncates_long_body():
    a = _fake_adr(body="X" * (MAX_ADR_BODY_LEN * 3))
    out = render_adrs_addendum([a])
    # Body excerpt must not exceed the cap (we account for some
    # rendering overhead but the payload itself is bounded)
    assert "X" * (MAX_ADR_BODY_LEN + 1) not in out


def test_render_omits_status_line_when_missing():
    a = _fake_adr(status="")
    out = render_adrs_addendum([a])
    assert "status:" not in out.lower()
