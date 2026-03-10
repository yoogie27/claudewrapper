from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


class GitWorktreeError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
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


def ensure_worktree(repo: Path, worktree_root: Path, identifier: str, fresh: bool = False) -> Path:
    repo = repo.resolve()
    worktree_root = worktree_root.resolve()
    worktree_root.mkdir(parents=True, exist_ok=True)

    branch = f"ticket/{identifier}"
    worktree_path = worktree_root / identifier

    base = get_default_branch(repo)
    try:
        _run(["git", "-C", str(repo), "fetch", "origin", base])
        base_ref = f"origin/{base}"
    except Exception:
        base_ref = base

    if worktree_path.exists():
        if fresh:
            # Reset the worktree branch to latest main so the next job
            # starts clean (e.g. after the previous PR was merged).
            try:
                _run(["git", "-C", str(worktree_path), "checkout", branch])
                _run(["git", "-C", str(worktree_path), "reset", "--hard", base_ref])
            except Exception:
                pass  # If reset fails, the worktree is still usable
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


def push_branch(worktree: Path, branch: str) -> None:
    _run(["git", "-C", str(worktree), "push", "-u", "origin", branch])


def parse_github_remote(repo: Path) -> tuple[str, str] | None:
    """Returns (owner, repo_name) if remote is GitHub, else None."""
    try:
        url = _run(["git", "-C", str(repo), "remote", "get-url", "origin"])
    except Exception:
        return None
    m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)
    return None
