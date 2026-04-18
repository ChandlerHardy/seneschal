"""Tests for the quality scan (debug leftovers and TODO markers)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quality_scan import scan_quality, summarize_quality  # noqa: E402


def make_diff(filename, added_lines):
    header = f"diff --git a/{filename} b/{filename}\n+++ b/{filename}\n@@ -0,0 +1,{len(added_lines)} @@\n"
    body = "\n".join(f"+{line}" for line in added_lines)
    return header + body + "\n"


def test_clean_code():
    diff = make_diff("src/foo.py", ["def foo():", "    return 1"])
    assert scan_quality(diff) == []


def test_python_print_detected():
    diff = make_diff("src/foo.py", ["def foo():", "    print('debug')", "    return 1"])
    hits = scan_quality(diff)
    assert len(hits) == 1
    assert hits[0].kind == "debug:print"
    assert hits[0].line == 2


def test_python_breakpoint_detected():
    diff = make_diff("src/foo.py", ["    breakpoint()"])
    hits = scan_quality(diff)
    assert len(hits) == 1
    assert "breakpoint" in hits[0].kind


def test_go_fmt_println_detected():
    diff = make_diff("main.go", ["\tfmt.Println(\"debug\")"])
    hits = scan_quality(diff)
    assert len(hits) == 1
    assert "fmt.Println" in hits[0].kind


def test_console_log_detected():
    diff = make_diff("app.ts", ["  console.log('debug');"])
    hits = scan_quality(diff)
    assert len(hits) == 1
    assert "console.log" in hits[0].kind


def test_debugger_statement_detected():
    diff = make_diff("app.tsx", ["  debugger;"])
    hits = scan_quality(diff)
    assert len(hits) == 1
    assert "debugger" in hits[0].kind


def test_var_dump_detected():
    diff = make_diff("app.php", ["  var_dump($user);"])
    hits = scan_quality(diff)
    assert len(hits) == 1
    assert "var_dump" in hits[0].kind


def test_debug_leftover_in_test_file_ignored():
    # Test files are allowed to print freely.
    diff = make_diff("tests/test_foo.py", ["def test_foo():", "    print('ok')"])
    hits = [h for h in scan_quality(diff) if h.kind.startswith("debug:")]
    assert hits == []


def test_todo_comment_detected():
    diff = make_diff("src/foo.py", ["# TODO: refactor this later"])
    hits = scan_quality(diff)
    assert len(hits) == 1
    assert hits[0].kind == "todo:todo"


def test_fixme_comment_detected():
    diff = make_diff("main.go", ["// FIXME: broken on edge case"])
    hits = scan_quality(diff)
    assert len(hits) == 1
    assert hits[0].kind == "todo:fixme"


def test_hack_comment_detected():
    diff = make_diff("util.ts", ["// HACK: remove when issue #42 fixed"])
    hits = scan_quality(diff)
    assert len(hits) == 1
    assert hits[0].kind == "todo:hack"


def test_todo_in_test_file_still_flagged():
    diff = make_diff("tests/test_foo.py", ["# TODO: add more cases"])
    hits = scan_quality(diff)
    assert len(hits) == 1
    assert hits[0].kind == "todo:todo"


def test_node_modules_ignored():
    diff = make_diff("node_modules/pkg/index.js", ["console.log('x');"])
    assert scan_quality(diff) == []


def test_vendored_dir_ignored():
    diff = make_diff("vendor/lib.go", ["fmt.Println(\"x\")"])
    assert scan_quality(diff) == []


def test_generated_dir_ignored():
    diff = make_diff("generated/proto.go", ["fmt.Println(\"x\")"])
    assert scan_quality(diff) == []


def test_print_in_comment_not_flagged():
    # "print" inside a comment shouldn't match the debug pattern because
    # the regex requires the line to start with optional whitespace then
    # `print(` — a leading `#` or `//` breaks the match.
    diff = make_diff("src/foo.py", ["# we used to print(x) here"])
    hits = [h for h in scan_quality(diff) if h.kind.startswith("debug:")]
    assert hits == []


def test_summarize_clean():
    assert "clean" in summarize_quality([]).lower()


def test_summarize_with_hits():
    diff = make_diff(
        "src/foo.py",
        [
            "print('debug')",
            "# TODO: refactor",
            "print('another')",
        ],
    )
    hits = scan_quality(diff)
    out = summarize_quality(hits)
    assert "3 hit" in out
    assert "debug:print" in out
    assert "todo:todo" in out
