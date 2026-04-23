#!/usr/bin/env bash
# Deploy Seneschal to a remote host (typically an OCI / VPS box).
#
# Usage:
#   ./install.sh [host]
#
# Default host is "oci" (an SSH config alias). The target box must have:
#   - Python 3.9+ installed
#   - An SSH-accessible user with sudo for systemd management
#   - Port 9100 open (or whatever the Flask app binds to)
#
# Before running this, place on the target box:
#   ~/seneschal/ch-code-reviewer.pem     (GitHub App private key, chmod 600)
#   ~/seneschal/webhook-secret.txt       (GitHub App webhook secret, chmod 600)
#
# And edit /etc/systemd/system/seneschal.service to set:
#   Environment=ANTHROPIC_API_KEY=sk-ant-...
#   Environment=SENESCHAL_TRIGGER_AUTHORS=your-github-username

set -euo pipefail

HOST="${1:-oci}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Deploying Seneschal to ${HOST}..."

# Target install dir on the host
ssh "$HOST" "mkdir -p ~/seneschal ~/seneschal/post_merge"

# Create venv if missing
ssh "$HOST" "
  if [ ! -d ~/seneschal/venv ]; then
    python3 -m venv ~/seneschal/venv
    echo 'created ~/seneschal/venv'
  fi
"

# Ship Python sources flat (matches the flat layout of this repo).
# Glob every *.py at the repo root so newly-added modules ship
# automatically. Tests live under tests/ and are not caught by this glob.
# If a future file at the repo root shouldn't deploy, put it under
# scripts/ or bin/ instead. The smoke-import below (~line 84) still
# names the runtime-required modules explicitly so a missing module on
# the target is caught before systemd starts.
scp "$REPO_DIR"/*.py "${HOST}:~/seneschal/"
scp "$REPO_DIR/requirements.txt" "${HOST}:~/seneschal/requirements.txt"

# Ship post_merge package (P1)
for f in post_merge/__init__.py post_merge/changelog.py post_merge/release.py \
         post_merge/followups.py post_merge/orchestrator.py; do
  scp "$REPO_DIR/$f" "${HOST}:~/seneschal/$f"
done

# Install Python deps
ssh "$HOST" "~/seneschal/venv/bin/pip install -q -r ~/seneschal/requirements.txt && echo 'pip install: OK'"

# Ship persona subagent definitions. full_review.py reads these at runtime
# from ~/.claude/agents/seneschal-*.md when the full-review code path fires.
ssh "$HOST" "mkdir -p ~/.claude/agents"
for f in agents/seneschal-architect.md \
         agents/seneschal-security.md \
         agents/seneschal-data-integrity.md \
         agents/seneschal-edge-case.md \
         agents/seneschal-design.md \
         agents/seneschal-simplifier.md; do
  scp "$REPO_DIR/$f" "${HOST}:~/.claude/$f"
done

# Ship the `seneschal-post` CLI helper to ~/bin on the host. This is the
# script the /seneschal-review Claude Code skill calls to post an
# aggregated multi-persona review as seneschal-cr[bot] via a minted
# installation token. Pure GitHub-API poster — no LLM dependency.
ssh "$HOST" "mkdir -p ~/bin"
scp "$REPO_DIR/bin/seneschal-post" "${HOST}:~/bin/seneschal-post"
ssh "$HOST" "chmod +x ~/bin/seneschal-post"

# Smoke-import so we catch missing deps before systemd starts.
# The review_index / cross_repo / dependency_grep trio is only imported
# by the MCP server today, but lives in the same ~/seneschal/ dir as the
# webhook code, so failing imports here catch broken deploys before they
# surface in Claude Code sessions.
ssh "$HOST" "cd ~/seneschal && ~/seneschal/venv/bin/python -c 'import analyzer; import backend; import diff_parser; import full_review; import seneschal_token; import review_index; import cross_repo; import dependency_grep; import license_check; import commit_convention; import branch_naming; from post_merge import orchestrator' && echo 'seneschal imports: OK'"

# Install / update the systemd unit
scp "$REPO_DIR/systemd/seneschal.service" "${HOST}:/tmp/seneschal.service"
ssh "$HOST" "
  sudo cp /tmp/seneschal.service /etc/systemd/system/seneschal.service
  sudo systemctl daemon-reload
  sudo systemctl enable seneschal.service
  sudo systemctl restart seneschal.service
  sleep 2
  sudo systemctl status seneschal.service --no-pager -n 10
"

echo "Seneschal deployed to ${HOST}."
echo "Webhook endpoint: http://${HOST}:9100/webhook/seneschal"
