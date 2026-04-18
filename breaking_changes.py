"""Detect potentially breaking API changes from a PR diff.

Compares removed vs added lines per file to find exported-function
signature changes and function removals. Go-only for now; easy to extend.

The signal here is intentionally noisy — false positives are expected
because we work from text rather than an AST. The value is in surfacing
candidates for the reviewer to look at, not in making merge decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from diff_parser import parse_diff_both_sides as _parse_diff_both_sides


def _find_matching(s: str, start: int, open_ch: str, close_ch: str) -> int:
    """Return the index of the bracket that closes s[start].

    Assumes s[start] == open_ch. Returns -1 if no match (unbalanced input).
    """
    if start >= len(s) or s[start] != open_ch:
        return -1
    depth = 0
    i = start
    while i < len(s):
        ch = s[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_go_func_line(line: str) -> Optional[Tuple[str, str]]:
    """Parse a single line containing a Go function declaration.

    Returns (name, signature_tail) for exported functions, or None.

    Handles cases the previous regex-only approach got wrong:
      - args containing nested parens / callbacks: ``func Register(fn func() error)``
      - generic functions: ``func Foo[T any](x T) T``
      - unnamed receivers: ``func (*Server) Foo(...)``
      - package-qualified receivers: ``func (s *pkg.Config) Foo(...)``
      - generic receivers: ``func (s *Server[T]) Foo(...)``

    Hand-rolled because Go signatures are not a regular language — the
    nested parens and brackets need a balanced scanner.
    """
    s = line.lstrip()
    if not s.startswith("func"):
        return None
    s = s[4:]
    if not s or not s[0].isspace():
        return None
    s = s.lstrip()

    # Optional receiver: (...). Use balanced-paren matching so nested parens
    # inside qualified types (`*pkg.Config`) and generics (`Server[T]`) work.
    if s.startswith("("):
        end = _find_matching(s, 0, "(", ")")
        if end < 0:
            return None
        s = s[end + 1:].lstrip()

    # Function name. Must start with an uppercase letter to be exported.
    if not s or not s[0].isalpha():
        return None
    i = 0
    while i < len(s) and (s[i].isalnum() or s[i] == "_"):
        i += 1
    name = s[:i]
    if not name or not name[0].isupper():
        return None
    s = s[i:]

    # Optional generic type parameters: [T any, ...].
    if s.startswith("["):
        end = _find_matching(s, 0, "[", "]")
        if end < 0:
            return None
        s = s[end + 1:]

    # Required arg list: (...). Balanced so callbacks like `func() error` work.
    if not s.startswith("("):
        return None
    arg_end = _find_matching(s, 0, "(", ")")
    if arg_end < 0:
        return None
    args = s[: arg_end + 1]
    rest = s[arg_end + 1:].rstrip()
    if rest.endswith("{"):
        rest = rest[:-1].rstrip()

    signature = args
    if rest:
        signature = f"{args} {rest}"
    return name, signature


def parse_diff_both_sides(diff_text: str) -> Dict[str, Dict[str, List[str]]]:
    """Delegate to `diff_parser.parse_diff_both_sides`.

    Kept as a thin local name so older callers of
    `breaking_changes.parse_diff_both_sides` still work. New code should
    import directly from `diff_parser`.
    """
    return _parse_diff_both_sides(diff_text)


@dataclass
class BreakingChange:
    kind: str  # "signature-change" or "function-removed"
    file: str
    name: str
    old_signature: str = ""
    new_signature: str = ""

    def summary(self) -> str:
        if self.kind == "function-removed":
            return f"Removed exported function `{self.name}` from {self.file}"
        return (
            f"Signature changed for `{self.name}` in {self.file}: "
            f"`{self.old_signature}` -> `{self.new_signature}`"
        )


def _extract_go_signatures(lines: List[str]) -> Dict[str, str]:
    """Return {name: signature_tail} for every exported function in the list."""
    out: Dict[str, str] = {}
    for line in lines:
        parsed = _parse_go_func_line(line)
        if parsed:
            name, sig = parsed
            out[name] = sig.strip()
    return out


def detect_breaking_changes(diff_text: str) -> List[BreakingChange]:
    """Scan a diff for Go function signature changes and removals."""
    per_file = parse_diff_both_sides(diff_text)
    out: List[BreakingChange] = []

    for filename, sides in per_file.items():
        if not filename.endswith(".go"):
            continue
        if filename.endswith("_test.go"):
            continue

        removed_sigs = _extract_go_signatures(sides["removed"])
        added_sigs = _extract_go_signatures(sides["added"])

        for name, old in removed_sigs.items():
            if name in added_sigs:
                new = added_sigs[name]
                if _normalize(new) != _normalize(old):
                    out.append(BreakingChange(
                        kind="signature-change",
                        file=filename,
                        name=name,
                        old_signature=old,
                        new_signature=new,
                    ))
            else:
                out.append(BreakingChange(
                    kind="function-removed",
                    file=filename,
                    name=name,
                ))
    return out


def _normalize(sig: str) -> str:
    """Collapse whitespace for signature comparison."""
    return " ".join(sig.split())


def summarize_breaking(changes: List[BreakingChange]) -> str:
    if not changes:
        return "**API stability: ok** — no exported signatures changed or removed."
    lines = [f"**API stability: {len(changes)} potential breaking change(s)**", ""]
    for c in changes:
        lines.append(f"- {c.summary()}")
    return "\n".join(lines)
