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

from app.cli_backend import CliBackend, ClaudeBackend, create_backend, BACKENDS
from app.config import Settings
from app.db import Database, utc_now
from app.sanitize import fence_user_content, sanitize_for_prompt
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
6. **Stay focused**: Only change what the task requires. Don't refactor unrelated code.

## Important constraints

- You are running headless in a CI-like environment. There is NO browser, NO GUI, NO user interaction.
- NEVER suggest opening a browser, showing a preview, or any visual demonstration.
- NEVER ask the user to do something manually. Complete the task autonomously.
- All output must be text-based. Do not use tools that require a display.\
"""


class Orchestrator:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        log_path = str(settings.data_path() / "logs" / "app.log")
        self.logger = setup_logger(log_path)
        self.stop_event = asyncio.Event()
        self._proc_holders: dict[str, dict] = {}
        self._project_workers: dict[str, asyncio.Task] = {}

    def _get_backend(self, backend_name: str) -> CliBackend:
        """Create a CLI backend for a given task's backend preference."""
        custom_cmd = self.db.get_config(f"backend_cmd:{backend_name}", "") or ""
        if backend_name == "claude":
            backend = create_backend(
                "claude", command_template=custom_cmd or self.settings.claude_command_template,
                prompt_via=self.settings.claude_prompt_via, prompt_arg=self.settings.claude_prompt_arg,
            )
        else:
            backend = create_backend(backend_name, command_template=custom_cmd)
        backend.model = self.db.get_config(f"{backend_name}_model", "") or self.db.get_config("claude_model", "") or ""
        backend.fallback_model = self.db.get_config(f"{backend_name}_fallback_model", "") or self.db.get_config("claude_fallback_model", "") or ""
        # Load persistent environment variables for this backend
        env_json = self.db.get_config(f"backend_env:{backend_name}", "") or ""
        if env_json:
            try:
                backend._extra_env = json.loads(env_json)
            except (json.JSONDecodeError, ValueError):
                backend._extra_env = {}
        else:
            backend._extra_env = {}
        return backend

    async def start(self) -> None:
        self.settings.ensure_dirs()
        # On startup, fail any runs stuck in 'running' from a prior crash/restart
        orphaned_tasks = self.db.fail_orphaned_runs()
        if orphaned_tasks:
            self.logger.info("Startup: marked %d orphaned running runs as failed", len(orphaned_tasks))
            for task_id in orphaned_tasks:
                try:
                    msg_id = uuid.uuid4().hex
                    self.db.create_message(msg_id, task_id, "system",
                                           "**Run interrupted:** Server was restarted while this task was running. "
                                           "Send a message to retry.")
                except Exception:
                    pass
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

    def _get_github_token(self, project: dict) -> str:
        """Return the effective GitHub token: project-specific first, then global."""
        return project.get("github_token", "").strip() or self.settings.github_token

    # ── Worker Loop ──

    async def _worker_loop(self, project_id: str) -> None:
        """Process runs for a single project, sequentially."""
        idle_cycles = 0
        while not self.stop_event.is_set():
            # Check global pause
            if self.db.get_config("queue_paused") == "1":
                idle_cycles = 0  # Don't count paused time toward idle timeout
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
            try:
                await self._execute_run(run)
            except Exception as exc:
                self.logger.error("Worker error for run %s: %s", run["id"], exc, exc_info=True)
                try:
                    self.db.update_run(run["id"], status="failed", ended_at=utc_now(), exit_code=-1)
                    task = self.db.get_task(run["task_id"])
                    if task:
                        # Surface error in chat so user sees it
                        err_msg_id = uuid.uuid4().hex
                        self.db.create_message(err_msg_id, task["id"], "system",
                                               f"**Run failed unexpectedly:**\n\n`{exc}`")
                        if not self.db.has_pending_runs(run["task_id"]):
                            self.db.update_task(run["task_id"], status="failed")
                except Exception as db_exc:
                    self.logger.error("Failed to record error for run %s: %s", run["id"], db_exc)

    async def _execute_run(self, run: dict) -> None:
        run_id = run["id"]
        task = self.db.get_task(run["task_id"])
        if not task:
            self.logger.error("Run %s: task not found (task_id=%s)", run_id, run["task_id"])
            self.db.update_run(run_id, status="failed", ended_at=utc_now(), exit_code=-1)
            return

        project = self.db.get_project(task["project_id"])
        if not project:
            self.logger.error("Run %s: project not found (project_id=%s)", run_id, task["project_id"])
            self.db.update_run(run_id, status="failed", ended_at=utc_now(), exit_code=-1)
            err_msg_id = uuid.uuid4().hex
            self.db.create_message(err_msg_id, task["id"], "system",
                                   "**Run failed:** Project not found. Was it deleted?")
            self.db.update_task(task["id"], status="failed")
            return

        identifier = task["identifier"]
        self.logger.info("[%s] Starting run %s (backend=%s)", identifier, run_id, task.get("cli_backend", "claude"))

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
                # Reset worktree to base when: wipe_worktree is enabled (default)
                # AND previous PR was merged, OR it's the first run (no worktree yet).
                # When wipe_worktree is disabled, never reset — changes accumulate.
                wipe_enabled = task.get("wipe_worktree", 1)
                reset_to_base = bool(wipe_enabled and task.get("pr_merged"))
                worktree_path = await asyncio.to_thread(
                    ensure_worktree, workdir, wt_root, identifier, ssh_env, base_branch, reset_to_base
                )
                write_worktree_meta(session_dir, workdir, worktree_path)
                self.db.update_task(task["id"], worktree_path=str(worktree_path))
                if reset_to_base:
                    # Clear merged flag after reset so subsequent runs preserve work
                    self.db.update_task(task["id"], pr_merged=0, pr_url=None)
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

        # Select CLI backend for this task
        backend_name = task.get("cli_backend") or "claude"
        try:
            backend = self._get_backend(backend_name)
        except ValueError:
            self.logger.warning("[%s] Unknown backend %r, falling back to claude", identifier, backend_name)
            backend = self._get_backend("claude")

        resume_session_id = task.get("claude_session_id") if backend_name in ("claude", "codex") else None

        # Build prompt — use pre-filled prompt_template from slash commands if available,
        # but always append git/completion instructions so commits happen.
        if run.get("prompt") and run["prompt"].strip():
            prompt = run["prompt"] + "\n\n" + self._completion_instructions(task, project)
        elif resume_session_id:
            # Resuming a Claude session — Claude already has full history.
            # Only send NEW user messages since the last run to save tokens.
            messages = self.db.list_messages(task["id"])
            new_msgs = []
            for msg in reversed(messages):
                if msg["role"] == "user":
                    new_msgs.insert(0, msg)
                else:
                    break  # stop at the last non-user message (assistant/system)
            if new_msgs:
                prompt = "\n\n".join(m["content"] for m in new_msgs)
                prompt += "\n\n" + self._completion_instructions(task, project)
            else:
                prompt = "Continue with the task.\n\n" + self._completion_instructions(task, project)
        else:
            messages = self.db.list_messages(task["id"])
            prompt = self._build_prompt(task, project, messages)
            self.db.update_run(run_id, prompt=prompt)

        if self.settings.test_mode:
            self.logger.info("[%s] TEST MODE — skipping %s", identifier, backend.display_name)
            (session_dir / "stdout.txt").write_text(
                json.dumps({"type": "result", "result": f"Test mode: no {backend.display_name} run.", "total_cost_usd": 0}) + "\n",
                encoding="utf-8",
            )
            self.db.update_run(run_id, status="done", ended_at=utc_now(), exit_code=0)
            assistant_msg_id = uuid.uuid4().hex
            self.db.create_message(assistant_msg_id, task["id"], "assistant",
                                   f"Test mode: no {backend.display_name} run.", run_id=run_id)
            task_status = "in_progress" if self.db.has_pending_runs(task["id"]) else "done"
            self.db.update_task(task["id"], status=task_status)
            return

        proc_holder: dict = {}
        self._proc_holders[run_id] = proc_holder
        ssh_env = self._get_git_ssh_env()
        merged_env = {**(ssh_env or {}), **getattr(backend, "_extra_env", {})}

        self.logger.info("[%s] Launching %s (resume=%s, workdir=%s, prompt_len=%d)",
                         identifier, backend.display_name, resume_session_id or "new", workdir, len(prompt))
        try:
            run_result = await asyncio.to_thread(
                backend.run, identifier, prompt, session_dir, workdir,
                proc_holder, resume_session_id, merged_env or None,
            )
        except Exception as exc:
            self.logger.error("[%s] %s runner error: %s", identifier, backend.display_name, exc, exc_info=True)
            err_msg_id = uuid.uuid4().hex
            self.db.create_message(err_msg_id, task["id"], "assistant",
                                   f"**{backend.display_name} failed to start:**\n\n`{exc}`\n\nCheck that the CLI is installed and accessible.",
                                   run_id=run_id)
            self.db.update_run(run_id, status="failed", ended_at=utc_now(), exit_code=-1)
            self.db.update_task(task["id"], status="failed")
            return
        finally:
            self._proc_holders.pop(run_id, None)

        exit_code = run_result.returncode
        summary_preview = (run_result.summary or "")[:100].replace("\n", " ")
        self.logger.info("[%s] %s exited code=%d, summary=%r, session=%s, tokens=%d/%d",
                         identifier, backend.display_name, exit_code, summary_preview,
                         run_result.session_id or "none",
                         run_result.input_tokens, run_result.output_tokens)
        if exit_code != 0 and run_result.stderr:
            self.logger.warning("[%s] stderr: %s", identifier, run_result.stderr[:500])

        # Check if the run was cancelled while the process was running.
        # cancel_run() sets status="failed" — don't overwrite that.
        current_run = self.db.get_run(run_id)
        if current_run and current_run["status"] == "failed":
            self.logger.info("[%s] Run %s was cancelled during execution, skipping post-processing", identifier, run_id)
            return

        new_session_id = run_result.session_id
        if new_session_id:
            self.db.update_task(task["id"], claude_session_id=new_session_id)

        summary = run_result.summary
        cost_usd = run_result.cost_usd
        input_tokens = run_result.input_tokens
        output_tokens = run_result.output_tokens
        model = run_result.model

        # If process failed with no summary, construct an error message from stderr
        if exit_code != 0 and not summary.strip():
            stderr_preview = (run_result.stderr or "").strip()[:1000]
            summary = f"**{backend.display_name} exited with code {exit_code}**"
            if stderr_preview:
                summary += f"\n\n```\n{stderr_preview}\n```"
            else:
                summary += "\n\nNo output captured. Check server logs for details."
            self.logger.warning("[%s] Empty summary with exit_code=%d, stderr: %s",
                                identifier, exit_code, stderr_preview[:200] or "(empty)")

        # Store assistant message
        assistant_msg_id = uuid.uuid4().hex
        self.db.create_message(
            assistant_msg_id, task["id"], "assistant", summary or "(No output)", run_id=run_id,
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
        github_token = self._get_github_token(project)
        if exit_code == 0 and github_token:
            pr_url, pr_error = await self._maybe_create_pr(task, session_dir, identifier,
                                                            base_branch=project.get("base_branch", ""),
                                                            github_token=github_token)
            if not pr_url and pr_error:
                # Auto-retry once after 20s for transient errors (DNS, network)
                self.logger.info("[%s] PR creation failed, retrying in 20s: %s", identifier, pr_error)
                await asyncio.sleep(20)
                pr_url, pr_error = await self._maybe_create_pr(task, session_dir, identifier,
                                                                base_branch=project.get("base_branch", ""),
                                                                github_token=github_token)
            if pr_url:
                await self._handle_pr_success(task, identifier, pr_url, github_token)
            elif pr_error:
                err_msg_id = uuid.uuid4().hex
                self.db.create_message(err_msg_id, task["id"], "system",
                                       pr_error + "\n\n<button class=\"btn btn-sm btn-primary retry-pr-btn\" "
                                       f"onclick=\"retryPR('{task['id']}')\">Retry PR</button>")

        self.logger.info("[%s] Run %s complete (status=%s, cost=$%.4f)",
                         identifier, run_id, status, cost_usd)

    async def _handle_pr_success(self, task: dict, identifier: str, pr_url: str, github_token: str) -> None:
        """Store PR URL, post system message, attempt auto-merge."""
        self.db.update_task(task["id"], pr_url=pr_url, status="in_review")
        pr_msg_id = uuid.uuid4().hex
        self.db.create_message(pr_msg_id, task["id"], "system",
                               f"Pull request created: [{pr_url}]({pr_url})")

        merged = await self._merge_github_pr(pr_url, github_token=github_token)
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

    async def retry_pr(self, task: dict, project: dict) -> tuple[str | None, str | None]:
        """Retry PR creation for a task. Called from the API endpoint."""
        identifier = task["identifier"]
        github_token = self._get_github_token(project)
        if not github_token:
            return None, "No GitHub token configured."

        # Find the latest session dir for this task
        runs = self.db.list_runs(task["id"])
        session_dir = None
        for r in runs:
            if r.get("session_dir"):
                session_dir = Path(r["session_dir"])
                break
        if not session_dir or not session_dir.exists():
            return None, "No session found for this task."

        pr_url, pr_error = await self._maybe_create_pr(
            task, session_dir, identifier,
            base_branch=project.get("base_branch", ""),
            github_token=github_token,
        )
        if pr_url:
            await self._handle_pr_success(task, identifier, pr_url, github_token)
            return pr_url, None
        return None, pr_error

    async def push_and_pr(self, task: dict, project: dict) -> tuple[str | None, str | None]:
        """Push current worktree and create PR. Uses worktree_path directly."""
        identifier = task["identifier"]
        github_token = self._get_github_token(project)
        if not github_token:
            return None, "No GitHub token configured."

        worktree_path = task.get("worktree_path")
        if not worktree_path or not Path(worktree_path).exists():
            return None, "No worktree found for this task."

        # We need a session dir for worktree meta. Find or create one.
        runs = self.db.list_runs(task["id"])
        session_dir = None
        for r in runs:
            if r.get("session_dir"):
                session_dir = Path(r["session_dir"])
                break
        if not session_dir:
            # Create a temporary session dir with worktree meta
            session_dir = self.settings.data_path() / "sessions" / identifier / "manual-pr"
            session_dir.mkdir(parents=True, exist_ok=True)

        repo = self.settings.project_repo_path(project["slug"])
        write_worktree_meta(session_dir, repo, Path(worktree_path))

        pr_url, pr_error = await self._maybe_create_pr(
            task, session_dir, identifier,
            base_branch=project.get("base_branch", ""),
            github_token=github_token,
        )
        if pr_url:
            await self._handle_pr_success(task, identifier, pr_url, github_token)
            return pr_url, None
        return None, pr_error

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
            parts.extend(["", "## Description", sanitize_for_prompt(task["description"])])

        # Source context from derived task (cross-model review)
        if task.get("source_context"):
            try:
                ctx = json.loads(task["source_context"])
                purpose_prompt = ctx.get("purpose_prompt", "")
                source_id = ctx.get("source_identifier", "")
                source_msgs = ctx.get("messages", [])
                if purpose_prompt or source_msgs:
                    parts.extend(["", "---", ""])
                    if purpose_prompt:
                        parts.append(purpose_prompt)
                        parts.append("")
                    parts.append(f"## Context from {source_id}" if source_id else "## Source Context")
                    parts.append("")
                    for sm in source_msgs:
                        role_label = {"user": "User", "assistant": "Assistant", "system": "System"}.get(sm["role"], sm["role"].title())
                        parts.append(f"**[{role_label}]:**")
                        parts.append(sm.get("content", ""))
                        parts.append("")
            except (json.JSONDecodeError, ValueError, KeyError):
                pass

        # Conversation history (limit to last 20 messages to avoid prompt bloat)
        if messages:
            recent = messages[-20:] if len(messages) > 20 else messages
            parts.extend(["", "---", "", "## Conversation History", ""])
            if len(messages) > 20:
                parts.append(f"*({len(messages) - 20} earlier messages omitted)*\n")
            for msg in recent:
                role_label = {"user": "User", "assistant": "Assistant", "system": "System"}.get(msg["role"], msg["role"].title())
                # Truncate very long assistant messages in history
                content = msg["content"]
                if msg["role"] == "assistant" and len(content) > 2000:
                    content = content[:2000] + "\n\n[...truncated]"
                parts.append(f"**[{role_label}]:**")
                # Fence user-provided content to reduce prompt injection risk
                if msg["role"] == "user":
                    content = fence_user_content(sanitize_for_prompt(content), label="user message")
                parts.append(content)
                parts.append("")

        parts.append(self._completion_instructions(task, project))

        return "\n".join(parts)

    def _completion_instructions(self, task: dict, project: dict) -> str:
        """Git context and commit instructions appended to every prompt."""
        parts = []
        if task.get("branch_name"):
            parts.extend([
                "---",
                "",
                "## Git Context",
                f"- Branch: `{task['branch_name']}`",
                f"- Base: `{project.get('base_branch', 'main')}`",
            ])

        prefix = "fix" if task["mode"] == "bug" else "feat" if task["mode"] == "feature" else "refactor"
        commit_msg = f"{prefix}: {task['title']} ({task['identifier']})"
        parts.extend([
            "",
            "## Completion",
            "",
            "IMPORTANT: After implementation and testing, you MUST commit your changes:",
            f"1. `git add -A`",
            f'2. `git commit -m "{commit_msg}"`',
            "3. Write a brief summary of what you changed and why.",
            "",
            "Do NOT skip the commit step. Your work is lost if you don't commit.",
        ])
        return "\n".join(parts)

    # ── Job Control ──

    def is_run_alive(self, run_id: str) -> bool:
        """Check if a run has a live process in this server instance."""
        holder = self._proc_holders.get(run_id)
        if not holder:
            return False
        proc = holder.get("proc")
        if proc and proc.poll() is not None:
            return False  # Process has exited
        return True

    def cancel_run(self, run_id: str) -> bool:
        holder = self._proc_holders.get(run_id)
        if holder and "proc" in holder:
            try:
                holder["proc"].terminate()
                self.db.update_run(run_id, status="failed", ended_at=utc_now(), exit_code=-1)
                run = self.db.get_run(run_id)
                if run:
                    msg_id = uuid.uuid4().hex
                    self.db.create_message(msg_id, run["task_id"], "system",
                                           "Run cancelled by user.")
                    self.db.update_task(run["task_id"], status="failed")
                return True
            except Exception as exc:
                self.logger.warning("Failed to terminate run %s: %s", run_id, exc)
        return False

    async def enqueue_message(self, task_id: str, content: str,
                              prompt_template: str = "") -> dict:
        """Create a user message and enqueue a run for it.

        If prompt_template is provided (from slash commands), it is stored on the
        run as metadata and used directly as the prompt instead of building from
        conversation history — avoids duplicating the template in the prompt.
        """
        task = self.db.get_task(task_id)
        if not task:
            raise ValueError("Task not found")

        # Re-open completed/failed tasks so they can continue
        if task["status"] in ("done", "failed"):
            self.db.update_task(task_id, status="open")

        # Store user message (short display form, e.g. "/bugs")
        msg_id = uuid.uuid4().hex
        self.db.create_message(msg_id, task_id, "user", content)

        # Create pending run. If a slash-command prompt_template is provided,
        # store it in the run's prompt field so _execute_run uses it directly.
        run_id = uuid.uuid4().hex
        run = self.db.create_run(run_id, task_id, prompt=prompt_template or "")

        # Ensure worker is running for this project
        self._ensure_worker(task["project_id"])

        return run

    # ── PR Management ──

    async def _maybe_create_pr(self, task: dict, session_dir: Path, identifier: str,
                                base_branch: str = "", github_token: str = "") -> tuple[str | None, str | None]:
        """Try to push and create a PR. Returns (pr_url, error_message)."""
        if not github_token:
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
                    "Authorization": f"Bearer {github_token}",
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

    async def _merge_github_pr(self, pr_url: str, github_token: str = "") -> bool:
        if not github_token:
            return False
        m = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', pr_url)
        if not m:
            return False
        owner, repo, pr_number = m.group(1), m.group(2), m.group(3)
        headers = {
            "Authorization": f"Bearer {github_token}",
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
                # Don't requeue runs that still have a live process in this server
                live_run_ids = {
                    rid for rid, holder in self._proc_holders.items()
                    if holder.get("proc") and holder["proc"].poll() is None
                }
                project_ids = self.db.requeue_stale_runs(cutoff.isoformat(), exclude_run_ids=live_run_ids)
                if project_ids:
                    self.logger.info("Reaper: requeued stale runs for %d projects", len(project_ids))
                    for pid in project_ids:
                        self._ensure_worker(pid)
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
                        session_path = Path(d)
                        meta = read_worktree_meta(session_path)
                        if meta and meta.get("repo") and meta.get("worktree"):
                            try:
                                remove_worktree(Path(meta["repo"]), Path(meta["worktree"]))
                            except Exception:
                                pass
                        try:
                            shutil.rmtree(str(session_path), ignore_errors=True)
                        except Exception:
                            pass
                        identifier_dir = self.settings.data_path() / "sessions" / entry["identifier"]
                        try:
                            identifier_dir.rmdir()
                        except OSError:
                            pass
                if entries:
                    self.logger.info("Cleaned %d old runs", len(entries))

                # Auto-delete tasks older than 14 days
                task_cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
                old_tasks = self.db.list_old_tasks(task_cutoff)
                for t in old_tasks:
                    try:
                        self.cleanup_task(t["id"])
                        self.db.delete_task(t["id"])
                    except Exception as exc:
                        self.logger.warning("Auto-delete task %s failed: %s", t["identifier"], exc)
                if old_tasks:
                    self.logger.info("Auto-deleted %d tasks older than 14 days", len(old_tasks))
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

        if task.get("worktree_path"):
            wt_path = Path(task["worktree_path"])
            if wt_path.exists():
                project = self.db.get_project(task["project_id"])
                if project:
                    repo = self.settings.project_repo_path(project["slug"])
                    try:
                        remove_worktree(repo, wt_path)
                        removed.append("worktree")
                    except Exception:
                        shutil.rmtree(str(wt_path), ignore_errors=True)
                        removed.append("worktree")
                else:
                    shutil.rmtree(str(wt_path), ignore_errors=True)
                    removed.append("worktree")

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
