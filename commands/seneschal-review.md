---
description: Multi-persona PR review for the Seneschal bot. Report-only — no triage, no auto-fix, just compile findings and exit.
---

# /seneschal-review

**Goal:** Spawn the configured reviewer personas in parallel, aggregate their findings into one markdown table, write the result to a state file, and exit. This command is the autonomous, report-only counterpart to ferdinand's `/review-pr` — designed to run inside a `claude -p` invocation on a server without any interactive prompts.

## Non-Negotiables

1. **CONTEXT gate:** if no diff exists, STOP
2. **NO TRIAGE GATE:** never ask the user to confirm anything
3. **NO HEAL LOOP:** never spawn fixers or implementers — this command only catches
4. **WRITE STATE FILE:** always write `.claude/plans/seneschal-review-{pr-number}.md` so the calling Python harness can read and post it

## Usage

```
/seneschal-review <pr-number>
```

The PR number is required. The command assumes the current working directory is the cloned repo (the bot's `ensure_repo_synced` puts the repo there before invoking `claude -p`).

## Flow

### 1. Resolve PR + diff

```bash
PR=$1
[ -z "$PR" ] && echo "Usage: /seneschal-review <pr-number>" && exit 1

# Fetch PR head + diff via gh.
HEAD_SHA=$(gh pr view "$PR" --json headRefOid --jq .headRefOid)
[ -z "$HEAD_SHA" ] && echo "PR $PR not found" && exit 1

DIFF=$(gh pr diff "$PR")
[ -z "$DIFF" ] && echo "Empty diff for PR $PR" && exit 0

CHANGED_FILES=$(gh pr view "$PR" --json files --jq '.files[].path')
FILE_COUNT=$(echo "$CHANGED_FILES" | wc -l | tr -d ' ')
LINE_COUNT=$(gh pr diff "$PR" | grep -cE '^[+-][^+-]' || echo 0)
```

Do NOT dump the full raw diff. Agents read files from the working tree directly.

### 2. Load persona config

Check for `.claude/plans/seneschal-personas-${PR}.json`. This file — written by the Python harness just before invoking this slash command — tells you which personas the repo has configured. The schema is:

```json
{
  "pr_number": 123,
  "personas": [
    {"name": "architect", "subagent_type": "seneschal-architect",
     "prompt_text": "", "source": "builtin"},
    {"name": "hipaa", "subagent_type": null,
     "prompt_text": "You are a HIPAA reviewer. Focus on ...",
     "source": "file:.seneschal/personas/hipaa.md"}
  ]
}
```

**If the state file exists and parses cleanly**, use that persona list. Each entry becomes exactly one Task subagent in step 3.

**If the state file is missing or malformed**, fall back to the six built-in personas (same as pre-v2 behavior):
- `seneschal-architect`, `seneschal-security`, `seneschal-data-integrity`, `seneschal-edge-case`, `seneschal-design`, `seneschal-simplifier`

Read the file with:

```bash
STATE=".claude/plans/seneschal-personas-${PR}.json"
if [ -f "$STATE" ]; then
  cat "$STATE"
else
  echo "(no persona config — running six builtins)"
fi
```

### 3. Spawn personas in parallel

For each persona in the config, spawn exactly one Task subagent. **All Task calls MUST be in the same response so they run concurrently.**

**For builtin personas** (where `subagent_type` is set), use the named subagent:

```
Task(subagent_type: "<subagent_type>", prompt: "
  Review PR #<num> on the current branch.

  Changed files:
  <list>

  Diff stat: <FILE_COUNT> files, <LINE_COUNT> lines

  Use Read/Grep on the files in the working tree to examine them
  directly. Use jcodemunch_search_symbols to verify cross-file
  references. Output the findings table format from your system prompt
  — nothing else.
")
```

**For file-based personas** (where `subagent_type` is null), use `general-purpose` and prepend the persona's prompt text:

```
Task(subagent_type: "general-purpose", prompt: "
  <persona.prompt_text>

  ---

  Review PR #<num> on the current branch.

  Changed files:
  <list>

  Diff stat: <FILE_COUNT> files, <LINE_COUNT> lines

  Use Read/Grep on the files in the working tree to examine them
  directly. Output findings in a markdown table with columns:
  Severity | File:line | Title | Detail
  Severities: BLOCKER, WARNING, MINOR. If no findings, output 'No findings.'
")
```

The file-based persona provides the reviewer perspective (what to look for) and the appended context tells a general-purpose subagent how to format output.

### 4. Aggregate findings

Each persona returns a markdown table (or `No findings.`). Combine them into a single report. Use the persona's `name` field as the section header (e.g. `@architect`, `@hipaa`).

```markdown
# Seneschal review — PR #<num>

**Mode:** Full multi-persona (<N> personas)
**Files:** <FILE_COUNT> changed
**Lines:** <LINE_COUNT> +/-
**Verdict:** <REQUEST_CHANGES if any BLOCKER, else COMMENT if any WARNING/MINOR, else APPROVE>

## Findings

### @<persona-name-1>
<table or "No findings.">

### @<persona-name-2>
<table or "No findings.">

...

## Source counts

- <persona-name-1>: <N> blockers, <N> warnings, <N> minor
- <persona-name-2>: <N> blockers, <N> warnings, <N> minor
...

**Total:** <N> blockers, <N> warnings, <N> minor

---
*Reviewed by Seneschal*
```

Drop the heading for any persona that returned `No findings.` Move them into a single line at the end:

> *Clean lanes: @architect, @security.*

### 5. Write state file

```bash
mkdir -p .claude/plans
cat > .claude/plans/seneschal-review-${PR}.md <<'STATE'
<aggregated report>
STATE
```

### 6. Post the review as Seneschal[bot]

Run `~/bin/seneschal-post <PR>` via the Bash tool. This single helper:
- Reads the state file you just wrote
- Resolves the GitHub remote from `git config`
- Mints a Seneschal App installation token via `~/seneschal/seneschal_token.py`
- Posts a formal PR review (`POST /repos/.../pulls/<N>/reviews`) under the `seneschal-cr[bot]` identity, with the verdict parsed from the `**Verdict:**` line of the state file

The helper is the same on both local Macs and servers — both have `~/seneschal/ch-code-reviewer.pem` and the token script.

If the helper exits non-zero (App not installed on this repo, network error, malformed state file), capture stderr and surface it in the final summary so the operator can see what went wrong. Do NOT retry — let the operator decide.

### 7. Exit

Print a one-line summary to stdout:

```
seneschal-review: <verdict> · <total findings> finding(s) · posted <url-or-failure-reason>
```

Then exit. Do NOT prompt the user for anything. Do NOT spawn any fixer agents. Do NOT modify any source files in the repo being reviewed.

## Verdict logic

- Any BLOCKER → `REQUEST_CHANGES`
- Any WARNING or MINOR → `COMMENT`
- All clean → `APPROVE`

The verdict is informational — the calling harness decides what to do with it (flip a label, post a review, etc.). The slash command itself only writes the state file.
