# Seneschal

**A self-hosted AI code reviewer that sees more than the diff.**

Runs as a GitHub App on your own infrastructure. Reviews pull requests on demand, files follow-up fixes as commits, and — unlike hosted alternatives — can tap into your repo's decision history, your configured review personas, your CI test results, and your local editor.

Uses your own Claude Max subscription via `claude -p` instead of per-token billing.

---

## What makes it different

Most AI code review bots only see the diff. Seneschal sees:

| Context type | What it does |
|---|---|
| **Configurable review personas** | Six builtins (architect, security, simplifier, design, data-integrity, edge-case) plus custom personas you define in markdown files in your repo. Drop in a `hipaa.md` or `performance.md` and the bot uses it. |
| **CI test results** | On webhook, fetches the GitHub Checks API for the PR's head SHA. Failing tests get surfaced to the reviewer, with correlation heuristics flagging which failures likely relate to the changed files. |
| **ADRs and decision logs** | Discovers your repo's ADRs (`docs/adr/`, `docs/decisions/`, `ADR.md`, etc.) and feeds the ones most relevant to the diff into the review prompt. A reviewer-with-team-history can flag "this re-introduces the pattern rejected in ADR-0042." |
| **Editor integration** | Ships an optional MCP server. Ask Claude Code "what did Seneschal say about my last PR?" and get a real answer without switching tabs. |

## How it compares

| | Seneschal | CodeRabbit | Greptile | Anthropic `claude-code-action` |
|---|---|---|---|---|
| Self-hosted | ✓ | ✗ | ✗ | (runs in GitHub Actions) |
| Code never leaves your infra | ✓ | ✗ | ✗ | — |
| Cost model | Your Claude Max plan | Per seat / mo | Per seat / mo | Per-token API |
| Opt-in per-PR trigger | ✓ (`/seneschal review`) | Auto on every PR | Auto | `@claude` mention |
| Multi-persona review | ✓ (configurable) | ✗ | ✗ | ✗ |
| CI-test-aware review | ✓ | ✗ | ✗ | ✗ |
| ADR / decision-log aware | ✓ | ✗ | partial | ✗ |
| MCP server for editor access | ✓ | ✗ | ✗ | ✗ |

## How it works

1. You install Seneschal as a GitHub App on your repos (one-time).
2. Seneschal runs as a long-lived Flask service on your server (systemd + webhook handler).
3. On a PR, a collaborator comments `/seneschal review`.
4. The bot clones the PR, pulls CI results from the Checks API, scans for ADRs, runs pre-review static analyzers (risk, scope drift, test gaps, secrets), and invokes `claude -p` with its review prompt.
5. It posts a formal PR review. If you've enabled auto-fix, it can commit follow-up patches.
6. The review is persisted locally so the MCP server can surface it later.

## Install

See [docs/mcp-server.md](docs/mcp-server.md) for the MCP piece. Full self-host guide coming; short version:

1. Clone this repo on your server.
2. Create a GitHub App, save its `.pem` and webhook secret to `~/seneschal/`.
3. Run `./install.sh <host>` to deploy via SSH.
4. In the systemd unit (`/etc/systemd/system/seneschal.service`), set:
   - `Environment=SENESCHAL_TRIGGER_AUTHORS=your-github-username`
   - `Environment=SENESCHAL_AUTOFIX_AUTHORS=your-github-username`
5. Point the GitHub App's webhook at `http://<your-host>:9100/webhook/seneschal`.

## Configuration

Per-repo config lives in `.seneschal.yml` at the repo root:

```yaml
rules:
  - "Prefer cobra over flag for Go CLIs"
  - "All new models must have unit tests"
ignore_paths:
  - docs/
  - examples/
review_style: concise        # concise | thorough | blunt
full_review: true            # invoke multi-persona review
auto_fix: false              # let the bot commit follow-up fixes

# Multi-persona review: six builtins plus your own markdown files.
# Omit `personas:` entirely and all six builtins run.
personas:
  - builtin: security
  - builtin: architect
  - file: .seneschal/personas/hipaa.md
```

A file-based persona is just prompt text — no frontmatter required:

```markdown
# HIPAA reviewer

Focus on:
- PHI (Protected Health Information) handling in any new field or endpoint
- Audit-logging coverage for read paths, not just writes
- Encryption-at-rest assumptions for new storage locations
- De-identification boundaries between logging and telemetry
```

Legacy `.ch-code-reviewer.yml` is still accepted for repos that haven't migrated.

## Status

Early public release. Extracted from a private monorepo on 2026-04-18. The bot is running in production on the author's infrastructure. Documentation is in flight.

## License

MIT. See [LICENSE](LICENSE).
