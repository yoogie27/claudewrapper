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
    ensure_worktree,
    get_commit_count_vs_base,
    get_default_branch,
    parse_github_remote,
    push_branch,
    read_worktree_meta,
    remove_worktree,
    write_worktree_meta,
)

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

    # Hidden marker appended to every comment we post.
    # Used to identify (and ignore) our own comments during polling.
    COMMENT_MARKER = "<!-- claudewrapper -->"

    @staticmethod
    def is_own_comment(body: str) -> bool:
        return Orchestrator.COMMENT_MARKER in (body or "")

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

    async def _poll_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self._poll_once()
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                self.logger.warning("Poll: network error (%s)", exc)
            except LinearAPIError as exc:
                self.logger.error("Poll: %s", exc)
            except Exception as exc:
                self.logger.exception("Poll: unexpected error: %s", exc)
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def _poll_once(self) -> None:
        if not self.settings.linear_api_key:
            self.logger.error("LINEAR_API_KEY missing. Polling disabled.")
            return
        last_poll = self.db.get_config("last_poll")
        if not last_poll:
            last_poll = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()

        issues = await self.linear.get_issues_updated_since(last_poll, first=self.settings.max_issues_per_poll)
        now_iso = datetime.now(timezone.utc).isoformat()

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

    async def _worker_loop(self, worker_id: str) -> None:
        while not self.stop_event.is_set():
            job = self.db.dequeue_job(worker_id, max_per_team=self.settings.max_concurrent_per_team)
            if not job:
                await asyncio.sleep(2)
                continue

            job_id = job["id"]
            issue_id = job["issue_id"]
            identifier = job["identifier"]
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

                if self.settings.use_git_worktrees and workdir:
                    worktree_root = Path(self.settings.worktree_root)
                    # Start fresh from latest main for new jobs so there are no
                    # conflicts with previously merged branches.  Retries keep
                    # the existing worktree state (preserves prior work).
                    fresh = reason not in ("retry", "retry_job", "feedback")
                    worktree_path = ensure_worktree(workdir, worktree_root, identifier, fresh=fresh)
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
                            proc_holder, resume_id,
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
                    await self._set_named_state(issue_id, team_id, self._get_state_name("status_error", self.settings.error_state_name))

                if ok:
                    self.logger.info("[%s] Completed successfully (status: %s)", identifier, status)

                # Bump last_updated_at so the poller ignores the changes we just made
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
                self.db.upsert_session(job_id, issue_id, identifier, "failed", str(session_dir), None, datetime.now(timezone.utc).isoformat())
                self.db.complete_job(job_id, False, f"Network error: {exc}")
            except LinearAPIError as exc:
                self.logger.error("[%s] Linear API error (job #%d): %s", identifier, job_id, exc)
                self.db.upsert_session(job_id, issue_id, identifier, "failed", str(session_dir), None, datetime.now(timezone.utc).isoformat())
                self.db.complete_job(job_id, False, str(exc))
            except Exception as exc:
                self.logger.exception("[%s] Unexpected error (job #%d): %s", identifier, job_id, exc)
                self.db.upsert_session(job_id, issue_id, identifier, "failed", str(session_dir), None, datetime.now(timezone.utc).isoformat())
                self.db.complete_job(job_id, False, str(exc))

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

        comments = issue.get("comments", {}).get("nodes", [])
        comment_text = "\n\n".join([
            f"[{c.get('createdAt', '')}] {(c.get('user') or {}).get('name', 'Unknown')}: {c.get('body', '')}"
            for c in comments
        ]) or "(no comments)"

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
        else:
            strategy = (
                "This is a **feature/task**. Plan before implementing:\n"
                "1. Identify the files and modules that need changes.\n"
                "2. Check existing patterns in the codebase for consistency.\n"
                "3. Implement incrementally, testing as you go.\n"
                "4. Run existing tests to confirm no regressions."
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

You are working on ticket **{issue['identifier']}**: "{issue['title']}".
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

{issue.get("description") or "(no description)"}

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
- Run tests after your changes (`npm test`, `pytest`, or whatever the project uses).
- If relevant tests don't exist, add basic coverage for your changes.
- Keep changes focused — only modify what's needed for this ticket.

## Completion

When you finish, write a structured comment on the Linear ticket:

**Summary:** What you changed and why.
**Files modified:** Key files affected.
**Testing:** How you validated the changes.
**Notes:** Anything important for reviewers or follow-up.

Commit with: `fix: {issue['title']} ({issue['identifier']})` for bugs, or `feat: {issue['title']} ({issue['identifier']})` for features.
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
        post-processing crashed."""
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

        summary, hitl = self._summarize_result(stdout_text)
        if summary:
            await self._post_comment(issue_id, summary)

        status = "done"
        if hitl:
            status = "awaiting_feedback"
            await self._set_hitl_state(issue_id, team_id)
        else:
            await self._set_named_state(issue_id, team_id, self._get_state_name("status_review", self.settings.review_state_name) or self._get_state_name("status_done", self.settings.done_state_name))
            pr_url = await self._maybe_create_pr(issue, session_dir, identifier)
            if pr_url:
                run_id = session["run_id"]
                if run_id:
                    self.db.set_session_pr_url(run_id, pr_url)
                await self._post_comment(issue_id, f"Pull request: {pr_url}")
                mapping = self.db.get_team_mapping(team_id)
                if mapping and mapping["auto_merge"]:
                    merged = await self._merge_github_pr(pr_url)
                    if merged:
                        self.logger.info("[%s] PR auto-merged: %s", identifier, pr_url)
                        await self._post_comment(issue_id, "PR automatically merged to main.")

        # Bump last_updated_at so the poller ignores the changes we just made
        self._touch_issue_updated(issue_id)

        now = datetime.now(timezone.utc).isoformat()
        self.db.upsert_session(session["run_id"] or 0, issue_id, identifier, status, str(session_dir), None, now)
        if session["run_id"]:
            self.db.complete_job(session["run_id"], True, None)

        self.logger.info("[%s] Reprocess complete (status: %s)", identifier, status)
        return True, f"Reprocessed — status: {status}"

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

        try:
            await asyncio.to_thread(push_branch, worktree, branch)
        except Exception as exc:
            self.logger.warning("Failed to push branch %s: %s", branch, exc)
            return None

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
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
                self.logger.warning("GitHub PR creation failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            self.logger.warning("GitHub PR creation error: %s", exc)

        return None

    async def _merge_github_pr(self, pr_url: str) -> bool:
        """Merge a GitHub PR via API. Returns True on success."""
        if not self.settings.github_token:
            return False
        import re
        m = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', pr_url)
        if not m:
            return False
        owner, repo, pr_number = m.group(1), m.group(2), m.group(3)
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.put(
                    f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
                    headers={
                        "Authorization": f"Bearer {self.settings.github_token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                    json={"merge_method": "squash"},
                )
                if resp.status_code == 200:
                    return True
                self.logger.warning("GitHub merge failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            self.logger.warning("GitHub merge error: %s", exc)
        return False

    async def merge_session_pr(self, run_id: int) -> tuple[bool, str]:
        """Merge the PR for a session. Returns (ok, message)."""
        session = self.db.get_session_by_run_id(run_id)
        if not session or not session["pr_url"]:
            return False, "No PR URL found for this session"
        ok = await self._merge_github_pr(session["pr_url"])
        if ok:
            return True, "Merged successfully"
        return False, "Merge failed — check logs or merge manually"
