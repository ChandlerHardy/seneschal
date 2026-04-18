# MCP server

Seneschal ships an optional [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes its review history to local Claude Code sessions.
Ask Claude "what did Seneschal say about my last PR on chandlerhardy/seneschal?"
and get a real answer instead of "I don't have that context."

## Tools

| Tool | Purpose |
|---|---|
| `seneschal_last_review(repo)` | Summary of the most recent review for a repo |
| `seneschal_review_history(repo, limit)` | Past N reviews, newest first |
| `seneschal_review_text(repo, pr_number)` | Full markdown body of one review |
| `seneschal_repo_memory(repo, repo_root)` | Contents of the repo's `.seneschal-memory.md` |

All tools are read-only. The server does not call the GitHub API or
spawn `claude -p` — it only reads files that the webhook handler
persisted to `~/.seneschal/reviews/<owner>/<repo>/<pr>.md`.

## Install

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

## When it's useful

- You're about to open a PR and want to recall what Seneschal flagged last time.
- You're paging in on an unfamiliar repo and want a tour of recent review activity.
- You're writing a follow-up PR to address Seneschal's feedback and want the
  full context of the prior review without digging through GitHub.
