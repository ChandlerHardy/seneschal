# Seneschal

**A self-hosted AI code reviewer that sees more than the diff.**

Runs as a GitHub App on your own infrastructure. Reviews pull requests on demand, files follow-up fixes as commits, and — unlike hosted alternatives — can tap into your repo's decision history, your configured review personas, and your CI test results.

Uses your own Claude Max subscription via `claude -p` instead of per-token billing.

---

## Status

Early public release. Extracted from a private monorepo on 2026-04-18.

The bot itself is running in production on the author's infrastructure. Documentation is in flight.

## How it differs from other AI review bots

| | Seneschal | CodeRabbit | Greptile | Anthropic `claude-code-action` |
|---|---|---|---|---|
| Self-hosted | ✓ | ✗ | ✗ | (GitHub Actions runners) |
| Code never leaves your infra | ✓ | ✗ | ✗ | — |
| Cost model | Your Claude Max plan | Per seat / mo | Per seat / mo | Per-token API |
| Opt-in per-PR trigger | ✓ (`/seneschal review`) | Auto on every PR | Auto | `@claude` mention |
| Multi-persona review | ✓ (configurable) | ✗ | ✗ | ✗ |
| CI-test-aware review | planned | ✗ | ✗ | ✗ |
| ADR / decision-log aware | planned | ✗ | partial | ✗ |
| MCP server for editor access | planned | ✗ | ✗ | ✗ |

## How it works

1. You install Seneschal as a GitHub App on your repos (one-time).
2. Seneschal runs as a long-lived Flask service on your server (systemd + webhook handler).
3. On a PR, a collaborator comments `/seneschal review`.
4. The bot clones the PR, runs pre-review static analyzers (risk, scope drift, test gaps, secrets), and invokes `claude -p` with its review prompt.
5. It posts a formal PR review. If you've enabled auto-fix, it can commit follow-up patches.

## Install

See [docs/install.md](docs/install.md) (in flight).

Short version:
1. Clone this repo on your server.
2. Create a GitHub App, save its `.pem` and webhook secret to `~/seneschal/`.
3. Run `./install.sh <host>` to deploy via SSH.
4. Set `SENESCHAL_TRIGGER_AUTHORS` and `SENESCHAL_AUTOFIX_AUTHORS` env vars in the systemd unit to your GitHub username.
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
full_review: false           # invoke multi-persona review
auto_fix: false              # let the bot commit follow-up fixes
```

## License

MIT. See [LICENSE](LICENSE).
