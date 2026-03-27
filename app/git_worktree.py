from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


class GitWorktreeError(RuntimeError):
    pass


class GitPushError(GitWorktreeError):
    """Push failure with a classified error type for actionable diagnostics."""

    def __init__(self, message: str, error_type: str):
        super().__init__(message)
        self.error_type = error_type  # host_key, auth, branch_exists, network, unknown


def _classify_push_error(stderr: str) -> str:
    """Classify a git push/fetch error for actionable logging."""
    s = stderr.lower()
    if "host key verification failed" in s or "known_hosts" in s or "authenticity of host" in s:
        return "host_key"
    if "permission denied" in s or "publickey" in s or "could not read from remote" in s:
        return "auth"
    if "non-fast-forward" in s or "[rejected]" in s or "already exists" in s:
        return "branch_exists"
    if "unable to access" in s or "could not resolve" in s or "connection refused" in s:
        return "network"
    return "unknown"


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    run_env = {**os.environ, **env} if env else None
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, env=run_env)
    if result.returncode != 0:
        raise GitWorktreeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return (result.stdout or "").strip()


def get_default_branch(repo: Path) -> str:
    try:
        ref = _run(["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD"])
        return ref.split("/")[-1]
    except Exception:
        pass
    for candidate in ["main", "master"]:
        try:
            _run(["git", "-C", str(repo), "rev-parse", "--verify", candidate])
            return candidate
        except Exception:
            continue
    return "main"


def ensure_worktree(repo: Path, worktree_root: Path, identifier: str, env: dict[str, str] | None = None, base_branch: str = "") -> Path:
    repo = repo.resolve()
    worktree_root = worktree_root.resolve()
    worktree_root.mkdir(parents=True, exist_ok=True)

    branch = f"ticket/{identifier}"
    worktree_path = worktree_root / identifier

    base = base_branch if base_branch else get_default_branch(repo)
    try:
        _run(["git", "-C", str(repo), "fetch", "origin", base], env=env)
        base_ref = f"origin/{base}"
    except Exception:
        # If the specified base_branch doesn't exist, fall back to default
        if base_branch:
            base = get_default_branch(repo)
            try:
                _run(["git", "-C", str(repo), "fetch", "origin", base], env=env)
                base_ref = f"origin/{base}"
            except Exception:
                base_ref = base
        else:
            base_ref = base

    if worktree_path.exists():
        # Always reset to latest main.  With concurrency=1 per project
        # there is no in-progress work to preserve, and starting from a
        # stale base causes merge-conflict failures on the resulting PR.
        try:
            _run(["git", "-C", str(worktree_path), "checkout", branch])
            _run(["git", "-C", str(worktree_path), "reset", "--hard", base_ref])
            _run(["git", "-C", str(worktree_path), "clean", "-fd"])
        except Exception:
            # Reset failed — tear down and recreate below
            try:
                _run(["git", "-C", str(repo), "worktree", "remove", "-f", str(worktree_path)])
            except Exception:
                import shutil
                shutil.rmtree(worktree_path, ignore_errors=True)
                try:
                    _run(["git", "-C", str(repo), "worktree", "prune"])
                except Exception:
                    pass
        else:
            return worktree_path

    # Prune stale worktree records (e.g. after manual cleanup or disk wipe)
    # so git doesn't refuse to reuse the branch name.
    try:
        _run(["git", "-C", str(repo), "worktree", "prune"])
    except Exception:
        pass

    try:
        _run(["git", "-C", str(repo), "worktree", "add", "-B", branch, str(worktree_path), base_ref])
    except GitWorktreeError:
        # Branch may be checked out in the main repo itself.
        # Switch the main repo back to the default branch, then retry.
        try:
            _run(["git", "-C", str(repo), "checkout", base])
        except Exception:
            pass
        try:
            _run(["git", "-C", str(repo), "branch", "-D", branch])
        except Exception:
            pass
        _run(["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree_path), base_ref])
    return worktree_path


def remove_worktree(repo: Path, worktree_path: Path) -> None:
    if not worktree_path.exists():
        return
    try:
        _run(["git", "-C", str(repo), "worktree", "remove", "-f", str(worktree_path)])
    except Exception:
        pass


def write_worktree_meta(session_dir: Path, repo: Path, worktree: Path) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    meta = {"repo": str(repo), "worktree": str(worktree)}
    (session_dir / "worktree.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def read_worktree_meta(session_dir: Path) -> dict | None:
    path = session_dir / "worktree.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_commit_count_vs_base(worktree: Path, base_ref: str) -> int:
    try:
        out = _run(["git", "-C", str(worktree), "rev-list", "--count", f"{base_ref}..HEAD"])
        return int(out.strip())
    except Exception:
        return 0


def push_branch(worktree: Path, branch: str, env: dict[str, str] | None = None) -> None:
    try:
        _run(["git", "-C", str(worktree), "push", "-u", "origin", branch], env=env)
    except GitWorktreeError as exc:
        error_type = _classify_push_error(str(exc))
        if error_type == "branch_exists":
            # Safe for ticket branches: force-with-lease to update
            try:
                _run(["git", "-C", str(worktree), "push", "-u", "--force-with-lease", "origin", branch], env=env)
                return
            except GitWorktreeError as retry_exc:
                raise GitPushError(str(retry_exc), _classify_push_error(str(retry_exc))) from retry_exc
        raise GitPushError(str(exc), error_type) from exc


def parse_github_remote(repo: Path) -> tuple[str, str] | None:
    """Returns (owner, repo_name) if remote is GitHub, else None."""
    try:
        url = _run(["git", "-C", str(repo), "remote", "get-url", "origin"])
    except Exception:
        return None
    return parse_github_url(url)


def parse_github_url(url: str) -> tuple[str, str] | None:
    """Parse (owner, repo_name) from a GitHub URL string. Works without filesystem."""
    if not url:
        return None
    m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url.strip())
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url.strip())
    if m:
        return m.group(1), m.group(2)
    return None
