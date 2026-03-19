#!/bin/bash
set -e

SSH_KEY_DIR="${SSH_KEY_DIR:-/data/ssh}"
mkdir -p "$SSH_KEY_DIR"

# ── SSH Setup ──────────────────────────────────────────────
if [ ! -f "$SSH_KEY_DIR/id_ed25519" ]; then
    echo "==> Generating SSH ed25519 keypair..."
    ssh-keygen -t ed25519 -N "" -f "$SSH_KEY_DIR/id_ed25519" -C "claudewrapper@docker"
    chmod 600 "$SSH_KEY_DIR/id_ed25519"
    echo ""
    echo "==> SSH public key (add this as a deploy key in your GitHub repos):"
    echo "────────────────────────────────────────────────────────"
    cat "$SSH_KEY_DIR/id_ed25519.pub"
    echo "────────────────────────────────────────────────────────"
    echo ""
fi

# SSH config for GitHub (auto-accept new host keys)
SSH_CONFIG="$SSH_KEY_DIR/config"
if [ ! -f "$SSH_CONFIG" ]; then
    cat > "$SSH_CONFIG" <<EOF
Host github.com
    HostName github.com
    User git
    IdentityFile $SSH_KEY_DIR/id_ed25519
    StrictHostKeyChecking accept-new
    UserKnownHostsFile $SSH_KEY_DIR/known_hosts
EOF
    chmod 600 "$SSH_CONFIG"
fi

# Pre-seed GitHub known_hosts
if [ ! -f "$SSH_KEY_DIR/known_hosts" ] || [ ! -s "$SSH_KEY_DIR/known_hosts" ]; then
    echo "==> Fetching GitHub SSH host keys..."
    ssh-keyscan -t ed25519,rsa github.com > "$SSH_KEY_DIR/known_hosts" 2>/dev/null || true
fi

# Export GIT_SSH_COMMAND globally so all git operations use our SSH config
export GIT_SSH_COMMAND="ssh -F $SSH_KEY_DIR/config -o UserKnownHostsFile=$SSH_KEY_DIR/known_hosts -o StrictHostKeyChecking=accept-new"

# ── Claude Code ────────────────────────────────────────────
echo "==> Checking Claude Code..."
npm update -g @anthropic-ai/claude-code 2>/dev/null || true

# ── Version Info ───────────────────────────────────────────
echo "==> Claude Code: $(claude --version 2>/dev/null || echo 'not installed')"
echo "==> Git:         $(git --version)"
echo "==> Node:        $(node --version)"
echo "==> Python:      $(python --version)"
echo ""

# ── Start Application ─────────────────────────────────────
exec "$@"
