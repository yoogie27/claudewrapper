from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.claude_runner import ClaudeRunner
from app.config import Settings
from app.db import Database, utc_now
from app.task_modes import get_mode_prompt
from app.utils import setup_logger
from app.git_worktree import (
    GitPushError,
    ensure_worktree,
    get_commit_count_vs_base,
    get_default_branch,
    parse_github_remote,
    push_branch,
    read_worktree_meta,
    remove_worktree,
    write_worktree_meta,
)
from app.ssh import get_git_ssh_env


DEFAULT_PROMPT = """\
You are an autonomous senior developer working in this repository.

## How you work

1. **Understand first**: Read the task description and ALL conversation messages carefully. \
The latest message is the actual instruction — prior messages provide context.
2. **Research the codebase**: Use LSP tools (find-definition, find-references) to understand \
the architecture before changing anything.
3. **Check history**: Use `git log` to understand recent changes and patterns.
4. **Follow existing patterns**: Match the coding style, conventions, and architecture already in the repo.
5. **Test your changes**: Run the project's tests after implementation.
6. **Stay focused**: Only change what the task requires. Don't refactor unrelated code.\
"""


class Orchestrator:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.runner = ClaudeRunner(
            settings.claude_command_template,
            settings.claude_prompt_via,
            settings.claude_prompt_arg,
            settings.claude_workdir_mode,
        )
        log_path = str(settings.data_path() / "logs" / "app.log")
        self.logger = setup_logger(log_path)
        self.stop_event = asyncio.Event()
        # run_id -> {"proc": Popen}
        self._proc_holders: dict[str, dict] = {}
        # project_id -> asyncio.Task (one worker per active project)
        self._project_workers: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        self.settings.ensure_dirs()
        asyncio.create_task(self._reaper_loop())
        asyncio.create_task(self._cleanup_loop())
        # Restart workers for projects with pending runs
        for project in self.db.list_projects():
            pending = self.db.get_pending_run(project["id"])
            if pending:
                self._ensure_worker(project["id"])

    def _ensure_worker(self, project_id: str) -> None:
        """Start a worker for a project if one isn't already running."""
        existing = self._project_workers.get(project_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._worker_loop(project_id))
        self._project_workers[project_id] = task

    def _get_git_ssh_env(self) -> dict[str, str] | None:
        key_dir = self.settings.ssh_key_path()
        if (key_dir / "id_ed25519").exists():
            return get_git_ssh_env(key_dir)
        return None

    # ── Worker Loop ──

    async def _worker_loop(self, project_id: str) -> None:
        """Process runs for a single project, sequentially."""
        idle_cycles = 0
        while not self.stop_event.is_set():
            # Check global pause
            if self.db.get_config("queue_paused") == "1":
                await asyncio.sleep(3)
                continue

            run = self.db.get_pending_run(project_id)
            if not run:
                idle_cycles += 1
                if idle_cycles > 30:  # ~60s idle, stop worker
                    break
                await asyncio.sleep(2)
                continue

            idle_cycles = 0
            await self._execute_run(run)

    async def _execute_run(self, run: dict) -> None:
        run_id = run["id"]
        task = self.db.get_task(run["task_id"])
        if not task:
            self.db.update_run(run_id, status="failed")
            return

        project = self.db.get_project(task["project_id"])
        if not project:
            self.db.update_run(run_id, status="failed")
            return

        identifier = task["identifier"]
        self.logger.info("[%s] Starting run %s", identifier, run_id)

        # Create session directory
        session_dir = self.settings.data_path() / "sessions" / identifier / run_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # Mark run as running
        now = utc_now()
        self.db.update_run(run_id, status="running", started_at=now, session_dir=str(session_dir))
        self.db.update_task(task["id"], status="in_progress")

        # Consolidate: cancel other pending runs for the same task.
        # Their messages are already in the DB and will be included in this run's prompt.
        consolidated = self.db.consolidate_pending_runs(task["id"], run_id)
        if consolidated:
            self.logger.info("[%s] Consolidated %d queued runs into run %s", identifier, consolidated, run_id)

        workdir = self.settings.project_repo_path(project["slug"])

        # Setup git worktree
        if self.settings.use_git_worktrees and workdir and workdir.exists():
            try:
                ssh_env = self._get_git_ssh_env()
                wt_root = self.settings.worktree_path()
                base_branch = project.get("base_branch", "") or ""
                worktree_path = await asyncio.to_thread(
                    ensure_worktree, workdir, wt_root, identifier, ssh_env, base_branch
                )
                write_worktree_meta(session_dir, workdir, worktree_path)
                self.db.update_task(task["id"], worktree_path=str(worktree_path))
                workdir = worktree_path
            except Exception as exc:
                error_str = str(exc)
                self.logger.error("[%s] Worktree setup failed: %s", identifier, error_str)

                # Classify the error for a helpful message
                hint = ""
                low = error_str.lower()
                if "permission denied" in low or "publickey" in low:
                    hint = "\n\n**Likely cause:** SSH authentication failed. If using HTTPS, check your git credentials. If using SSH, check your SSH key configuration in Settings."
                elif "could not resolve" in low or "connection refused" in low:
                    hint = "\n\n**Likely cause:** Network error — cannot reach the remote. Check your internet connection."
                elif "not a git repository" in low:
                    hint = "\n\n**Likely cause:** The project path is not a valid git repository. Check the path in project settings."
                elif "does not exist" in low or "not found" in low:
                    hint = f"\n\n**Likely cause:** The branch or ref could not be found. Check the base branch setting (currently: `{project.get('base_branch', 'main')}`)."
                else:
                    hint = "\n\nCheck the Settings page > Project Repo Checks for detailed diagnostics."

                # Store error as assistant message so user sees it in chat
                error_msg = f"**Git setup failed:**\n\n`{error_str}`{hint}"
                err_msg_id = uuid.uuid4().hex
                self.db.create_message(err_msg_id, task["id"], "assistant", error_msg, run_id=run_id)

                self.db.update_run(run_id, status="failed", ended_at=utc_now(), exit_code=-1)
                self.db.update_task(task["id"], status="failed")
                return

        # Build prompt
        messages = self.db.list_messages(task["id"])
        prompt = self._build_prompt(task, project, messages)
        self.db.update_run(run_id, prompt=prompt)

        # Determine if we should resume a prior Claude session
        resume_session_id = task.get("claude_session_id")

        # Run Claude
        if self.settings.test_mode:
            self.logger.info("[%s] TEST MODE — skipping Claude", identifier)
            # Write a fake result for test mode
            (session_dir / "stdout.txt").write_text(
                json.dumps({"type": "result", "result": "Test mode: no Claude run.", "total_cost_usd": 0}) + "\n",
                encoding="utf-8",
            )
            self.db.update_run(run_id, status="done", ended_at=utc_now(), exit_code=0)
            assistant_msg_id = uuid.uuid4().hex
            self.db.create_message(assistant_msg_id, task["id"], "assistant",
                                   "Test mode: no Claude run.", run_id=run_id)
            task_status = "in_progress" if self.db.has_pending_runs(task["id"]) else "done"
            self.db.update_task(task["id"], status=task_status)
            return

        # Apply model selection from settings
        self.runner.model = self.db.get_config("claude_model", "") or ""
        self.runner.fallback_model = self.db.get_config("claude_fallback_model", "") or ""

        proc_holder: dict = {}
        self._proc_holders[run_id] = proc_holder
        ssh_env = self._get_git_ssh_env()

        try:
            result = await asyncio.to_thread(
                self.runner.run, identifier, prompt, session_dir, workdir,
                proc_holder, resume_session_id, ssh_env,
            )
        except Exception as exc:
            self.logger.error("[%s] Claude runner error: %s", identifier, exc)
            err_msg_id = uuid.uuid4().hex
            self.db.create_message(err_msg_id, task["id"], "assistant",
                                   f"**Claude failed to start:**\n\n`{exc}`\n\nCheck that Claude CLI is installed and accessible.",
                                   run_id=run_id)
            self.db.update_run(run_id, status="failed", ended_at=utc_now(), exit_code=-1)
            self.db.update_task(task["id"], status="failed")
            return
        finally:
            self._proc_holders.pop(run_id, None)

        # Parse result
        exit_code = result.returncode
        self.logger.info("[%s] Claude exited with code %d", identifier, exit_code)

        # Extract session ID for future --resume
        new_session_id = getattr(result, "claude_session_id", None)
        if new_session_id:
            self.db.update_task(task["id"], claude_session_id=new_session_id)

        # Parse output
        summary, cost_usd, input_tokens, output_tokens, model = self._parse_result(result.stdout)

        # Store assistant message
        assistant_msg_id = uuid.uuid4().hex
        self.db.create_message(
            assistant_msg_id, task["id"], "assistant", summary, run_id=run_id,
            metadata={"cost_usd": cost_usd, "input_tokens": input_tokens, "output_tokens": output_tokens},
        )

        # Update run
        status = "done" if exit_code == 0 else "failed"
        self.db.update_run(
            run_id, status=status, ended_at=utc_now(), exit_code=exit_code,
            cost_usd=cost_usd, input_tokens=input_tokens, output_tokens=output_tokens, model=model,
            claude_session_id=new_session_id or "",
        )

        # Update task status — but don't set "done" if more runs are pending
        if exit_code == 0:
            if self.db.has_pending_runs(task["id"]):
                task_status = "in_progress"
            else:
                task_status = "done"
        else:
            task_status = "failed"
        self.db.update_task(task["id"], status=task_status)

        # Try to create PR if there are commits
        if exit_code == 0 and self.settings.github_token:
            pr_url, pr_error = await self._maybe_create_pr(task, session_dir, identifier,
                                                            base_branch=project.get("base_branch", ""))
            if pr_url:
                self.db.update_task(task["id"], pr_url=pr_url, status="in_review")
                pr_msg_id = uuid.uuid4().hex
                self.db.create_message(pr_msg_id, task["id"], "system",
                                       f"Pull request created: [{pr_url}]({pr_url})")

                # Auto-merge the PR
                merged = await self._merge_github_pr(pr_url)
                merge_msg_id = uuid.uuid4().hex
                if merged:
                    self.db.update_task(task["id"], pr_merged=1, status="done")
                    self.db.create_message(merge_msg_id, task["id"], "system",
                                           f"PR merged and closed successfully.")
                    self.logger.info("[%s] PR auto-merged: %s", identifier, pr_url)
                else:
                    self.db.create_message(merge_msg_id, task["id"], "system",
                                           f"**PR could not be auto-merged.** Check for merge conflicts or required status checks on [{pr_url}]({pr_url}).")
                    self.logger.warning("[%s] PR auto-merge failed: %s", identifier, pr_url)
            elif pr_error:
                # Surface the PR failure in chat so the user sees it
                err_msg_id = uuid.uuid4().hex
                self.db.create_message(err_msg_id, task["id"], "system", pr_error)

        self.logger.info("[%s] Run %s complete (status=%s, cost=$%.4f)",
                         identifier, run_id, status, cost_usd)

    # ── Prompt Building ──

    def _build_prompt(self, task: dict, project: dict, messages: list[dict]) -> str:
        mode_prompt = get_mode_prompt(task["mode"], db=self.db)
        project_prompt = project.get("default_prompt", "").strip() or DEFAULT_PROMPT

        parts = [
            mode_prompt,
            "",
            project_prompt,
            "",
            "---",
            "",
            f"## Task: {task['identifier']} — {task['title']}",
            f"**Mode:** {task['mode'].title()}",
        ]

        if task.get("description"):
            parts.extend(["", "## Description", task["description"]])

        # Conversation history (limit to last 20 messages to avoid prompt bloat)
        if messages:
            recent = messages[-20:] if len(messages) > 20 else messages
            parts.extend(["", "---", "", "## Conversation History", ""])
            if len(messages) > 20:
                parts.append(f"*({len(messages) - 20} earlier messages omitted)*\n")
            for msg in recent:
                role_label = "User" if msg["role"] == "user" else "Assistant"
                # Truncate very long assistant messages in history
                content = msg["content"]
                if msg["role"] == "assistant" and len(content) > 2000:
                    content = content[:2000] + "\n\n[...truncated]"
                parts.append(f"**[{role_label}]:**")
                parts.append(content)
                parts.append("")

        # Git context
        if task.get("branch_name"):
            parts.extend([
                "---",
                "",
                "## Git Context",
                f"- Branch: `{task['branch_name']}`",
                f"- Base: `{project.get('base_branch', 'main')}`",
            ])

        # Completion instructions
        prefix = "fix" if task["mode"] == "bug" else "feat" if task["mode"] == "feature" else "refactor"
        parts.extend([
            "",
            "## Completion",
            "",
            "After implementation and testing:",
            f"1. Commit with: `{prefix}: {task['title']} ({task['identifier']})`",
            "2. Write a brief summary of what you changed and why.",
        ])

        return "\n".join(parts)

    # ── Result Parsing ──

    def _parse_result(self, stdout: str) -> tuple[str, float, int, int, str]:
        """Parse stream-json output. Returns (summary, cost_usd, input_tokens, output_tokens, model)."""
        text = (stdout or "").strip()
        if not text:
            return "No output captured.", 0.0, 0, 0, ""

        cost_usd = 0.0
        input_tokens = 0
        output_tokens = 0
        model = ""

        lines = text.splitlines()

        # Find result event (has cost/token data)
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("type") == "result":
                    cost_usd = obj.get("total_cost_usd", 0.0) or 0.0
                    usage = obj.get("usage", {}) or {}
                    input_tokens = usage.get("input_tokens", 0) or 0
                    output_tokens = usage.get("output_tokens", 0) or 0
                    model = obj.get("model", "") or ""

                    raw = obj.get("result", "")
                    if isinstance(raw, list):
                        summary = "".join(
                            c.get("text", "") for c in raw if c.get("type") == "text"
                        ).strip()
                    else:
                        summary = str(raw).strip()
                    if summary:
                        return summary, cost_usd, input_tokens, output_tokens, model
                    break
            except (json.JSONDecodeError, ValueError):
                continue

        # Fallback: collect assistant text blocks
        assistant_text = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("type") == "assistant":
                    content = (obj.get("message") or {}).get("content", [])
                    for block in (content if isinstance(content, list) else []):
                        if block.get("type") == "text":
                            assistant_text.append(block["text"])
            except (json.JSONDecodeError, ValueError):
                continue

        if assistant_text:
            combined = "\n".join(assistant_text).strip()
            if combined:
                return combined[:5000], cost_usd, input_tokens, output_tokens, model

        return text[:5000], cost_usd, input_tokens, output_tokens, model

    # ── Job Control ──

    def cancel_run(self, run_id: str) -> bool:
        holder = self._proc_holders.get(run_id)
        if holder and "proc" in holder:
            try:
                holder["proc"].terminate()
                self.db.update_run(run_id, status="failed", ended_at=utc_now(), exit_code=-1)
                # Also update the task
                run = self.db.get_run(run_id)
                if run:
                    self.db.update_task(run["task_id"], status="failed")
                return True
            except Exception as exc:
                self.logger.warning("Failed to terminate run %s: %s", run_id, exc)
        return False

    async def enqueue_message(self, task_id: str, content: str) -> dict:
        """Create a user message and enqueue a Claude run for it."""
        task = self.db.get_task(task_id)
        if not task:
            raise ValueError("Task not found")

        # Re-open completed/failed tasks so they can continue
        if task["status"] in ("done", "failed"):
            self.db.update_task(task_id, status="open")

        # Store user message
        msg_id = uuid.uuid4().hex
        self.db.create_message(msg_id, task_id, "user", content)

        # Create pending run
        run_id = uuid.uuid4().hex
        run = self.db.create_run(run_id, task_id)

        # Ensure worker is running for this project
        self._ensure_worker(task["project_id"])

        return run

    # ── PR Management ──

    async def _maybe_create_pr(self, task: dict, session_dir: Path, identifier: str,
                                base_branch: str = "") -> tuple[str | None, str | None]:
        """Try to push and create a PR. Returns (pr_url, error_message)."""
        if not self.settings.github_token:
            return None, None
        meta = read_worktree_meta(session_dir)
        if not meta:
            return None, None

        repo = Path(meta["repo"])
        worktree = Path(meta["worktree"])
        branch = f"ticket/{identifier}"

        base = base_branch if base_branch else get_default_branch(repo)
        try:
            base_ref = f"origin/{base}"
            count = await asyncio.to_thread(get_commit_count_vs_base, worktree, base_ref)
        except Exception:
            count = 0

        if count == 0:
            return None, None

        github_info = parse_github_remote(repo)
        if not github_info:
            return None, None
        owner, repo_name = github_info

        ssh_env = self._get_git_ssh_env()
        try:
            await asyncio.to_thread(push_branch, worktree, branch, ssh_env)
        except GitPushError as exc:
            error_hints = {
                "auth": "SSH authentication failed. Check your deploy key or switch to HTTPS.",
                "host_key": "SSH host key verification failed. Check known_hosts.",
                "network": "Cannot reach remote. Check network connectivity.",
            }
            hint = error_hints.get(exc.error_type, "")
            msg = f"Git push failed: {exc}" + (f"\n\n{hint}" if hint else "")
            self.logger.error("[%s] Git push FAILED (%s): %s", identifier, exc.error_type, exc)
            return None, msg
        except Exception as exc:
            self.logger.error("[%s] Git push FAILED: %s", identifier, exc)
            return None, f"Git push failed: {exc}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                headers = {
                    "Authorization": f"Bearer {self.settings.github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
                # Check for existing PR
                existing_resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo_name}/pulls",
                    headers=headers,
                    params={"head": f"{owner}:{branch}", "state": "open"},
                )
                if existing_resp.status_code == 200:
                    existing_prs = existing_resp.json()
                    if existing_prs:
                        return existing_prs[0].get("html_url"), None

                resp = await client.post(
                    f"https://api.github.com/repos/{owner}/{repo_name}/pulls",
                    headers=headers,
                    json={
                        "title": f"{identifier}: {task['title']}",
                        "body": f"Task: {identifier}\nMode: {task['mode']}\n\nAutomatically generated by ClaudeWrapper.",
                        "head": branch,
                        "base": base,
                    },
                )
                if resp.status_code in (200, 201):
                    return resp.json().get("html_url"), None

                # Build a helpful error message based on status code
                status = resp.status_code
                body = resp.text[:300]
                self.logger.warning("PR creation failed: %s %s", status, body)

                if status == 403:
                    gh_msg = "**PR creation failed (403 Forbidden)**\n\n"
                    if "personal access token" in body.lower():
                        gh_msg += "Your GitHub token doesn't have permission to create PRs on this repo.\n\n"
                        gh_msg += "**How to fix:**\n"
                        gh_msg += "- **Fine-grained token:** Enable *Pull requests: Read and write* permission\n"
                        gh_msg += "- **Classic token:** Enable the `repo` scope\n"
                        gh_msg += "- **Org with SSO:** Authorize the token for your organization (token settings > Configure SSO)\n\n"
                        gh_msg += "Go to Settings > GitHub Integration for diagnostics."
                    else:
                        gh_msg += f"`{body[:200]}`"
                    return None, gh_msg
                elif status == 404:
                    return None, ("**PR creation failed (404 Not Found)**\n\n"
                                  "The repo was not found via the GitHub API. This usually means:\n"
                                  "- The token doesn't have access to this repo\n"
                                  "- For org repos with SSO: authorize the token for the organization\n\n"
                                  "Go to Settings > GitHub Integration for diagnostics.")
                elif status == 422:
                    return None, f"**PR creation failed (422)**\n\nGitHub rejected the PR: `{body[:200]}`"
                else:
                    return None, f"**PR creation failed ({status})**\n\n`{body[:200]}`"
        except Exception as exc:
            self.logger.warning("PR creation error: %s", exc)
            return None, f"**PR creation failed**\n\n`{exc}`"

    async def _merge_github_pr(self, pr_url: str) -> bool:
        if not self.settings.github_token:
            return False
        m = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', pr_url)
        if not m:
            return False
        owner, repo, pr_number = m.group(1), m.group(2), m.group(3)
        headers = {
            "Authorization": f"Bearer {self.settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        pr_api = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Wait for mergeability
                poll_delays = [3, 5, 10, 15, 20, 30]
                mergeable = None
                for i, delay in enumerate(poll_delays):
                    pr_resp = await client.get(pr_api, headers=headers)
                    if pr_resp.status_code == 200:
                        pr_data = pr_resp.json()
                        mergeable = pr_data.get("mergeable")
                        if mergeable is not None:
                            break
                    await asyncio.sleep(delay)

                if mergeable is False:
                    return False

                # Attempt merge
                merge_delays = [5, 15, 30]
                for attempt in range(len(merge_delays) + 1):
                    resp = await client.put(
                        f"{pr_api}/merge", headers=headers,
                        json={"merge_method": "squash"},
                    )
                    if resp.status_code == 200:
                        return True
                    if resp.status_code == 405 and attempt < len(merge_delays):
                        await asyncio.sleep(merge_delays[attempt])
                        continue
                    return False
        except Exception as exc:
            self.logger.warning("Merge error: %s", exc)
        return False

    # ── Maintenance Loops ──

    async def _reaper_loop(self) -> None:
        """Reset runs stuck in 'running' (crash recovery)."""
        while not self.stop_event.is_set():
            await asyncio.sleep(300)  # every 5 min
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.settings.stale_job_timeout_minutes)
                count = self.db.requeue_stale_runs(cutoff.isoformat())
                if count:
                    self.logger.info("Reaper: requeued %d stale runs", count)
                self.db.wal_checkpoint()
            except Exception as exc:
                self.logger.error("Reaper error: %s", exc)

    async def _cleanup_loop(self) -> None:
        """Clean up old session files."""
        while not self.stop_event.is_set():
            await asyncio.sleep(3600)  # hourly
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.session_ttl_days)
                entries = self.db.cleanup_old_runs(cutoff.isoformat())
                for entry in entries:
                    d = entry.get("session_dir")
                    if d:
                        meta = read_worktree_meta(Path(d))
                        if meta and meta.get("repo") and meta.get("worktree"):
                            try:
                                remove_worktree(Path(meta["repo"]), Path(meta["worktree"]))
                            except Exception:
                                pass
                    identifier_dir = self.settings.data_path() / "sessions" / entry["identifier"]
                    try:
                        shutil.rmtree(str(identifier_dir), ignore_errors=True)
                    except Exception:
                        pass
                if entries:
                    self.logger.info("Cleaned %d old runs", len(entries))
            except Exception as exc:
                self.logger.error("Cleanup error: %s", exc)

    # ── Task Cleanup ──

    def cleanup_task(self, task_id: str) -> tuple[bool, str]:
        """Remove worktree, session files, and runs for a task."""
        task = self.db.get_task(task_id)
        if not task:
            return False, "Task not found"

        identifier = task["identifier"]
        removed = []

        # Remove worktree
        if task.get("worktree_path"):
            wt_path = Path(task["worktree_path"])
            if wt_path.exists():
                try:
                    shutil.rmtree(str(wt_path), ignore_errors=True)
                    removed.append("worktree")
                except Exception:
                    pass

        # Remove session files
        identifier_dir = self.settings.data_path() / "sessions" / identifier
        if identifier_dir.exists():
            try:
                shutil.rmtree(str(identifier_dir), ignore_errors=True)
                removed.append("session files")
            except Exception:
                pass

        # Clean runs and messages from DB
        runs = self.db.list_runs(task_id)
        if runs:
            for r in runs:
                self.db.update_run(r["id"], status="failed", ended_at=utc_now())
            removed.append("runs")

        msg = f"Cleaned: {', '.join(removed)}" if removed else "Nothing to remove"
        return True, msg
