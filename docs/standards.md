# Standards enforcement

Seneschal's standards module (P3) surfaces three opt-in checks as
findings on every PR review:

1. **License-header scan** — new source files must carry the configured
   license header.
2. **Strict commit-convention** — PR titles must follow conventional-commit
   style (`feat:`, `fix:`, etc.).
3. **Branch-naming regex** — the PR's head-ref must match at least one
   configured pattern.

All three checks are **OFF by default**. Enable them per-repo by adding a
`standards:` block to `.seneschal.yml` at the repo root.

## Schema

```yaml
standards:
  # --- License-header scan ---------------------------------------------
  # Inline header text. Supports {YEAR} placeholder (matches any 4-digit
  # year). Multi-line strings are compared line-by-line.
  license_header: |
    // Copyright {YEAR} Acme Corp.
    // Licensed under the MIT License.

  # Alternative: point at a file inside the repo. Inline `license_header`
  # wins if both are set. Path is resolved relative to the repo root and
  # vetted for traversal (reuses the `safe_changelog_path` deny-list).
  license_header_file: LICENSE_HEADER.txt

  # Optional glob filter. Empty = check every newly-added file. Non-empty
  # = only check files matching at least one glob. Supports `**`.
  license_applies_to:
    - "**/*.go"
    - "**/*.py"
    - "src/**/*.ts"

  # Files matching any of these globs are skipped even if they match
  # `license_applies_to`. Useful for vendored code and generated files.
  license_exemptions:
    - "vendor/**"
    - "**/generated/**"
    - "third_party/**"

  # --- Strict commit-convention ----------------------------------------
  # When true, PR titles that don't match `type:` or `type(scope):`
  # produce a WARNING finding. Accepted types come from title_check.py's
  # CONVENTIONAL_TYPES (feat, fix, docs, style, refactor, perf, test,
  # build, ci, chore, revert).
  commit_convention_strict: true

  # --- Branch-naming regex ---------------------------------------------
  # List of regex patterns. ANY match = valid; empty list = feature OFF.
  # Patterns are truncated to ~200 chars at parse-time (ReDoS defense).
  # Invalid regex patterns are logged to stderr and skipped rather than
  # crashing the check.
  branch_name_patterns:
    - "^feat/"
    - "^fix/"
    - "^chore/"
    - "^release/v[0-9]+"

  # --- Severity overrides ----------------------------------------------
  # Per-category overrides. Accepted values: blocker, warning, nit, info.
  # None / omitted = use the category default:
  #   license → warning
  #   commit-convention (strict mode) → warning
  #   branch-name → nit
  license_severity: warning
  commit_convention_severity: warning
  branch_name_severity: nit
```

## Examples

### Go monorepo with vendored dependencies

```yaml
standards:
  license_header: "// Copyright {YEAR} Acme Corp. All rights reserved."
  license_applies_to:
    - "**/*.go"
  license_exemptions:
    - "vendor/**"
  commit_convention_strict: true
  branch_name_patterns:
    - "^feat/"
    - "^fix/"
    - "^release/"
```

### Python library with loose branching

```yaml
standards:
  # Use a header file so the Apache 2.0 boilerplate lives in one place.
  license_header_file: LICENSE_HEADER.txt
  license_applies_to:
    - "src/**/*.py"
  # Allow any branch name — this feature stays OFF by omitting the knob.
  commit_convention_strict: false
```

## Behavior notes

### License check fires only on NEW files

Modifying an existing file that's missing a header does NOT trigger the
scan — the intent is to keep new additions in compliance, not retrofit
the repo. New-file detection prefers GitHub's PR-files API `status` field
when available, and falls back to the `new file mode` marker in the raw
diff.

### `{YEAR}` placeholder semantics

The literal string `{YEAR}` in `license_header` is translated to the
regex `\d{4}` (any 4 consecutive digits). So this header:

```
// Copyright {YEAR} Acme Corp.
```

...accepts `// Copyright 2024 Acme Corp.`, `// Copyright 2026 Acme Corp.`,
etc. It does NOT accept `// Copyright YEAR Acme Corp.` (literal `YEAR`)
or `// Copyright 24 Acme Corp.` (two digits).

No other placeholders are honored — everything else in the header text
is treated as a literal.

### `**` glob support

Seneschal's `glob_match` helper promotes patterns containing `**` to
regex (stdlib `fnmatch` doesn't recognize `**`):

- `**` matches zero or more path segments (including `/`).
- `**/*.go` matches `foo.go`, `a/foo.go`, `a/b/foo.go`.
- `src/**` matches `src/a.go`, `src/a/b.go`.
- `*` matches within a single segment (no `/`).
- `?` matches a single character (no `/`).

Simple patterns with no `**` fall back to `fnmatch.fnmatch`.

### Double-finding suppression

Seneschal's `title_check.py` already emits a *soft nudge* finding when a
PR title looks vague ("wip", "update stuff", etc.). When you enable
`commit_convention_strict: true`, the strict check and the soft nudge
would otherwise both fire on the same PR. To avoid double-reporting,
the soft `title` finding is suppressed whenever the strict
`commit-convention` finding is active.

If `commit_convention_strict` is false (default), the soft nudge
continues to fire as normal.

### Binary files are skipped

The license scanner looks for NUL bytes in the first ~40 added lines. If
found, the file is skipped (binary images, protobuf descriptors, etc.).

### Header text is capped at 2KB

`license_header` strings longer than 2048 bytes are truncated during
YAML parsing. `license_header_file` contents are similarly capped after
being read from disk.

## Interaction with other findings

Standards findings appear in the same pre-review analysis body as every
other finding. They respect the severity ordering (BLOCKER → WARNING →
NIT → INFO), so upgrading `license_severity: blocker` will push missing
headers to the top of the review.

The strict commit-convention finding does NOT gate approval on its own —
it's a WARNING by default. If you want to block PRs with non-conforming
titles, set `commit_convention_severity: blocker` (Seneschal's downstream
review will then emit a BLOCKER finding, and `has_blockers()` becomes
true).
