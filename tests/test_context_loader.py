"""Tests for the blast-radius context loader.

The pure parsing/formatting logic is tested directly. The rg-backed caller
search is covered with a real temporary git-like directory where we write
known Python files and verify we find the correct call sites.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from context_loader import (  # noqa: E402
    BlastRadius,
    CallSite,
    SymbolContext,
    _parse_rg_line,
    compute_blast_radius,
    find_callers,
)


def test_parse_rg_line_strips_repo_prefix():
    site = _parse_rg_line("/tmp/repo/src/foo.py:12:    foo(x)", "/tmp/repo")
    assert site is not None
    assert site.file == "src/foo.py"
    assert site.line == 12
    assert "foo(x)" in site.preview


def test_parse_rg_line_invalid_returns_none():
    assert _parse_rg_line("garbage line", "/tmp/repo") is None


def test_blast_radius_summary_empty():
    br = BlastRadius()
    assert "no touched" in br.summary().lower()


def test_blast_radius_summary_with_symbols():
    br = BlastRadius(
        symbols=[
            SymbolContext(
                symbol="foo",
                defined_in="src/foo.py",
                callers=[CallSite("src/bar.py", 3, "foo()")],
            )
        ]
    )
    text = br.summary()
    assert "foo" in text
    assert "1 caller" in text
    assert "src/bar.py:3" in text


def test_blast_radius_as_prompt_section_empty():
    br = BlastRadius()
    assert br.as_prompt_section() == ""


def test_blast_radius_as_prompt_section_has_heading():
    br = BlastRadius(
        symbols=[
            SymbolContext(symbol="foo", defined_in="src/foo.py", callers=[])
        ]
    )
    text = br.as_prompt_section()
    assert "Blast Radius" in text
    assert "foo" in text


def test_find_callers_finds_real_call_sites():
    with tempfile.TemporaryDirectory() as d:
        # Definition file
        with open(os.path.join(d, "src_module.py"), "w") as fh:
            fh.write("def my_function(x):\n    return x + 1\n")
        os.makedirs(os.path.join(d, "sub"), exist_ok=True)
        with open(os.path.join(d, "sub", "caller1.py"), "w") as fh:
            fh.write("from src_module import my_function\n\n"
                     "result = my_function(5)\n")
        with open(os.path.join(d, "sub", "caller2.py"), "w") as fh:
            fh.write("def use_it():\n    return my_function(10)\n")

        callers = find_callers("my_function", "src_module.py", d)
        caller_files = [c.file for c in callers]
        assert any("caller1.py" in f for f in caller_files)
        assert any("caller2.py" in f for f in caller_files)
        # Definition file should be excluded
        assert not any(f == "src_module.py" for f in caller_files)


def test_find_callers_ignores_node_modules():
    with tempfile.TemporaryDirectory() as d:
        nm = os.path.join(d, "node_modules", "pkg")
        os.makedirs(nm, exist_ok=True)
        with open(os.path.join(nm, "index.js"), "w") as fh:
            fh.write("my_function(1);\n")
        with open(os.path.join(d, "real.js"), "w") as fh:
            fh.write("my_function(2);\n")

        callers = find_callers("my_function", "definition.js", d)
        assert not any("node_modules" in c.file for c in callers)
        assert any("real.js" in c.file for c in callers)


def test_find_callers_returns_empty_for_missing_dir():
    callers = find_callers("foo", "src/foo.py", "/tmp/nonexistent-repo-abc123")
    assert callers == []


def test_compute_blast_radius_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "module.py"), "w") as fh:
            fh.write("def my_func():\n    return 1\n")
        with open(os.path.join(d, "use.py"), "w") as fh:
            fh.write("from module import my_func\nmy_func()\n")

        diff = """diff --git a/module.py b/module.py
+++ b/module.py
@@ -0,0 +1,2 @@
+def my_func():
+    return 1
"""
        br = compute_blast_radius(diff, d)
        assert len(br.symbols) == 1
        assert br.symbols[0].symbol == "my_func"
        assert br.symbols[0].caller_count >= 1


def test_compute_blast_radius_ignores_test_files():
    with tempfile.TemporaryDirectory() as d:
        diff = """diff --git a/tests/test_foo.py b/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -0,0 +1,2 @@
+def test_my_thing():
+    pass
"""
        br = compute_blast_radius(diff, d)
        assert br.symbols == []
