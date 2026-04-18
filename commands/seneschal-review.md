---
description: Multi-persona PR review for the Seneschal bot. Report-only — no triage, no auto-fix, just compile findings and exit.
---

# /seneschal-review

**Goal:** Spawn the Seneschal reviewer personas in parallel, aggregate their findings into one markdown table, write the result to a state file, and exit. This command is the autonomous, report-only counterpart to ferdinand's `/review-pr` — designed to run inside a `claude -p` invocation on OCI without any interactive prompts.

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

### 2. Spawn personas in parallel

Spawn six Task subagents, all in parallel (one Task call per persona, all in the same response). Each gets the same context block:

```
Task(subagent_type: "seneschal-architect", prompt: "
  Review PR #<num> on the current branch.

  Changed files:
  <list>

  Diff stat: <FILE_COUNT> files, <LINE_COUNT> lines

  Use Read/Grep on the files in the working tree to examine them
  directly. Use jcodemunch_search_symbols to verify cross-file
  references. Output the findings table format from your system prompt
  — nothing else.
")

Task(subagent_type: "seneschal-security", prompt: "<same context>")
Task(subagent_type: "seneschal-data-integrity", prompt: "<same context>")
Task(subagent_type: "seneschal-edge-case", prompt: "<same context>")
Task(subagent_type: "seneschal-design", prompt: "<same context>")
Task(subagent_type: "seneschal-simplifier", prompt: "<same context>")
```

All six MUST be in the same response so they run concurrently.

### 3. Aggregate findings

Each persona returns a markdown table (or `No findings.`). Combine them into a single report:

```markdown
# Seneschal review — PR #<num>

**Mode:** Full multi-persona
**Files:** <FILE_COUNT> changed
**Lines:** <LINE_COUNT> +/-
**Verdict:** <REQUEST_CHANGES if any BLOCKER, else COMMENT if any WARNING/MINOR, else APPROVE>

## Findings

### @architect
<table or "No findings.">

### @security
<table or "No findings.">

### @data-integrity
<table or "No findings.">

### @edge-case
<table or "No findings.">

### @design
<table or "No findings.">

### @simplifier
<table or "No findings.">

## Source counts

- architect: <N> blockers, <N> warnings, <N> minor
- security: <N> blockers, <N> warnings, <N> minor
- data-integrity: <N> blockers, <N> warnings, <N> minor
- edge-case: <N> blockers, <N> warnings, <N> minor
- design: <N> blockers, <N> warnings, <N> minor
- simplifier: <N> blockers, <N> warnings, <N> minor

**Total:** <N> blockers, <N> warnings, <N> minor

---
*Reviewed by Seneschal*
```

Drop the heading for any persona that returned `No findings.` Move them into a single line at the end:

> *Clean lanes: @architect, @security.*

### 4. Write state file

```bash
mkdir -p .claude/plans
cat > .claude/plans/seneschal-review-${PR}.md <<'STATE'
<aggregated report>
STATE
```

### 5. Post the review as Seneschal[bot]

Run `~/bin/seneschal-post <PR>` via the Bash tool. This single helper:
- Reads the state file you just wrote
- Resolves the GitHub remote from `git config`
- Mints a Seneschal App installation token via `~/seneschal/seneschal_token.py`
- Posts a formal PR review (`POST /repos/.../pulls/<N>/reviews`) under
  the `seneschal-cr[bot]` identity, with the verdict parsed from the
  `**Verdict:**` line of the state file

The helper is the same on both local Macs and OCI — both have
`~/seneschal/ch-code-reviewer.pem` and the token script. The bot path
(OCI) and the manual path (local) converge on this one Bash call.

If the helper exits non-zero (App not installed on this repo, network
error, malformed state file), capture stderr and surface it in the
final summary so the operator can see what went wrong. Do NOT retry —
let the operator decide.

### 6. Exit

Print a one-line summary to stdout:

```
seneschal-review: <verdict> · <total findings> finding(s) · posted <url-or-failure-reason>
```

Then exit. Do NOT prompt the user for anything. Do NOT spawn any fixer
agents. Do NOT modify any source files in the repo being reviewed.

## Verdict logic

- Any BLOCKER → `REQUEST_CHANGES`
- Any WARNING or MINOR → `COMMENT`
- All clean → `APPROVE`

The verdict is informational — the calling harness decides what to do with it (flip a label, post a review, etc.). The slash command itself only writes the state file.
