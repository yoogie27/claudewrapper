from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from app.claude_runner import ClaudeRunner
from app.config import Settings
from app.db import Database
from app.linear_client import LinearClient, LinearAPIError
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
from app.sanitize import fence_user_content, sanitize_for_prompt, validate_identifier, safe_identifier
from app.ssh import get_git_ssh_env

# Old generic prompts that should be replaced by the new default
_OLD_GENERIC_PROMPTS = {
    "you are an autonomous coding agent. use this repository to implement the ticket.",
    "you are an autonomous coding agent.",
    "you are an autonomous senior developer. implement the ticket in this repository following existing patterns and conventions.",
}

DEFAULT_PROMPT = """\
You are an autonomous senior developer working in this repository.

## How you work

1. **Understand first**: Read the full ticket, ALL comments, and any feedback carefully. \
If the ticket was reopened or has new comments, treat the latest feedback as strict correction instructions.
2. **Research the codebase**: Use LSP tools (find-definition, find-references) to understand \
the architecture before changing anything. Navigate through symbols, don't just grep.
3. **Check history**: Use `git log -L` to trace how specific functions evolved, \
or `git log -S` to see how similar changes were made in the past.
4. **Use Linear as knowledge base**: If you have Linear MCP tools available, search for \
similar resolved tickets for patterns, conventions, and prior solutions.
5. **Follow existing patterns**: Match the coding style, conventions, and architecture already in the repo.
6. **Test your changes**: Run the project's tests after implementation. If no tests exist for your change, add basic coverage.
7. **Stay focused**: Only change what the ticket requires. Don't refactor unrelated code.\
"""


class Orchestrator:
    def __init__(self, settings: Settings, db: Database) -> None:
        self.settings = settings
        self.db = db
        self.linear = LinearClient(settings.linear_api_key)
        self.runner = ClaudeRunner(
            settings.claude_command_template,
            settings.claude_prompt_via,
            settings.claude_prompt_arg,
            settings.claude_workdir_mode,
        )
        log_path = str(settings.data_path() / "logs" / "app.log")
        self.logger = setup_logger(log_path)
        self.stop_event = asyncio.Event()
        self.mcp_process = None
        # job_id -> {"proc": Popen} — populated by the runner thread so cancel can reach it
        self._proc_holders: dict[int, dict] = {}

    async def start(self) -> None:
        self.settings.ensure_dirs()
        # Auto-enable MCP when LINEAR_API_KEY is present
        if self.settings.linear_mcp_enabled or self.settings.linear_api_key:
            self._setup_mcp_config()
        await self._auto_detect_states()
        asyncio.create_task(self._poll_loop())
        for i in range(self.settings.worker_count):
            asyncio.create_task(self._worker_loop(f"worker-{i+1}"))
        asyncio.create_task(self._cleanup_loop())
        asyncio.create_task(self._reaper_loop())
        asyncio.create_task(self._pr_status_loop())

    def _get_git_ssh_env(self) -> dict[str, str] | None:
        """Build GIT_SSH_COMMAND env dict if SSH keys are configured."""
        if not self.settings.ssh_key_dir:
            return None
        key_dir = Path(self.settings.ssh_key_dir)
        if (key_dir / "id_ed25519").exists():
            return get_git_ssh_env(key_dir)
        return None

    # Hidden marker appended to every comment we post.
    # Used to identify (and ignore) our own comments during polling.
    COMMENT_MARKER = "<!-- claudewrapper -->"

    @staticmethod
    def is_own_comment(body: str) -> bool:
        return Orchestrator.COMMENT_MARKER in (body or "")

    def _safe_mark_failed(self, job_id: int, issue_id: str, identifier: str, session_dir: Path, error: str) -> None:
        """Mark a job/session as failed, swallowing DB errors so the worker survives."""
        try:
            self.db.upsert_session(job_id, issue_id, identifier, "failed", str(session_dir), None, datetime.now(timezone.utc).isoformat())
            self.db.complete_job(job_id, False, error)
        except Exception as db_exc:
            self.logger.error("[%s] Could not record failure in DB (job #%d): %s — original error: %s", identifier, job_id, db_exc, error)

    def _touch_issue_updated(self, issue_id: str) -> None:
        """Bump last_updated_at so the poller won't re-scan old comments."""
        now = datetime.now(timezone.utc).isoformat()
        self.db._conn.execute(
            "UPDATE issue_state SET last_updated_at=?, last_seen_at=? WHERE issue_id=?",
            (now, now, issue_id),
        )
        self.db._conn.commit()

    async def _post_comment(self, issue_id: str, body: str) -> None:
        """Post a comment on a Linear issue with a marker so we can identify it later."""
        await self.linear.create_comment(issue_id, body + "\n" + self.COMMENT_MARKER)

    async def _auto_detect_states(self) -> None:
        """Auto-detect sensible state defaults from Linear if not manually configured."""
        if not self.settings.linear_api_key:
            return
        # Only auto-detect if at least one state is unconfigured
        keys = {
            "status_done": self.settings.done_state_name,
            "status_review": self.settings.review_state_name,
            "status_hitl": self.settings.hitl_state_name,
            "status_error": self.settings.error_state_name,
        }
        all_set = all(self.db.get_config(k) or fallback for k, fallback in keys.items())
        if all_set:
            return

        # Get the first enabled team's states as reference
        mappings = self.db.list_team_mappings()
        enabled = [m for m in mappings if m["enabled"]]
        if not enabled:
            return

        try:
            states = await self.linear.get_workflow_states(enabled[0]["team_id"])
        except Exception as exc:
            self.logger.warning("Auto-detect states failed: %s", exc)
            return

        state_by_name = {s["name"].lower(): s for s in states}
        state_by_type: dict[str, list] = {}
        for s in states:
            state_by_type.setdefault(s["type"], []).append(s)

        def _find(candidates: list[str], state_type: str | None = None) -> str:
            """Find a matching state by name candidates, then by type."""
            for name in candidates:
                if name.lower() in state_by_name:
                    return state_by_name[name.lower()]["name"]
            if state_type and state_type in state_by_type:
                return state_by_type[state_type][0]["name"]
            return ""

        defaults = {
            "status_done": _find(["Done", "Closed", "Complete", "Completed"], "completed"),
            "status_review": _find(["In Review", "Review", "Code Review", "Ready for Review"], None),
            "status_hitl": _find(["Awaiting Feedback", "Needs Input", "Waiting", "Paused"], None),
            "status_error": _find(["Blocked", "Error", "Failed", "Cancelled"], "canceled"),
        }

        # Log all available states for debugging
        self.logger.info(
            "Team '%s' workflow states: %s",
            enabled[0]["team_name"],
            ", ".join(f"{s['name']} ({s['type']})" for s in states),
        )

        for key, fallback in keys.items():
            if not (self.db.get_config(key) or fallback):
                detected = defaults.get(key, "")
                if detected:
                    self.db.set_config(key, detected)
                    self.logger.info("Auto-detected %s = '%s'", key, detected)

    def _setup_mcp_config(self) -> None:
        """Build MCP config that gets injected into each workdir before Claude runs.

        Supports two modes:
        - LINEAR_MCP_COMMAND set: uses command-based stdio MCP server (e.g. npx @anthropic/linear-mcp)
        - LINEAR_MCP_COMMAND empty: auto-configures with npx and the LINEAR_API_KEY
        """
        cmd = self.settings.linear_mcp_command.strip()
        if cmd:
            # Parse user-provided command into command + args
            import shlex
            parts = shlex.split(cmd)
            mcp_entry = {
                "command": parts[0],
                "args": parts[1:] if len(parts) > 1 else [],
            }
            if self.settings.linear_api_key:
                mcp_entry["env"] = {"LINEAR_API_KEY": self.settings.linear_api_key}
        else:
            # Default: use npx with the Linear MCP server package
            mcp_entry = {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-linear"],
                "env": {"LINEAR_API_KEY": self.settings.linear_api_key},
            }

        self.runner.mcp_config = {
            "mcpServers": {
                "linear": mcp_entry,
            }
        }
        self.logger.info("MCP config prepared for injection into workdirs")

    # Adaptive polling intervals (seconds).  After activity the loop polls
    # aggressively; when nothing happens it backs off to save API quota.
    _POLL_FAST = 10        # right after a ticket was found
    _POLL_STEPS = [10, 30, 60, 120, 300, 600]  # progressive back-off
    _POLL_MAX = 600        # ceiling = 10 minutes

    async def _poll_loop(self) -> None:
        step_idx = 0  # index into _POLL_STEPS (0 = fastest)
        while not self.stop_event.is_set():
            try:
                found = await self._poll_once()
                if found:
                    # Activity detected → reset to fastest polling
                    step_idx = 0
                    self.logger.info("Poll: activity detected, interval → %ds", self._POLL_FAST)
                else:
                    # No activity → advance one step toward the ceiling
                    step_idx = min(step_idx + 1, len(self._POLL_STEPS) - 1)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                self.logger.warning("Poll: network error (%s)", exc)
                step_idx = min(step_idx + 1, len(self._POLL_STEPS) - 1)
            except LinearAPIError as exc:
                self.logger.error("Poll: %s", exc)
            except Exception as exc:
                self.logger.exception("Poll: unexpected error: %s", exc)
            interval = self._POLL_STEPS[step_idx]
            await asyncio.sleep(interval)

    async def _poll_once(self) -> bool:
        """Poll Linear for updated issues. Returns True if any job was enqueued."""
        if not self.settings.linear_api_key:
            self.logger.error("LINEAR_API_KEY missing. Polling disabled.")
            return False
        last_poll = self.db.get_config("last_poll")
        if not last_poll:
            last_poll = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()

        issues = await self.linear.get_issues_updated_since(last_poll, first=self.settings.max_issues_per_poll)
        now_iso = datetime.now(timezone.utc).isoformat()
        enqueued = False

        for issue in issues:
            issue_id = issue["id"]
            identifier = issue["identifier"]
            team_id = issue["team"]["id"]
            title = issue.get("title")
            state_type = issue["state"]["type"] if issue.get("state") else None
            state_name = issue["state"]["name"] if issue.get("state") else None
            updated_at = issue.get("updatedAt") or now_iso
            created_at = issue.get("createdAt")

            prev = self.db.get_issue_state(issue_id)
            reason = None

            if created_at and created_at > last_poll:
                reason = "new"
            elif prev and prev["last_state_type"] in ("completed", "canceled") and state_type not in ("completed", "canceled"):
                reason = "reopened"
            elif prev and prev["last_updated_at"] and updated_at > prev["last_updated_at"]:
                # Issue was updated — check if there are new comments
                try:
                    comments = await self.linear.get_issue_comments_since(issue_id, prev["last_updated_at"])
                    ignored_ids = self.settings.ignored_author_ids()
                    ignored_emails = self.settings.ignored_author_emails()
                    real_comments = [
                        c for c in comments
                        if c.get("user")
                        and not self.is_own_comment(c.get("body"))
                        and c["user"].get("id") not in ignored_ids
                        and c["user"].get("email", "").lower() not in ignored_emails
                    ]
                    if real_comments:
                        reason = "comment"
                        for rc in real_comments:
                            body_preview = (rc.get("body") or "")[:80].replace("\n", " ")
                            user_name = (rc.get("user") or {}).get("name", "?")
                            self.logger.info("[%s] Triggering comment by %s: %s", identifier, user_name, body_preview)
                except Exception as exc:
                    self.logger.error("Failed to check comments for %s: %s", identifier, exc)

            if reason:
                mapping = self.db.get_team_mapping(team_id)
                if mapping and mapping["enabled"] and mapping["auto_process"]:
                    self.db.enqueue_job(issue_id, identifier, team_id, reason)
                    enqueued = True

            self.db.upsert_issue_state(
                issue_id=issue_id,
                identifier=identifier,
                team_id=team_id,
                state_type=state_type,
                state_name=state_name,
                last_comment_at=None,
                last_seen_at=now_iso,
                last_updated_at=updated_at,
                title=title,
            )

        self.db.set_config("last_poll", now_iso)
        return enqueued

    def is_queue_paused(self) -> bool:
        return self.db.get_config("queue_paused") == "1"

    def set_queue_paused(self, paused: bool, reason: str = "") -> None:
        if paused:
            self.db.set_config("queue_paused", "1")
            if reason:
                self.db.set_config("queue_paused_reason", reason)
            self.logger.warning("Queue PAUSED%s", f": {reason}" if reason else "")
        else:
            self.db.delete_config("queue_paused")
            self.db.delete_config("queue_paused_reason")
            self.logger.info("Queue RESUMED")

    async def poll_now(self) -> bool:
        """Trigger an immediate poll. Returns True if any job was enqueued."""
        return await self._poll_once()

    async def _worker_loop(self, worker_id: str) -> None:
        while not self.stop_event.is_set():
            # Check global queue pause
            if self.is_queue_paused():
                await asyncio.sleep(3)
                continue

            job = self.db.dequeue_job(worker_id, max_per_team=self.settings.max_concurrent_per_team)
            if not job:
                await asyncio.sleep(2)
                continue

            job_id = job["id"]
            issue_id = job["issue_id"]
            identifier = safe_identifier(job["identifier"])
            team_id = job["team_id"]
            reason = job["reason"]
            self.logger.info("[%s] Job #%d dequeued (reason: %s)", identifier, job_id, job["reason"])

            # Check if team is paused — re-queue and wait
            if self.db.is_team_paused(team_id):
                self.logger.info("[%s] Team paused, re-queuing", identifier)
                self.db.update_job_status(job_id, "pending")
                await asyncio.sleep(5)
                continue
            # Each run gets its own subdirectory so history is preserved on disk
            session_dir = self.settings.data_path() / "sessions" / identifier / str(job_id)
            self.db.upsert_session(job_id, issue_id, identifier, "running", str(session_dir), datetime.now(timezone.utc).isoformat(), None)

            try:
                mapping = self.db.get_team_mapping(team_id)
                if not mapping:
                    raise RuntimeError("Team mapping missing")

                issue = await self.linear.get_issue_details(issue_id)
                closed = await self.linear.get_recent_closed_issues(team_id, first=20)
                label_instructions = self.db.list_label_instructions(team_id)

                prompt = self._build_prompt(issue, closed, mapping["default_prompt"], reason, label_instructions)
                self.logger.info("[%s] Prompt built (%d chars)", identifier, len(prompt))

                workdir = None
                if self.settings.claude_workdir_mode in ("team_path", "repo_root"):
                    workdir = Path(mapping["local_path"]).resolve()

                ssh_env = self._get_git_ssh_env()

                if self.settings.use_git_worktrees and workdir:
                    worktree_root = Path(self.settings.worktree_root)
                    worktree_path = ensure_worktree(workdir, worktree_root, identifier, env=ssh_env)
                    write_worktree_meta(session_dir, workdir, worktree_path)
                    workdir = worktree_path

                if self.settings.test_mode:
                    # Test mode: skip Claude, simulate a successful run,
                    # but execute the full post-processing pipeline (comments,
                    # state transitions, PR) so we can verify nothing re-triggers.
                    session_dir.mkdir(parents=True, exist_ok=True)
                    (session_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
                    fake_summary = f"[TEST MODE] Simulated completion for {identifier}."
                    (session_dir / "stdout.txt").write_text(fake_summary, encoding="utf-8")
                    (session_dir / "stderr.txt").write_text("", encoding="utf-8")
                    self.logger.info("[%s] Test mode — simulating successful run", identifier)
                    stdout_text = fake_summary
                    ok = True
                else:
                    # Check if we can resume a prior Claude session for this ticket
                    resume_id = self.db.get_last_claude_session_id(identifier) if reason in ("retry", "feedback", "batch") else None
                    if resume_id:
                        self.logger.info("[%s] Resuming Claude session %s in %s", identifier, resume_id, workdir or "no workdir")
                    else:
                        self.logger.info("[%s] Starting fresh Claude session in %s", identifier, workdir or "no workdir")

                    # Run in thread so the event loop stays responsive (SSE streaming, webhooks)
                    proc_holder: dict = {}
                    self._proc_holders[job_id] = proc_holder
                    try:
                        result = await asyncio.to_thread(
                            self.runner.run, identifier, prompt, session_dir, workdir,
                            proc_holder, resume_id, ssh_env,
                        )
                    finally:
                        self._proc_holders.pop(job_id, None)

                    self.logger.info("[%s] Claude exited with code %d (stdout: %d bytes, stderr: %d bytes)", identifier, result.returncode, len(result.stdout), len(result.stderr))

                    # Store Claude session UUID for future --resume
                    claude_sid = getattr(result, "claude_session_id", None)
                    if claude_sid:
                        self.db.set_claude_session_id(job_id, claude_sid)
                        self.logger.info("[%s] Claude session ID: %s", identifier, claude_sid)

                    stdout_text = result.stdout
                    ok = result.returncode == 0

                    if not ok:
                        stderr_tail = result.stderr.strip()[-500:] if result.stderr else "(empty)"
                        self.logger.warning("[%s] Claude failed (exit %d). stderr tail:\n%s", identifier, result.returncode, stderr_tail)

                # ── Post-processing (runs for both test and real modes) ──
                # Bump timestamp BEFORE posting comments/changing state so that
                # webhooks and the poller triggered by our own changes don't
                # re-enqueue this issue.
                self._touch_issue_updated(issue_id)

                summary, hitl = self._summarize_result(stdout_text)
                if summary:
                    await self._post_comment(issue_id, summary)

                status = "done" if ok else "failed"

                if hitl:
                    status = "awaiting_feedback"
                    await self._set_hitl_state(issue_id, team_id)
                elif ok:
                    await self._set_named_state(issue_id, team_id, self._get_state_name("status_review", self.settings.review_state_name) or self._get_state_name("status_done", self.settings.done_state_name))
                    pr_url = await self._maybe_create_pr(issue, session_dir, identifier)
                    if pr_url:
                        self.db.set_session_pr_url(job_id, pr_url)
                        await self._post_comment(issue_id, f"Pull request: {pr_url}")
                        # Auto-merge if the team has it enabled
                        mapping = self.db.get_team_mapping(team_id)
                        if mapping and mapping["auto_merge"]:
                            merged = await self._merge_github_pr(pr_url)
                            if merged:
                                self.logger.info("[%s] PR auto-merged: %s", identifier, pr_url)
                                await self._post_comment(issue_id, "PR automatically merged to main.")
                            else:
                                # Auto-merge failed — resume Claude session to fix and merge
                                claude_sid = self.db.get_last_claude_session_id(identifier)
                                if claude_sid and not self.settings.test_mode:
                                    self.logger.info("[%s] Auto-merge failed, resuming Claude to fix merge", identifier)
                                    merge_ok = await self._claude_fix_and_merge(
                                        identifier, claude_sid, pr_url, session_dir, workdir, ssh_env, job_id,
                                    )
                                    if merge_ok:
                                        self.logger.info("[%s] Claude fixed merge, PR merged: %s", identifier, pr_url)
                                        await self._post_comment(issue_id, "PR automatically merged to main (after Claude resolved merge issues).")
                                    else:
                                        self.set_queue_paused(True, f"Auto-merge failed for {identifier} (Claude could not resolve)")
                                else:
                                    self.set_queue_paused(True, f"Auto-merge failed for {identifier}")
                else:
                    await self._set_named_state(issue_id, team_id, self._get_state_name("status_error", self.settings.error_state_name))
                    self.set_queue_paused(True, f"Job failed for {identifier}")

                if ok:
                    self.logger.info("[%s] Completed successfully (status: %s)", identifier, status)

                # Bump again after all comments/state changes to cover the full window
                self._touch_issue_updated(issue_id)

                self.db.upsert_session(job_id, issue_id, identifier, status, str(session_dir), None, datetime.now(timezone.utc).isoformat())
                if not ok and not self.settings.test_mode:
                    error_msg = f"Exit {result.returncode}"
                    if result.stderr and result.stderr.strip():
                        error_msg += f": {result.stderr.strip()[-300:]}"
                    self.db.complete_job(job_id, ok, error_msg)
                else:
                    self.db.complete_job(job_id, ok, None)

            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                self.logger.error("[%s] Network error (job #%d): %s", identifier, job_id, exc)
                self._safe_mark_failed(job_id, issue_id, identifier, session_dir, f"Network error: {exc}")
            except LinearAPIError as exc:
                self.logger.error("[%s] Linear API error (job #%d): %s", identifier, job_id, exc)
                self._safe_mark_failed(job_id, issue_id, identifier, session_dir, str(exc))
            except Exception as exc:
                self.logger.exception("[%s] Unexpected error (job #%d): %s", identifier, job_id, exc)
                self._safe_mark_failed(job_id, issue_id, identifier, session_dir, str(exc))

    def _build_prompt(
        self,
        issue: dict[str, Any],
        closed: list[dict[str, Any]],
        default_prompt: str,
        reason: str,
        label_instructions: list | None = None,
    ) -> str:
        # Replace old generic prompts with the rich default
        if not default_prompt or default_prompt.strip().lower() in _OLD_GENERIC_PROMPTS:
            default_prompt = DEFAULT_PROMPT

        labels_list = [n["name"] for n in issue.get("labels", {}).get("nodes", [])]
        labels = ", ".join(labels_list) or "none"
        assignee = issue.get("assignee")
        assignee_line = f"{assignee.get('name')} <{assignee.get('email')}>" if assignee else "(unassigned)"

        closed_list = "\n".join([f"- {c['identifier']}: {c['title']} ({c['url']})" for c in closed]) or "(none)"

        # Sanitize and fence user-supplied content to limit prompt injection
        description_raw = sanitize_for_prompt(issue.get("description") or "")
        description = fence_user_content(description_raw, "ticket description") if description_raw else "(no description)"

        comments = issue.get("comments", {}).get("nodes", [])
        comment_parts = []
        for c in comments:
            body = sanitize_for_prompt(c.get("body", ""), max_length=10_000)
            user_name = (c.get("user") or {}).get("name", "Unknown")
            ts = c.get("createdAt", "")
            comment_parts.append(f"[{ts}] {user_name}:\n{fence_user_content(body, f'comment by {user_name}')}")
        comment_text = "\n\n".join(comment_parts) or "(no comments)"

        # Determine strategy based on labels and reason
        label_lower = {l.lower() for l in labels_list}
        is_bug = "bug" in label_lower or "fix" in label_lower
        is_reopened = reason in ("reopened", "comment")

        if is_bug:
            strategy = (
                "This is a **bug fix**. Go straight into analysis:\n"
                "1. Reproduce and understand the problem from the description and comments.\n"
                "2. Use LSP tools (find-definition, find-references) to trace the affected code paths.\n"
                "3. Fix the root cause, not just symptoms.\n"
                "4. Validate the fix with tests. If no tests exist, add at least one."
            )
            pre_commit_review = (
                "## Pre-Commit Review (MANDATORY)\n\n"
                "**Do NOT commit, push, or create a PR until you have completed these steps:**\n\n"
                "### Step 1: Write a regression test\n"
                "Write a test that reproduces the original bug — a test that would FAIL on the "
                "old code and PASS on your fix. This prevents the same bug from reappearing in the future. "
                "Place the test alongside existing tests using the project's test framework and conventions.\n\n"
                "### Step 2: Impact analysis\n"
                "Examine whether the root cause affects other modules or systems:\n"
                "- Are there similar patterns elsewhere in the codebase that have the same flaw?\n"
                "- Could the bug manifest in related code paths or edge cases you haven't checked?\n"
                "- Use `grep`, LSP find-references, and `git log -S` to find all call sites and related logic.\n"
                "- If you find additional instances of the same problem, fix them as part of this change.\n\n"
                "### Step 3: Run all tests\n"
                "Run the full test suite. All tests must pass — including your new regression test — "
                "before you proceed to commit.\n\n"
                "Only after all three steps are complete: commit, push, and create the PR."
            )
        else:
            strategy = (
                "This is a **feature/task**. Plan before implementing:\n"
                "1. Identify the files and modules that need changes.\n"
                "2. Check existing patterns in the codebase for consistency.\n"
                "3. Implement incrementally, testing as you go.\n"
                "4. Run existing tests to confirm no regressions."
            )
            pre_commit_review = (
                "## Pre-Commit Review (MANDATORY)\n\n"
                "**Do NOT commit, push, or create a PR until you have completed these steps:**\n\n"
                "### Step 1: Self-review\n"
                "Review all your changes with a critical eye. Specifically check for:\n"
                "- **Correctness:** Are there logic errors, off-by-one mistakes, or wrong assumptions?\n"
                "- **Race conditions:** Are there concurrency or timing issues (async, threads, shared state)?\n"
                "- **Parameter handling:** Are all function parameters validated, typed correctly, and "
                "passed through every layer? Check for mismatches between callers and callees.\n"
                "- **Performance:** Have you introduced O(n^2) loops, unnecessary DB queries, repeated I/O, "
                "or memory leaks? Compare to the existing performance characteristics.\n"
                "- **Edge cases:** What happens with empty inputs, None values, missing keys, or very large data?\n"
                "- **Security:** Any injection risks, exposed secrets, or unsafe deserialization?\n\n"
                "If you find problems, fix them before proceeding.\n\n"
                "### Step 2: Write tests\n"
                "Add tests for the new functionality. Cover the happy path and at least one meaningful "
                "edge case. Use the project's existing test framework and conventions.\n\n"
                "### Step 3: Run all tests\n"
                "Run the full test suite. All existing and new tests must pass before you proceed to commit.\n\n"
                "Only after all three steps are complete: commit, push, and create the PR."
            )

        reopened_block = ""
        if is_reopened:
            reopened_block = (
                "\n## IMPORTANT: Feedback / Reopened Ticket\n"
                "This ticket was previously worked on and has been reopened or received new comments.\n"
                "**The most recent comments are strict correction instructions.** Prioritize them above all else.\n"
                "Review what was done before and apply the requested changes.\n"
            )

        # Label-specific instructions
        label_block = ""
        if label_instructions:
            issue_label_names = {n.lower() for n in labels_list}
            extras = [
                row["instruction"]
                for row in label_instructions
                if row["label_name"].lower() in issue_label_names
            ]
            if extras:
                label_block = "\n## Label-specific Instructions\n" + "\n".join(f"- {e}" for e in extras) + "\n"

        prompt = f"""\
# Autonomous Development Agent

{default_prompt.strip()}

---

## Your Mission

You are working on ticket **{issue['identifier']}**: "{sanitize_for_prompt(issue['title'], max_length=500)}".
Trigger reason: **{reason}**
{reopened_block}
## Ticket Details

| Field | Value |
|-------|-------|
| ID | {issue['identifier']} |
| Title | {issue['title']} |
| URL | {issue['url']} |
| Team | {issue['team']['name']} |
| State | {issue['state']['name']} ({issue['state']['type']}) |
| Assignee | {assignee_line} |
| Labels | {labels} |

## Description

{description}

## Comments & Feedback

{comment_text}

## Strategy

{strategy}

## Knowledge Base — Recently Resolved Tickets

Use these as reference for patterns, conventions, and solutions your team has used:

{closed_list}
{label_block}
## Research Workflow

Before writing any code:

1. **Understand the full context**: Read ALL comments carefully. If the ticket was reopened or has feedback, the latest comments override previous instructions.
2. **Structural analysis**: Use LSP tools (find-definition, find-references) to understand the architecture of affected modules. Navigate through symbols rather than just grepping.
3. **Git history**: Use `git log -L` to trace how specific functions evolved, or `git log -S` to find how similar changes were made.
4. **Search for similar issues**: If you have access to Linear MCP tools, search for related tickets that might contain useful context or solutions.

## Implementation Standards

- Follow existing coding standards and patterns in the repository.
- Keep changes focused — only modify what's needed for this ticket.
- Do NOT commit or push yet — complete the Pre-Commit Review first.

{pre_commit_review}

## Completion

After the pre-commit review is complete and all tests pass:

1. Commit with: `fix: {issue['title']} ({issue['identifier']})` for bugs, or `feat: {issue['title']} ({issue['identifier']})` for features.
2. Push the branch and create a PR.
3. Write a structured summary:

**Summary:** What you changed and why.
**Files modified:** Key files affected.
**Testing:** How you validated — include which tests were added and that the full suite passes.
**Notes:** Anything important for reviewers or follow-up.
"""
        return prompt

    def _summarize_result(self, stdout: str) -> tuple[str, bool]:
        text = (stdout or "").strip()
        if not text:
            return "Claude session completed. No stdout captured. Check session logs for details.", False

        # Try to parse stream-json output (one JSON object per line).
        # Look for the final {"type":"result",...} line first, then fall back
        # to single-object JSON, then plain text.
        import json

        # Stream-json: scan lines in reverse for the result event
        lines = text.splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("type") == "result":
                    raw = obj.get("result", "")
                    if isinstance(raw, list):
                        # result can be a content array
                        result = "".join(
                            c.get("text", "") for c in raw if c.get("type") == "text"
                        ).strip()
                    else:
                        result = str(raw).strip()
                    if not result:
                        result = "(empty result)"
                    cost = obj.get("total_cost_usd")
                    cost_line = f"\n(Cost: ${cost:.4f})" if cost is not None else ""
                    hitl = self._detect_hitl(result)
                    prefix = "Claude needs input:\n" if hitl else ""
                    return (prefix + result + cost_line)[:5000], hitl
            except (json.JSONDecodeError, ValueError):
                continue

        # Fallback: single JSON object (old --output-format json)
        if text.startswith("{") and text.endswith("}"):
            try:
                obj = json.loads(text)
                result = (obj.get("result") or "").strip()
                is_error = bool(obj.get("is_error"))
                if not result:
                    result = "(empty result)"
                prefix = "Claude error:\n" if is_error else ""
                hitl = self._detect_hitl(result)
                if hitl:
                    prefix = "Claude needs input:\n"
                return (prefix + result)[:5000], hitl
            except Exception:
                pass

        # Fallback: collect assistant text blocks from stream-json
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
                return combined[:5000], self._detect_hitl(combined)

        return text[:5000], self._detect_hitl(text)

    def _detect_hitl(self, text: str) -> bool:
        low = text.lower()
        # Explicit tool call signal
        if "askuserquestion" in low:
            return True
        # Short output that ends with a question (very likely asking for input)
        stripped = text.strip()
        if len(stripped) < 200 and stripped.endswith("?"):
            return True
        return False

    async def _set_hitl_state(self, issue_id: str, team_id: str) -> None:
        name = (self._get_state_name("status_hitl", self.settings.hitl_state_name) or "").strip()
        if not name:
            return
        try:
            state_id = await self.linear.get_workflow_state_by_name(team_id, name)
            if state_id:
                await self.linear.update_issue_state(issue_id, state_id)
                new_type = self.linear.get_workflow_state_type(team_id, name)
                if new_type:
                    self.db._conn.execute(
                        "UPDATE issue_state SET last_state_type=?, last_seen_at=datetime('now') WHERE issue_id=?",
                        (new_type, issue_id),
                    )
                    self.db._conn.commit()
        except Exception as exc:
            self.logger.error("Failed to set HITL state: %s", exc)

    async def _set_named_state(self, issue_id: str, team_id: str, state_name: str | None) -> None:
        name = (state_name or "").strip()
        if not name:
            return
        try:
            state_id = await self.linear.get_workflow_state_by_name(team_id, name)
            if state_id:
                await self.linear.update_issue_state(issue_id, state_id)
                # Update the DB so the next poll doesn't see the old state type and
                # falsely detect a "reopened" transition (e.g. completed → started).
                new_type = self.linear.get_workflow_state_type(team_id, name)
                if new_type:
                    self.db._conn.execute(
                        "UPDATE issue_state SET last_state_type=?, last_seen_at=datetime('now') WHERE issue_id=?",
                        (new_type, issue_id),
                    )
                    self.db._conn.commit()
        except Exception as exc:
            self.logger.error("Failed to set state '%s': %s", name, exc)

    def _get_state_name(self, key: str, fallback: str) -> str:
        return self.db.get_config(key) or fallback

    def cancel_job(self, job_id: int) -> bool:
        """Terminate the subprocess for a running job. Returns True if a signal was sent."""
        holder = self._proc_holders.get(job_id)
        if holder and "proc" in holder:
            try:
                holder["proc"].terminate()
                return True
            except Exception as exc:
                self.logger.warning("Failed to terminate job %d: %s", job_id, exc)
        return False

    async def reprocess_session(self, identifier: str) -> tuple[bool, str]:
        """Re-run post-processing (comment, state change, PR) for a session
        without re-running Claude.  Useful when Claude succeeded but
        post-processing crashed.  Skips comments if already posted, reuses
        existing PRs instead of creating duplicates."""
        session = self.db.get_latest_session_by_identifier(identifier)
        if not session:
            return False, "No session found"

        issue_state = self.db.get_issue_by_identifier(identifier)
        if not issue_state:
            return False, "No issue state found"

        issue_id = issue_state["issue_id"]
        team_id = issue_state["team_id"]
        session_dir = Path(session["session_dir"])

        # Read the stdout from the previous run
        stdout_path = session_dir / "stdout.txt"
        if not stdout_path.exists():
            return False, "No stdout.txt in session dir"
        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")

        try:
            issue = await self.linear.get_issue_details(issue_id)
        except Exception as exc:
            return False, f"Failed to fetch issue: {exc}"

        self.logger.info("[%s] Reprocessing session (post-processing only)", identifier)

        # Bump timestamp BEFORE changes to prevent re-triggers
        self._touch_issue_updated(issue_id)

        # Only post the summary comment if we haven't already (check existing comments)
        summary, hitl = self._summarize_result(stdout_text)
        existing_comments = issue.get("comments", {}).get("nodes", [])
        has_our_comment = any(self.is_own_comment(c.get("body", "")) for c in existing_comments)

        if summary and not has_our_comment:
            await self._post_comment(issue_id, summary)

        status = "done"
        if hitl:
            status = "awaiting_feedback"
            await self._set_hitl_state(issue_id, team_id)
        else:
            await self._set_named_state(issue_id, team_id, self._get_state_name("status_review", self.settings.review_state_name) or self._get_state_name("status_done", self.settings.done_state_name))
            # Only create PR if we don't already have one
            pr_url = session["pr_url"]
            if not pr_url:
                pr_url = await self._maybe_create_pr(issue, session_dir, identifier)
            if pr_url:
                run_id = session["run_id"]
                if run_id:
                    self.db.set_session_pr_url(run_id, pr_url)
                # Only post PR comment if not already posted
                pr_comment_exists = any(
                    pr_url in c.get("body", "") and self.is_own_comment(c.get("body", ""))
                    for c in existing_comments
                )
                if not pr_comment_exists:
                    await self._post_comment(issue_id, f"Pull request: {pr_url}")
                mapping = self.db.get_team_mapping(team_id)
                if mapping and mapping["auto_merge"]:
                    merged = await self._merge_github_pr(pr_url)
                    if merged:
                        self.logger.info("[%s] PR auto-merged: %s", identifier, pr_url)
                        await self._post_comment(issue_id, "PR automatically merged to main.")

        # Bump again after all changes
        self._touch_issue_updated(issue_id)

        now = datetime.now(timezone.utc).isoformat()
        self.db.upsert_session(session["run_id"] or 0, issue_id, identifier, status, str(session_dir), None, now)
        if session["run_id"]:
            self.db.complete_job(session["run_id"], True, None)

        self.logger.info("[%s] Reprocess complete (status: %s)", identifier, status)
        return True, f"Reprocessed — status: {status}"

    def cleanup_session(self, identifier: str) -> tuple[bool, str]:
        """Remove worktree, session files, and DB row for a ticket."""
        session = self.db.get_latest_session_by_identifier(identifier)
        if not session:
            return False, "No session found"
        if session["status"] == "running":
            return False, "Cannot clean up a running session"

        removed = []

        # Remove git worktree
        session_dir = Path(session["session_dir"])
        meta = read_worktree_meta(session_dir)
        if meta and meta.get("repo") and meta.get("worktree"):
            try:
                remove_worktree(Path(meta["repo"]), Path(meta["worktree"]))
                removed.append("worktree")
            except Exception as exc:
                self.logger.error("[%s] Failed to remove worktree: %s", identifier, exc)

        # Also try the default worktree path (covers cases where meta is missing)
        wt_path = Path(self.settings.worktree_root).resolve() / identifier
        if wt_path.exists():
            try:
                shutil.rmtree(str(wt_path), ignore_errors=True)
                if "worktree" not in removed:
                    removed.append("worktree")
            except Exception as exc:
                self.logger.error("[%s] Failed to delete worktree dir: %s", identifier, exc)

        # Remove session files
        identifier_dir = self.settings.data_path() / "sessions" / identifier
        if identifier_dir.exists():
            try:
                shutil.rmtree(str(identifier_dir), ignore_errors=True)
                removed.append("session files")
            except Exception as exc:
                self.logger.error("[%s] Failed to delete session dir: %s", identifier, exc)

        # Remove DB row
        try:
            self.db._conn.execute("DELETE FROM sessions WHERE identifier = ?", (identifier,))
            self.db._conn.commit()
            removed.append("db row")
        except Exception as exc:
            self.logger.error("[%s] Failed to delete session row: %s", identifier, exc)

        self.logger.info("[%s] Cleanup complete: %s", identifier, ", ".join(removed) or "nothing to remove")
        return True, f"Cleaned up: {', '.join(removed)}" if removed else (True, "Nothing to remove")

    async def enqueue_team_tickets(self, team_id: str) -> int:
        """Fetch all open tickets for a team and enqueue them as jobs.

        Returns the number of newly enqueued tickets.
        """
        mapping = self.db.get_team_mapping(team_id)
        if not mapping:
            self.logger.warning("enqueue_team_tickets: no mapping for team %s", team_id)
            return 0

        state_types = ["started", "unstarted", "backlog", "triage"]
        try:
            issues = await self.linear.get_team_issues(team_id, state_types=state_types, first=200)
        except Exception as exc:
            self.logger.error("enqueue_team_tickets: %s", exc)
            return 0

        # Sort by priority (1=urgent first, 0=no priority last)
        issues.sort(key=lambda i: (i.get("priority") or 999))

        count = 0
        for issue in issues:
            issue_id = issue["id"]
            identifier = issue["identifier"]

            # Ensure issue state is tracked
            state = issue.get("state") or {}
            self.db.upsert_issue_state(
                issue_id=issue_id,
                identifier=identifier,
                team_id=team_id,
                state_type=state.get("type"),
                state_name=state.get("name"),
                last_comment_at=None,
                last_seen_at=issue.get("updatedAt", ""),
                last_updated_at=issue.get("updatedAt", ""),
                title=issue.get("title"),
            )

            # enqueue_job already skips if a pending/running job exists
            self.db.enqueue_job(issue_id, identifier, team_id, "batch")
            count += 1

        self.logger.info("enqueue_team_tickets: enqueued %d tickets for team %s", count, team_id)
        return count

    async def _reaper_loop(self) -> None:
        """Resets jobs stuck in 'running' after a server crash or restart."""
        while not self.stop_event.is_set():
            await asyncio.sleep(300)  # check every 5 minutes
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.settings.stale_job_timeout_minutes)
                count = self.db.requeue_stale_jobs(cutoff.isoformat())
                if count:
                    self.logger.warning("Requeued %d stale job(s) stuck in running state", count)
            except Exception as exc:
                self.logger.error("Reaper error: %s", exc)
            # Periodically checkpoint WAL to prevent unbounded growth
            self.db.wal_checkpoint()

    async def _pr_status_loop(self) -> None:
        """Periodically check GitHub for PR merge status so the dashboard stays accurate."""
        while not self.stop_event.is_set():
            await asyncio.sleep(60)
            if not self.settings.github_token:
                continue
            try:
                open_sessions = self.db.get_open_pr_sessions()
                if not open_sessions:
                    continue
                import re
                headers = {
                    "Authorization": f"Bearer {self.settings.github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
                async with httpx.AsyncClient(timeout=15.0) as client:
                    for sess in open_sessions:
                        pr_url = sess["pr_url"]
                        m = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', pr_url)
                        if not m:
                            continue
                        owner, repo, pr_number = m.group(1), m.group(2), m.group(3)
                        try:
                            resp = await client.get(
                                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}",
                                headers=headers,
                            )
                            if resp.status_code == 200:
                                data = resp.json()
                                if data.get("merged"):
                                    self.db.set_pr_merged(pr_url)
                                    self.logger.info("PR status sync: %s is merged", pr_url)
                        except Exception:
                            pass  # individual check failure is fine
            except Exception as exc:
                self.logger.error("PR status loop error: %s", exc)

    async def _cleanup_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(3600)
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(days=self.settings.session_ttl_days)
                entries = self.db.cleanup_sessions(cutoff.isoformat())
                for entry in entries:
                    d = entry["session_dir"]
                    meta = read_worktree_meta(Path(d))
                    if meta and meta.get("repo") and meta.get("worktree"):
                        try:
                            remove_worktree(Path(meta["repo"]), Path(meta["worktree"]))
                        except Exception as exc:
                            self.logger.error("Failed to remove worktree %s: %s", meta, exc)
                    # Clean the entire identifier directory (covers all prior runs)
                    identifier_dir = self.settings.data_path() / "sessions" / entry["identifier"]
                    try:
                        shutil.rmtree(str(identifier_dir), ignore_errors=True)
                    except Exception as exc:
                        self.logger.error("Failed to delete session dir %s: %s", identifier_dir, exc)
                if entries:
                    self.logger.info("Cleaned %s sessions", len(entries))
            except Exception as exc:
                self.logger.error("Cleanup error: %s", exc)

    async def _maybe_create_pr(self, issue: dict[str, Any], session_dir: Path, identifier: str) -> str | None:
        """Push the worktree branch and open a GitHub PR if configured and commits exist."""
        if not self.settings.github_token:
            return None
        meta = read_worktree_meta(session_dir)
        if not meta:
            return None

        repo = Path(meta["repo"])
        worktree = Path(meta["worktree"])
        branch = f"ticket/{identifier}"

        base = get_default_branch(repo)
        try:
            base_ref = f"origin/{base}"
            count = await asyncio.to_thread(get_commit_count_vs_base, worktree, base_ref)
        except Exception:
            count = 0

        if count == 0:
            return None

        github_info = parse_github_remote(repo)
        if not github_info:
            return None
        owner, repo_name = github_info

        ssh_env = self._get_git_ssh_env()
        try:
            await asyncio.to_thread(push_branch, worktree, branch, ssh_env)
        except GitPushError as exc:
            error_hints = {
                "auth": "Check that the SSH deploy key is added to the GitHub repo (Settings > Deploy keys with write access).",
                "host_key": "SSH host key verification failed. Ensure SSH_KEY_DIR is configured and known_hosts is populated.",
                "branch_exists": "Branch conflict on remote — force-with-lease also failed.",
                "network": "Network error reaching remote. Check connectivity and remote URL.",
            }
            hint = error_hints.get(exc.error_type, "")
            self.logger.error("[%s] Git push FAILED (%s): %s. %s", identifier, exc.error_type, exc, hint)
            return None
        except Exception as exc:
            self.logger.error("[%s] Git push FAILED (unexpected): %s", identifier, exc)
            return None

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Check for existing open PR on this branch before creating a new one
                existing_resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo_name}/pulls",
                    headers={
                        "Authorization": f"Bearer {self.settings.github_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    params={"head": f"{owner}:{branch}", "state": "open"},
                )
                if existing_resp.status_code == 200:
                    existing_prs = existing_resp.json()
                    if existing_prs:
                        existing_url = existing_prs[0].get("html_url")
                        self.logger.info("[%s] PR already exists: %s", identifier, existing_url)
                        return existing_url

                resp = await client.post(
                    f"https://api.github.com/repos/{owner}/{repo_name}/pulls",
                    headers={
                        "Authorization": f"Bearer {self.settings.github_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json={
                        "title": f"{identifier}: {issue['title']}",
                        "body": f"Resolves {identifier}\n\nAutomatically generated by ClaudeWrapper.",
                        "head": branch,
                        "base": base,
                    },
                )
                if resp.status_code in (200, 201):
                    return resp.json().get("html_url")
                # 422 often means PR already exists (e.g. from a different state)
                if resp.status_code == 422:
                    self.logger.info("[%s] PR creation returned 422 (likely already exists): %s", identifier, resp.text[:200])
                    return None
                self.logger.warning("GitHub PR creation failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            self.logger.warning("GitHub PR creation error: %s", exc)

        return None

    async def _claude_fix_and_merge(
        self,
        identifier: str,
        claude_session_id: str,
        pr_url: str,
        session_dir: Path,
        workdir: Path | None,
        ssh_env: dict[str, str] | None,
        job_id: int,
    ) -> bool:
        """Resume a Claude session to resolve merge issues and merge the PR.

        Returns True if the PR was successfully merged after Claude's intervention.
        """
        merge_prompt = (
            f"The pull request {pr_url} could not be automatically merged. "
            "This usually means there are merge conflicts with the base branch, "
            "or required checks are failing.\n\n"
            "Please:\n"
            "1. Pull the latest changes from the base branch and rebase or merge them into your branch.\n"
            "2. Resolve any merge conflicts.\n"
            "3. Run tests to make sure everything still passes.\n"
            "4. Push the updated branch.\n\n"
            "Do NOT create a new PR — just update the existing branch so the open PR becomes mergeable."
        )

        self.logger.info("[%s] Calling Claude to fix merge (session %s)", identifier, claude_session_id)

        proc_holder: dict = {}
        self._proc_holders[job_id] = proc_holder
        try:
            result = await asyncio.to_thread(
                self.runner.run, identifier, merge_prompt, session_dir, workdir,
                proc_holder, claude_session_id, ssh_env,
            )
        finally:
            self._proc_holders.pop(job_id, None)

        self.logger.info(
            "[%s] Claude merge-fix exited with code %d (stdout: %d bytes)",
            identifier, result.returncode, len(result.stdout),
        )

        # Update session ID in case Claude created a new one
        new_sid = getattr(result, "claude_session_id", None)
        if new_sid:
            self.db.set_claude_session_id(job_id, new_sid)

        if result.returncode != 0:
            self.logger.warning("[%s] Claude merge-fix failed (exit %d)", identifier, result.returncode)
            return False

        # Try merging again now that Claude has (hopefully) fixed things
        merged = await self._merge_github_pr(pr_url)
        return merged

    async def _merge_github_pr(self, pr_url: str) -> bool:
        """Merge a GitHub PR via API.

        Strategy: first wait for GitHub to compute mergeability by polling the
        PR endpoint (``mergeable`` flips from ``null`` to ``true``/``false``),
        then attempt the merge.  This avoids blind 405 retries.
        """
        if not self.settings.github_token:
            return False
        import re
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
                # Phase 1: wait for mergeability to be computed
                poll_delays = [3, 5, 10, 15, 20, 30]  # up to ~83s total
                mergeable = None
                for i, delay in enumerate(poll_delays):
                    pr_resp = await client.get(pr_api, headers=headers)
                    if pr_resp.status_code == 200:
                        pr_data = pr_resp.json()
                        mergeable = pr_data.get("mergeable")
                        mergeable_state = pr_data.get("mergeable_state", "unknown")
                        if mergeable is not None:
                            self.logger.info("PR %s mergeable=%s state=%s (poll %d/%d)",
                                             pr_url, mergeable, mergeable_state, i + 1, len(poll_delays))
                            break
                    self.logger.info("PR mergeability not yet computed, waiting %ds (%d/%d)",
                                     delay, i + 1, len(poll_delays))
                    await asyncio.sleep(delay)

                if mergeable is False:
                    mergeable_state = pr_data.get("mergeable_state", "unknown")
                    self.logger.warning("PR is not mergeable (state=%s): %s", mergeable_state, pr_url)
                    return False

                # Phase 2: attempt merge (with a short retry for race conditions)
                merge_delays = [5, 15, 30]
                for attempt in range(len(merge_delays) + 1):
                    resp = await client.put(
                        f"{pr_api}/merge",
                        headers=headers,
                        json={"merge_method": "squash"},
                    )
                    if resp.status_code == 200:
                        self.db.set_pr_merged(pr_url)
                        return True
                    if resp.status_code == 405 and attempt < len(merge_delays):
                        self.logger.info("PR merge returned 405, retrying in %ds (%d/%d)",
                                         merge_delays[attempt], attempt + 1, len(merge_delays))
                        await asyncio.sleep(merge_delays[attempt])
                        continue
                    self.logger.warning("GitHub merge failed: %s %s", resp.status_code, resp.text[:200])
                    return False
        except Exception as exc:
            self.logger.warning("GitHub merge error: %s", exc)
        return False

    async def merge_session_pr(self, run_id: int) -> tuple[bool, str]:
        """Merge the PR for a session.  Falls back to resuming Claude to fix
        merge conflicts when the direct merge fails.  Returns (ok, message)."""
        session = self.db.get_session_by_run_id(run_id)
        if not session or not session["pr_url"]:
            return False, "No PR URL found for this session"

        pr_url = session["pr_url"]
        ok = await self._merge_github_pr(pr_url)
        if ok:
            return True, "Merged successfully"

        # Direct merge failed — try Claude fallback
        if self.settings.test_mode:
            return False, "Merge failed (test mode, Claude fallback skipped)"

        identifier = session["identifier"]
        claude_sid = self.db.get_last_claude_session_id(identifier)
        if not claude_sid:
            return False, "Merge failed and no Claude session to resume"

        session_dir = Path(session["session_dir"])
        ssh_env = self._get_git_ssh_env()

        # Determine workdir from worktree meta or team mapping
        workdir = None
        meta = read_worktree_meta(session_dir)
        if meta and meta.get("worktree"):
            wt = Path(meta["worktree"])
            if wt.exists():
                workdir = wt
        if not workdir:
            issue_state = self.db.get_issue_by_identifier(identifier)
            if issue_state:
                mapping = self.db.get_team_mapping(issue_state["team_id"])
                if mapping and mapping["local_path"]:
                    workdir = Path(mapping["local_path"]).resolve()

        self.logger.info("[%s] Manual merge failed, resuming Claude to fix", identifier)
        merge_ok = await self._claude_fix_and_merge(
            identifier, claude_sid, pr_url, session_dir, workdir, ssh_env, run_id,
        )
        if merge_ok:
            return True, "Merged after Claude resolved conflicts"
        return False, "Merge failed — Claude could not resolve conflicts"
