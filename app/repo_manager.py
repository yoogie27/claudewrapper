"""GitHub repository cloning and management."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def parse_repo_info(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub URL (HTTPS or SSH)."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    return None


def normalize_to_ssh_url(url: str) -> str:
    """Convert an HTTPS GitHub URL to SSH format. Pass through SSH URLs."""
    info = parse_repo_info(url)
    if not info:
        return url
    owner, repo = info
    return f"git@github.com:{owner}/{repo}.git"


def clone_or_fetch(
    url: str, repos_dir: Path, ssh_env: dict[str, str] | None = None,
) -> tuple[bool, str, Path | None]:
    """Clone a repo or fetch updates if it already exists.

    Returns (success, message, path).
    """
    info = parse_repo_info(url)
    if not info:
        return False, f"Cannot parse GitHub URL: {url}", None

    owner, repo = info
    target = repos_dir / owner / repo
    ssh_url = normalize_to_ssh_url(url)
    run_env = {**os.environ, **ssh_env} if ssh_env else None
    cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    if target.exists() and (target / ".git").exists():
        # Already cloned — fetch latest
        result = subprocess.run(
            ["git", "-C", str(target), "fetch", "--all"],
            capture_output=True, text=True, env=run_env, creationflags=cflags,
        )
        if result.returncode != 0:
            return False, f"git fetch failed: {result.stderr.strip()}", target
        # Pull on the current branch
        subprocess.run(
            ["git", "-C", str(target), "pull", "--ff-only"],
            capture_output=True, text=True, env=run_env, creationflags=cflags,
        )
        return True, "Repository updated", target

    # Fresh clone
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", ssh_url, str(target)],
        capture_output=True, text=True, env=run_env, creationflags=cflags,
        timeout=300,
    )
    if result.returncode != 0:
        return False, f"git clone failed: {result.stderr.strip()}", None

    return True, "Repository cloned successfully", target


def get_clone_status(repos_dir: Path, url: str) -> dict:
    """Check the clone status of a repo."""
    info = parse_repo_info(url)
    if not info:
        return {"status": "error", "message": f"Invalid URL: {url}", "path": ""}

    owner, repo = info
    target = repos_dir / owner / repo

    if not target.exists():
        return {"status": "not_cloned", "message": "Not yet cloned", "path": ""}

    if not (target / ".git").exists():
        return {"status": "error", "message": "Directory exists but is not a git repo", "path": str(target)}

    return {"status": "cloned", "message": "Repository available", "path": str(target)}
