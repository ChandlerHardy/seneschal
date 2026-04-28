# MCP server

Seneschal ships an optional [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes its review history and cross-repo knowledge to local
Claude Code sessions. Ask Claude "what did Seneschal say about my last
PR on chandlerhardy/seneschal?" or "search every review for 'migration'"
and get a real answer instead of "I don't have that context."

## Tools

### Single-repo review history

| Tool | Purpose |
|---|---|
| `seneschal_last_review(repo)` | Summary of the most recent review for a repo |
| `seneschal_review_history(repo, limit)` | Past N reviews, newest first |
| `seneschal_review_text(repo, pr_number)` | Full markdown body of one review |
| `seneschal_repo_memory(repo, repo_root)` | Contents of the repo's `.seneschal-memory.md` |

### Cross-repo knowledge custody

| Tool | Purpose |
|---|---|
| `seneschal_search_reviews(query, repo?, limit)` | Full-text search across every indexed review (snippets redacted for secrets) |
| `seneschal_search_adrs(query, repo?, limit)` | Full-text search across ADRs discovered in every known repo |
| `seneschal_merged_prs(repo?, since?, limit)` | Merged PRs from the index, newest-first, with optional `since` ISO-8601 lower bound |
| `seneschal_followups(repo?, status, limit)` | Open `seneschal-followup` issues across known repos (or one repo if scoped) |
| `seneschal_dependency_usage(package_name, limit)` | Grep every known repo's manifests for a package reference |

The first four cross-repo tools are read-only against the on-disk review
store + a local SQLite cache. `seneschal_followups` hits GitHub's issues
endpoint via a short-lived installation token (see _Authentication_
below). Repos where the Seneschal App isn't installed are silently
skipped rather than surfaced as errors.

## Install

The MCP server supports two transports — pick whichever matches your
deployment:

* **Stdio** (default): the local Claude Code process spawns the MCP
  server as a subprocess and talks to it over stdin/stdout. Right when
  the review store + bot live on the same machine you run Claude Code on.
* **HTTP**: the MCP server runs as a long-lived HTTP listener; Claude
  Code connects to it as a network client. Right when the bot lives on
  a server (e.g. an OCI box accessible via Tailscale) and you want
  desktop Claude Code to query the same review store the bot writes to,
  without copying state between hosts.

### Stdio (local)

1. Install the optional dependency:

   ```bash
   pip install fastmcp
   ```

2. Add to your Claude Code MCP config (`~/.claude.json`):

   ```json
   {
     "mcpServers": {
       "seneschal": {
         "command": "/path/to/seneschal/bin/seneschal-mcp-server",
         "args": []
       }
     }
   }
   ```

3. Restart Claude Code. Verify with `claude mcp list`.

### HTTP (remote, e.g. via Tailscale)

1. On the server, run the MCP entry point with `--http` (or set
   `SENESCHAL_MCP_TRANSPORT=http`). Recommended deployment is a systemd
   unit alongside the webhook bot — the public repo's webhook bot is
   stdio-agnostic, so failures in one process don't take down the other.

   ```bash
   SENESCHAL_MCP_TRANSPORT=http \
   SENESCHAL_MCP_HOST=<your-tailnet-ip> \
   SENESCHAL_MCP_PORT=9101 \
     /home/ubuntu/seneschal/venv/bin/python -m mcp_server.server
   ```

   Replace `<your-tailnet-ip>` with the address of the host's tailnet
   interface (`tailscale ip -4` on the server). Bind explicitly to that
   IP — never `0.0.0.0` on a publicly reachable host. The 127.0.0.1
   default exists so a forgotten env var produces a loopback bind, never
   an open public listener.

2. On the client (your laptop), register with Claude Code over HTTP:

   ```bash
   claude mcp add seneschal --transport http http://<host>:9101/mcp
   ```

   Where `<host>` is the Tailscale-resolvable name of your server (e.g.
   `oci`, or its `100.x.y.z` MagicDNS / IP).

3. Verify with `claude mcp list` — seneschal should show as connected.
   Call any tool from Claude Code; the request flows over Tailscale to
   the server's review store and SQLite index.

#### Auth model

The HTTP endpoint has no application-level auth — the tailnet ACL is
the trust boundary. This is the right shape for personal/single-operator
deployments. For multi-user or multi-tenant deployments, front the
listener with a reverse proxy that enforces token auth.

#### Offline cache (optional)

When the server is unreachable (no network, server down), the MCP
client can fall back to a local mirror of the review store. The
canonical setup is a periodic `rsync` from the server's
`~/.seneschal/reviews/` (and `index.db`) into the same paths on the
client, then a stdio fallback registration that points at the local
copy. Most users will never need this — when the network is down,
Claude Code itself usually is too.

### Env summary

The HTTP-transport knobs (CLI args win when both are set):

| Variable | CLI flag | Default | Purpose |
|---|---|---|---|
| `SENESCHAL_MCP_TRANSPORT` | `--http` | `stdio` | Set to `http` to bind a network listener. |
| `SENESCHAL_MCP_HOST` | `--host` | `127.0.0.1` | HTTP bind address. Set to your tailnet IP. |
| `SENESCHAL_MCP_PORT` | `--port` | `9101` | HTTP bind port. |
| `SENESCHAL_MCP_PATH` | `--path` | `/mcp` | HTTP endpoint path. |

## Data layout

Each successfully posted review is persisted as a markdown file with
JSON frontmatter:

```
~/.seneschal/reviews/
  chandlerhardy/
    seneschal/
      12.md
      14.md
    elucidate-chess/
      3.md
```

Example `12.md`:

```markdown
---
{
  "pr_number": 12,
  "verdict": "APPROVE",
  "timestamp": "2026-04-18T18:42:00Z",
  "url": "https://github.com/chandlerhardy/seneschal/pull/12#pullrequestreview-123"
}
---
## Pre-review analysis

Risk: low  — focused 2-file change.

...
```

Override the storage root with the `SENESCHAL_REVIEW_STORE` env var
(useful in tests and for teams that want to keep the store on shared storage).

## SQLite index

The cross-repo search tools (`seneschal_search_reviews`,
`seneschal_search_adrs`, `seneschal_merged_prs`) are backed by a SQLite
cache at `~/.seneschal/index.db`. The markdown store is always canonical —
if the DB is corrupted or the schema changes, it's silently dropped and
rebuilt on the next MCP server restart. Rebuild cost is O(n reviews);
even a very active installation finishes in well under a second.

The index is FTS5-enabled when the host's SQLite build supports it,
with an automatic LIKE `'%...%'` fallback otherwise. FTS query syntax
is sanitized — user queries are treated as literal phrases, so `foo-bar`
or `x AND unterminated"` won't crash the tool.

Override the index path with the `SENESCHAL_INDEX_PATH` env var.

## Cross-repo enumeration

`seneschal_search_adrs`, `seneschal_followups`, and
`seneschal_dependency_usage` walk every local git checkout under
`SENESCHAL_REPOS_ROOT` (default `~/repos`) that carries a GitHub origin
URL in `.git/config`. Non-GitHub origins (GitLab, Bitbucket, personal
hosts) are silently skipped.

**Cache invalidation:** the enumeration is cached for the MCP server's
process lifetime. If you clone a new repo that should be part of the
working set, restart the MCP server (`claude mcp restart seneschal` or
terminate the stdio process). Same goes for new ADRs in a repo the MCP
server already knows about — the index sync runs once at startup.

Override the root with `SENESCHAL_REPOS_ROOT`.

## Authentication

`seneschal_followups` is the only tool that leaves the machine. It mints
a short-lived (50-minute) GitHub App installation token per repo via the
`seneschal_token.mint_installation_token` helper. The PEM is read from
`~/seneschal/ch-code-reviewer.pem` by default.

**PAT fallback:** if you set `SENESCHAL_GITHUB_TOKEN=ghp_...` the MCP
server uses that token verbatim for every `seneschal_followups` call,
skipping the App-mint flow. Handy for local development when the App
isn't installed, or to give a read-only PAT when you don't want to ship
the private key.

Env summary:

| Variable | Default | Purpose |
|---|---|---|
| `SENESCHAL_REVIEW_STORE` | `~/.seneschal/reviews` | Canonical review markdown store |
| `SENESCHAL_INDEX_PATH` | `~/.seneschal/index.db` | SQLite cache path |
| `SENESCHAL_REPOS_ROOT` | `~/repos` | Where to enumerate local git checkouts |
| `SENESCHAL_APP_ID` | `3127694` | GitHub App numeric ID |
| `SENESCHAL_PEM_PATH` | `~/seneschal/ch-code-reviewer.pem` | Path to App private key |
| `SENESCHAL_GITHUB_TOKEN` | (unset) | PAT that replaces App-mint if set |

## When it's useful

- You're about to open a PR and want to recall what Seneschal flagged last time.
- You're paging in on an unfamiliar repo and want a tour of recent review activity.
- You're writing a follow-up PR to address Seneschal's feedback and want the
  full context of the prior review without digging through GitHub.
- A CVE drops for a package in your stack — `seneschal_dependency_usage`
  gives you the list of repos to patch in one query.
- You want to know what's on your plate across every repo —
  `seneschal_followups` aggregates `seneschal-followup` issues that
  Seneschal filed from post-merge reviews.
