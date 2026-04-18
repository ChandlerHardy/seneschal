"""Tests for the Go breaking-change detector."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from breaking_changes import (  # noqa: E402
    BreakingChange,
    detect_breaking_changes,
    parse_diff_both_sides,
    summarize_breaking,
)


def test_parse_diff_both_sides_captures_adds_and_removes():
    diff = """diff --git a/x.go b/x.go
+++ b/x.go
@@ -1,3 +1,3 @@
 unchanged
-old line
+new line
"""
    result = parse_diff_both_sides(diff)
    assert "x.go" in result
    assert result["x.go"]["removed"] == ["old line"]
    assert result["x.go"]["added"] == ["new line"]


def test_signature_change_detected():
    diff = """diff --git a/store.go b/store.go
+++ b/store.go
@@ -1,3 +1,3 @@
-func FindUser(id string) *User {
+func FindUser(id string, ctx context.Context) *User {
 \treturn nil
 }
"""
    changes = detect_breaking_changes(diff)
    assert len(changes) == 1
    c = changes[0]
    assert c.kind == "signature-change"
    assert c.name == "FindUser"
    assert "context.Context" in c.new_signature
    assert "context.Context" not in c.old_signature


def test_function_removal_detected():
    diff = """diff --git a/store.go b/store.go
+++ b/store.go
@@ -5,3 +4,0 @@
-func DeletedFunc(x int) error {
-\treturn nil
-}
"""
    changes = detect_breaking_changes(diff)
    removals = [c for c in changes if c.kind == "function-removed"]
    assert len(removals) == 1
    assert removals[0].name == "DeletedFunc"


def test_unchanged_function_not_flagged():
    diff = """diff --git a/store.go b/store.go
+++ b/store.go
@@ -5,3 +5,4 @@
-func Thing(x int) error {
+func Thing(x int) error {
 \tnew_body_line := 1
 \treturn nil
 }
"""
    changes = detect_breaking_changes(diff)
    # Signatures are identical — no breaking change.
    assert changes == []


def test_private_function_not_flagged():
    diff = """diff --git a/store.go b/store.go
+++ b/store.go
@@ -1,3 +1,3 @@
-func thing(x int) error {
+func thing(x int, y int) error {
 \treturn nil
 }
"""
    changes = detect_breaking_changes(diff)
    # Lowercase name = not exported, not tracked.
    assert changes == []


def test_test_file_ignored():
    diff = """diff --git a/store_test.go b/store_test.go
+++ b/store_test.go
@@ -1,3 +1,3 @@
-func TestOld() {
+func TestNew() {
 }
"""
    changes = detect_breaking_changes(diff)
    assert changes == []


def test_non_go_file_ignored():
    diff = """diff --git a/src/foo.py b/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,3 @@
-def foo():
+def foo(x):
 \treturn 1
"""
    changes = detect_breaking_changes(diff)
    assert changes == []


def test_receiver_method_signature_change():
    diff = """diff --git a/store.go b/store.go
+++ b/store.go
@@ -1,3 +1,3 @@
-func (r *Repo) Find(id string) *User {
+func (r *Repo) Find(id string, n int) *User {
 \treturn nil
 }
"""
    changes = detect_breaking_changes(diff)
    assert len(changes) == 1
    assert changes[0].name == "Find"


def test_callback_arg_signature_change_detected():
    """B5 regression: char class [^)] truncated callback signatures."""
    diff = """diff --git a/store.go b/store.go
+++ b/store.go
@@ -1,3 +1,3 @@
-func Register(fn func() error) error {
+func Register(fn func() error, name string) error {
 \treturn nil
 }
"""
    changes = detect_breaking_changes(diff)
    assert len(changes) == 1
    c = changes[0]
    assert c.kind == "signature-change"
    assert c.name == "Register"
    # Callback type must survive the parse.
    assert "func() error" in c.old_signature
    assert "func() error" in c.new_signature
    assert "name string" in c.new_signature
    assert "name string" not in c.old_signature


def test_generic_function_signature_change_detected():
    """B6 regression: generic type parameters were rejected entirely."""
    diff = """diff --git a/store.go b/store.go
+++ b/store.go
@@ -1,3 +1,3 @@
-func Map[T any](xs []T, f func(T) T) []T {
+func Map[T any](xs []T, f func(T) T, parallel bool) []T {
 \treturn xs
 }
"""
    changes = detect_breaking_changes(diff)
    assert len(changes) == 1
    assert changes[0].name == "Map"
    assert "parallel bool" in changes[0].new_signature


def test_unnamed_receiver_method_detected():
    """W27: unnamed receivers `func (*Server)` were rejected."""
    diff = """diff --git a/store.go b/store.go
+++ b/store.go
@@ -1,3 +1,3 @@
-func (*Server) Start(addr string) error {
+func (*Server) Start(addr string, opts ...Option) error {
 \treturn nil
 }
"""
    changes = detect_breaking_changes(diff)
    assert len(changes) == 1
    assert changes[0].name == "Start"


def test_qualified_receiver_method_detected():
    """W27: package-qualified receiver types `func (s *pkg.Config)` were rejected."""
    diff = """diff --git a/store.go b/store.go
+++ b/store.go
@@ -1,3 +1,3 @@
-func (c *config.Loader) Load(path string) error {
+func (c *config.Loader) Load(ctx context.Context, path string) error {
 \treturn nil
 }
"""
    changes = detect_breaking_changes(diff)
    assert len(changes) == 1
    assert changes[0].name == "Load"


def test_generic_receiver_method_detected():
    """W27: generic receivers `func (s *Server[T])` were rejected."""
    diff = """diff --git a/store.go b/store.go
+++ b/store.go
@@ -1,3 +1,3 @@
-func (s *Stack[T]) Push(v T) {
+func (s *Stack[T]) Push(v T, count int) {
 }
"""
    changes = detect_breaking_changes(diff)
    assert len(changes) == 1
    assert changes[0].name == "Push"


def test_summarize_empty():
    out = summarize_breaking([])
    assert "ok" in out.lower()


def test_summarize_with_changes():
    changes = [
        BreakingChange(kind="signature-change", file="x.go", name="Foo", old_signature="()", new_signature="(int)"),
        BreakingChange(kind="function-removed", file="y.go", name="Bar"),
    ]
    out = summarize_breaking(changes)
    assert "2 potential" in out
    assert "Foo" in out
    assert "Bar" in out
