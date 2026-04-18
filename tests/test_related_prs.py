"""Tests for related-PR finder."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from related_prs import OtherPR, find_related_prs, summarize_related  # noqa: E402


def other(number, title, files):
    return OtherPR(number=number, title=title, files=frozenset(files))


def test_no_others_returns_empty():
    related = find_related_prs(["a.py", "b.py"], [])
    assert related == []


def test_no_overlap_returns_empty():
    others = [other(10, "Unrelated", ["x.py", "y.py"])]
    related = find_related_prs(["a.py", "b.py"], others)
    assert related == []


def test_single_overlap_returned():
    others = [other(10, "Touches a", ["a.py", "z.py"])]
    related = find_related_prs(["a.py", "b.py"], others)
    assert len(related) == 1
    assert related[0].number == 10
    assert related[0].overlapping_files == ["a.py"]


def test_sorted_by_overlap_count_desc():
    current = ["a.py", "b.py", "c.py"]
    others = [
        other(10, "One overlap", ["a.py", "x.py"]),
        other(11, "Two overlaps", ["a.py", "b.py"]),
        other(12, "Three overlaps", ["a.py", "b.py", "c.py"]),
    ]
    related = find_related_prs(current, others)
    assert [r.number for r in related] == [12, 11, 10]


def test_ties_broken_by_pr_number():
    current = ["a.py"]
    others = [
        other(20, "later", ["a.py"]),
        other(10, "earlier", ["a.py"]),
        other(15, "middle", ["a.py"]),
    ]
    related = find_related_prs(current, others)
    assert [r.number for r in related] == [10, 15, 20]


def test_max_results_cap():
    current = ["a.py"]
    others = [other(i, f"PR {i}", ["a.py"]) for i in range(20)]
    related = find_related_prs(current, others, max_results=3)
    assert len(related) == 3


def test_empty_current_returns_empty():
    others = [other(10, "x", ["a.py"])]
    related = find_related_prs([], others)
    assert related == []


def test_summarize_empty():
    out = summarize_related([])
    assert "none" in out.lower()


def test_summarize_with_related():
    others = [other(10, "Fix thing", ["a.py", "b.py"])]
    related = find_related_prs(["a.py", "b.py"], others)
    out = summarize_related(related)
    assert "#10" in out
    assert "Fix thing" in out
    assert "a.py" in out


def test_summarize_truncates_many_files():
    others = [
        other(10, "Big PR", ["a.py", "b.py", "c.py", "d.py", "e.py"]),
    ]
    related = find_related_prs(["a.py", "b.py", "c.py", "d.py", "e.py"], others)
    out = summarize_related(related)
    assert "more" in out
