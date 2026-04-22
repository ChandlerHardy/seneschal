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
ssh "$HOST" "mkdir -p ~/seneschal"

# Create venv if missing
ssh "$HOST" "
  if [ ! -d ~/seneschal/venv ]; then
    python3 -m venv ~/seneschal/venv
    echo 'created ~/seneschal/venv'
  fi
"

# Ship Python sources flat (matches the flat layout of this repo)
for f in app.py analyzer.py risk.py scope.py diff_parser.py test_gaps.py \
         related_prs.py repo_config.py review_memory.py context_loader.py \
         findings.py summary.py title_check.py breaking_changes.py \
         quality_scan.py secrets_scan.py full_review.py seneschal_token.py \
         backend.py \
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

# Smoke-import so we catch missing deps before systemd starts
ssh "$HOST" "cd ~/seneschal && ~/seneschal/venv/bin/python -c 'import analyzer; import backend; import diff_parser; import full_review; import seneschal_token' && echo 'seneschal imports: OK'"

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
