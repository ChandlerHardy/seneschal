# seneschal-personal — PRIVATE FORK

This is the private personal-deployment fork of
[`ChandlerHardy/seneschal`](https://github.com/ChandlerHardy/seneschal).

**Do not publish this repo.** The patches here re-introduce a Claude CLI
(`claude -p`) backend that the public repo intentionally does NOT ship
for TOS reasons. Keeping this fork private is the whole point.

## Diff against upstream

| File | Change |
|------|--------|
| `backend_cli.py` | NEW. `CliBackend` implements the `Backend` protocol via `claude -p`. |
| `backend.py` | PATCH. `get_backend()` checks `SENESCHAL_BACKEND=cli` env and selects `CliBackend`. Otherwise identical to upstream. |
| `systemd/seneschal.service.private` | NEW. Systemd unit with `SENESCHAL_BACKEND=cli` preset. Replaces the upstream `seneschal.service` at deploy time. |
| `install.sh` | PATCH. Ships `backend_cli.py`, uses the `.private` systemd unit, skips the `ANTHROPIC_API_KEY` requirement. |
| `PRIVATE_FORK.md` | NEW. This file. |

Everything else tracks upstream verbatim. Keep the diff surface this
small so upstream merges stay near-zero-conflict.

## Remotes

- `origin` → `ChandlerHardy/seneschal-personal` (private)
- `upstream` → `ChandlerHardy/seneschal` (public)

## Pull upstream

```bash
git fetch upstream
git merge upstream/main
# Resolve any conflicts (rare — only backend.py factory + install.sh overlap)
git push origin main
```

## Deploy

```bash
./install.sh oci
```

The target host must have the `claude` CLI installed and authenticated
(run `claude` interactively once to auth). No `ANTHROPIC_API_KEY`
needed; `CliBackend` routes through the host's Claude session.

## Why this is personal use

Personal automation of the operator's own code review, on infrastructure
the operator has tenancy on, authenticated with the operator's own
Claude Max subscription, reviewing the operator's own PRs, is personal
use in any common-sense reading. Anthropic ships `claude -p` with a
programmatic flag specifically designed for this shape. The public TOS
concern is *publicly advertising or distributing* the Max-sub shortcut
as a product feature — resolved by stripping it from the public repo
entirely. This private fork is not distribution.

If the Max-auth on OCI ever drifts (re-auth friction) and you want a
zero-ambiguity fallback, set `ANTHROPIC_API_KEY=<zai-key>` and
`ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic/v1` in the systemd
unit and flip `SENESCHAL_BACKEND` off. Same wire format as real
Anthropic; GLM-backed rather than real Claude.
