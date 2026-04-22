#!/usr/bin/env bash
# PRIVATE FORK deploy script. Ships backend_cli.py alongside the public
# files and installs the `seneschal.service.private` systemd unit, which
# sets SENESCHAL_BACKEND=cli so reviews route through `claude -p` against
# the operator's own Claude Max session on the target host.
#
# Usage:
#   ./install.sh [host]
#
# Default host is "oci" (an SSH config alias). The target box must have:
#   - Python 3.9+ installed
#   - The Claude CLI installed and authenticated (run `claude` interactively
#     once to auth; this fork's CliBackend shells out to `claude -p`)
#   - An SSH-accessible user with sudo for systemd management
#   - Port 9100 open (or whatever the Flask app binds to)
#
# Before running this, place on the target box:
#   ~/seneschal/ch-code-reviewer.pem     (GitHub App private key, chmod 600)
#   ~/seneschal/webhook-secret.txt       (GitHub App webhook secret, chmod 600)
#
# No ANTHROPIC_API_KEY is required in this mode — the CLI backend uses
# the already-authenticated Claude session on the host.

set -euo pipefail

HOST="${1:-oci}"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Deploying Seneschal to ${HOST}..."

# Target install dir on the host
ssh "$HOST" "mkdir -p ~/seneschal"

# Create venv if missing
ssh "$HOST" "
  if [ ! -d ~/seneschal/venv ]; then
    python3 -m venv ~/seneschal/venv
    echo 'created ~/seneschal/venv'
  fi
"

# Ship Python sources flat (matches the flat layout of this repo).
# `backend_cli.py` is private-fork-only; it's the CLI-backed `Backend`
# impl the patched `backend.py` factory selects when SENESCHAL_BACKEND=cli.
for f in app.py analyzer.py risk.py scope.py diff_parser.py test_gaps.py \
         related_prs.py repo_config.py review_memory.py context_loader.py \
         findings.py summary.py title_check.py breaking_changes.py \
         quality_scan.py secrets_scan.py full_review.py seneschal_token.py \
         backend.py backend_cli.py \
         __init__.py requirements.txt; do
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
  scp "$REPO_DIR/$f" "${HOST}:~/.claude/$(echo "$f" | sed 's#^#agents/#' 2>/dev/null || echo "$f")"
done

# Smoke-import so we catch missing deps before systemd starts. Set
# SENESCHAL_BACKEND=cli for the import check too — otherwise `backend.py`
# tries to construct ApiBackend, which requires ANTHROPIC_API_KEY.
ssh "$HOST" "cd ~/seneschal && SENESCHAL_BACKEND=cli ~/seneschal/venv/bin/python -c 'import analyzer; import backend; import backend_cli; import diff_parser; import full_review; import seneschal_token; backend.get_backend()' && echo 'seneschal imports: OK (cli backend)'"

# Install / update the systemd unit — PRIVATE FORK uses the .private unit
# file, which sets SENESCHAL_BACKEND=cli.
scp "$REPO_DIR/systemd/seneschal.service.private" "${HOST}:/tmp/seneschal.service"
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
