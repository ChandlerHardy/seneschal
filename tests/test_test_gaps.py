"""Tests for test-gap detector and unified diff parser."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_gaps import (  # noqa: E402
    collect_referenced_identifiers,
    extract_added_symbols,
    find_test_gaps,
    is_test_file,
    parse_unified_diff,
    parse_unified_diff_with_lines,
    summarize_gaps,
)


# ----- Diff parser -----

def test_parse_diff_single_file():
    diff = """diff --git a/src/foo.py b/src/foo.py
index abc..def 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,6 @@
 existing line
+def new_function():
+    return 1
 another existing
"""
    result = parse_unified_diff(diff)
    assert "src/foo.py" in result
    assert result["src/foo.py"] == ["def new_function():", "    return 1"]


def test_parse_diff_multiple_files():
    diff = """diff --git a/a.py b/a.py
+++ b/a.py
@@ -0,0 +1,2 @@
+def a():
+    pass
diff --git a/b.go b/b.go
+++ b/b.go
@@ -0,0 +1,1 @@
+func B() {}
"""
    result = parse_unified_diff(diff)
    assert len(result) == 2
    assert result["a.py"] == ["def a():", "    pass"]
    assert result["b.go"] == ["func B() {}"]


def test_parse_diff_ignores_deletions_and_context():
    diff = """diff --git a/x.py b/x.py
+++ b/x.py
@@ -1,5 +1,4 @@
 context
-def removed():
-    pass
+def added():
+    pass
"""
    result = parse_unified_diff(diff)
    assert result["x.py"] == ["def added():", "    pass"]


# ----- Symbol extraction -----

def test_extract_python_def_and_class():
    lines = [
        "def foo(x, y):",
        "    return x",
        "class Bar:",
        "    pass",
    ]
    syms = extract_added_symbols("src/module.py", lines)
    assert "foo" in syms
    assert "Bar" in syms


def test_extract_python_ignores_private():
    lines = ["def _helper(x):", "class _Internal:"]
    syms = extract_added_symbols("src/module.py", lines)
    assert syms == []


def test_extract_python_async():
    lines = ["async def fetch_user(uid):"]
    syms = extract_added_symbols("src/module.py", lines)
    assert "fetch_user" in syms


def test_extract_go_func_exported_only():
    lines = [
        "func privateThing() {",
        "func PublicThing(x int) error {",
        "func (r *Repo) Find(id string) *User {",
    ]
    syms = extract_added_symbols("internal/user.go", lines)
    assert "PublicThing" in syms
    assert "Find" in syms
    assert "privateThing" not in syms


def test_extract_go_type():
    lines = ["type User struct {", "type Handler interface {"]
    syms = extract_added_symbols("internal/types.go", lines)
    assert "User" in syms
    assert "Handler" in syms


def test_extract_js_function_and_const_arrow():
    lines = [
        "export function myFunc(a, b) {",
        "export const handler = (req) => {",
        "const _private = () => {",
        "class MyClass {",
    ]
    syms = extract_added_symbols("src/api.ts", lines)
    assert "myFunc" in syms
    assert "handler" in syms
    assert "MyClass" in syms
    assert "_private" not in syms


def test_extract_unknown_extension_returns_empty():
    syms = extract_added_symbols("README.md", ["# heading"])
    assert syms == []


def test_extract_swift_func_and_types():
    lines = [
        "public func fetchUser(id: String) -> User {",
        "class GardenStore {",
        "struct Plant {",
        "protocol Identifiable {",
        "enum PlantStage {",
        "private func internalHelper() {",
    ]
    syms = extract_added_symbols("ios/Store.swift", lines)
    assert "fetchUser" in syms
    assert "GardenStore" in syms
    assert "Plant" in syms
    assert "Identifiable" in syms
    assert "PlantStage" in syms
    assert "internalHelper" not in syms


def test_extract_php_function_and_class():
    lines = [
        "public function findUser($id) {",
        "class UserRepository {",
        "private function internalHelper() {",
        "abstract class BaseRepository {",
    ]
    syms = extract_added_symbols("src/Repository.php", lines)
    assert "findUser" in syms
    assert "UserRepository" in syms
    assert "BaseRepository" in syms
    assert "internalHelper" not in syms


def test_extract_vue_script_block():
    lines = [
        "export function useGarden() {",
        "export const plantThing = () => {",
    ]
    syms = extract_added_symbols("components/Garden.vue", lines)
    assert "useGarden" in syms
    assert "plantThing" in syms


# ----- Line-tracked diff parsing -----

def test_parse_diff_with_lines_single_hunk():
    diff = """diff --git a/src/foo.py b/src/foo.py
+++ b/src/foo.py
@@ -10,3 +10,5 @@
 context line 10
+new line at 11
+new line at 12
 context line 13
"""
    result = parse_unified_diff_with_lines(diff)
    assert "src/foo.py" in result
    pairs = result["src/foo.py"]
    assert len(pairs) == 2
    assert pairs[0] == (11, "new line at 11")
    assert pairs[1] == (12, "new line at 12")


def test_parse_diff_with_lines_handles_deletions():
    diff = """diff --git a/x.py b/x.py
+++ b/x.py
@@ -5,5 +5,4 @@
 context 5
-removed 6
-removed 7
+added at 6
 context 8
"""
    result = parse_unified_diff_with_lines(diff)
    pairs = result["x.py"]
    assert len(pairs) == 1
    assert pairs[0] == (6, "added at 6")


def test_test_gap_includes_line_number():
    diff = """diff --git a/src/foo.py b/src/foo.py
+++ b/src/foo.py
@@ -1,2 +1,4 @@
 existing
 existing2
+def new_thing(x):
+    return x * 2
"""
    gaps = find_test_gaps(diff)
    assert len(gaps) == 1
    assert gaps[0].symbol == "new_thing"
    # The `def new_thing` line should be at line 3 in the new file.
    assert gaps[0].line == 3


def test_parse_diff_multiple_hunks_resets_line_counter():
    diff = """diff --git a/a.py b/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 ctx
+line at 2
 ctx2
@@ -20,1 +21,2 @@
 ctx
+line at 22
"""
    result = parse_unified_diff_with_lines("a.py")  # wrong arg
    # Re-run with the actual diff
    result = parse_unified_diff_with_lines(diff)
    pairs = result["a.py"]
    # Should have two entries with different line numbers.
    assert (2, "line at 2") in pairs
    assert (22, "line at 22") in pairs


# ----- Test file detection -----

def test_is_test_file_python_pytest():
    assert is_test_file("tests/test_foo.py") is True
    assert is_test_file("src/foo/tests/test_bar.py") is True


def test_is_test_file_go():
    assert is_test_file("internal/user_test.go") is True


def test_is_test_file_jest():
    assert is_test_file("src/foo.test.ts") is True
    assert is_test_file("src/foo.spec.js") is True
    assert is_test_file("src/__tests__/foo.tsx") is True


def test_is_test_file_negative():
    assert is_test_file("src/foo.py") is False
    assert is_test_file("src/user.go") is False


# ----- Referenced identifiers -----

def test_collect_referenced_identifiers():
    lines = ["import { myFunc } from './foo';", "expect(myFunc(1)).toBe(2);"]
    idents = collect_referenced_identifiers(lines)
    assert "myFunc" in idents
    assert "expect" in idents


# ----- End-to-end gap finding -----

def test_gap_found_when_no_test_file_references_symbol():
    diff = """diff --git a/src/module.py b/src/module.py
+++ b/src/module.py
@@ -0,0 +1,3 @@
+def new_thing(x):
+    return x * 2
+
"""
    gaps = find_test_gaps(diff)
    assert len(gaps) == 1
    assert gaps[0].symbol == "new_thing"


def test_no_gap_when_test_file_references_symbol():
    diff = """diff --git a/src/module.py b/src/module.py
+++ b/src/module.py
@@ -0,0 +1,2 @@
+def new_thing(x):
+    return x * 2
diff --git a/tests/test_module.py b/tests/test_module.py
+++ b/tests/test_module.py
@@ -0,0 +1,2 @@
+def test_new_thing():
+    assert new_thing(3) == 6
"""
    gaps = find_test_gaps(diff)
    assert gaps == []


def test_multiple_gaps_grouped_by_file():
    diff = """diff --git a/a.go b/a.go
+++ b/a.go
@@ -0,0 +1,2 @@
+func AlphaFunc() {}
+func BetaFunc() {}
"""
    gaps = find_test_gaps(diff)
    assert len(gaps) == 2
    names = sorted(g.symbol for g in gaps)
    assert names == ["AlphaFunc", "BetaFunc"]


def test_summarize_gaps_empty():
    out = summarize_gaps([])
    assert "ok" in out.lower()


def test_summarize_gaps_groups_by_file():
    diff = """diff --git a/a.go b/a.go
+++ b/a.go
@@ -0,0 +1,2 @@
+func AlphaFunc() {}
+func BetaFunc() {}
"""
    gaps = find_test_gaps(diff)
    out = summarize_gaps(gaps)
    assert "a.go" in out
    assert "AlphaFunc" in out
    assert "BetaFunc" in out
