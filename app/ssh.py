"""SSH key management for GitHub authentication."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


class SSHError(RuntimeError):
    pass


def ensure_ssh_keypair(key_dir: Path) -> Path:
    """Generate an ed25519 SSH keypair if one doesn't already exist.

    Returns the path to the public key.
    """
    key_dir.mkdir(parents=True, exist_ok=True)
    private_key = key_dir / "id_ed25519"
    public_key = key_dir / "id_ed25519.pub"

    if private_key.exists() and public_key.exists():
        return public_key

    cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(private_key), "-C", "claudewrapper@docker"],
        capture_output=True, text=True, creationflags=cflags,
    )
    if result.returncode != 0:
        raise SSHError(f"ssh-keygen failed: {result.stderr.strip()}")

    # Ensure proper permissions on the private key
    if sys.platform != "win32":
        private_key.chmod(0o600)

    return public_key


def get_public_key(key_dir: Path) -> str | None:
    """Read and return the SSH public key, or None if not generated."""
    pub = key_dir / "id_ed25519.pub"
    if not pub.exists():
        return None
    return pub.read_text(encoding="utf-8").strip()


def ensure_ssh_config(key_dir: Path) -> None:
    """Write an SSH config file that auto-accepts github.com host keys."""
    config_path = key_dir / "config"
    private_key = key_dir / "id_ed25519"
    known_hosts = key_dir / "known_hosts"

    config_content = f"""\
Host github.com
    HostName github.com
    User git
    IdentityFile {private_key}
    StrictHostKeyChecking accept-new
    UserKnownHostsFile {known_hosts}
"""
    config_path.write_text(config_content, encoding="utf-8")

    if sys.platform != "win32":
        config_path.chmod(0o600)


def seed_known_hosts(key_dir: Path) -> None:
    """Pre-populate known_hosts with GitHub's SSH host keys via ssh-keyscan."""
    known_hosts = key_dir / "known_hosts"
    if known_hosts.exists() and known_hosts.stat().st_size > 0:
        return

    cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            ["ssh-keyscan", "-t", "ed25519,rsa", "github.com"],
            capture_output=True, text=True, timeout=15, creationflags=cflags,
        )
        if result.returncode == 0 and result.stdout.strip():
            known_hosts.write_text(result.stdout, encoding="utf-8")
    except Exception:
        # Fallback: touch the file so it exists (accept-new will populate it on first connect)
        if not known_hosts.exists():
            known_hosts.write_text("", encoding="utf-8")


def get_git_ssh_env(key_dir: Path) -> dict[str, str]:
    """Return environment dict to inject into git subprocess calls for SSH auth."""
    config_path = key_dir / "config"
    known_hosts = key_dir / "known_hosts"
    return {
        "GIT_SSH_COMMAND": f"ssh -F {config_path} -o UserKnownHostsFile={known_hosts} -o StrictHostKeyChecking=accept-new",
    }


def setup_ssh(key_dir: Path) -> None:
    """Full SSH setup: generate key, write config, seed known_hosts."""
    ensure_ssh_keypair(key_dir)
    ensure_ssh_config(key_dir)
    seed_known_hosts(key_dir)
