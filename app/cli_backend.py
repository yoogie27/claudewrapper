"""Pluggable CLI backend abstraction.

Each backend wraps a coding-agent CLI tool (Claude Code, Gemini CLI, Codex CLI)
and knows how to format commands, run subprocesses, and parse output into a
normalized result. The orchestrator selects a backend per task.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RunResult:
    """Normalized result from any CLI backend."""
    returncode: int
    stdout: str
    stderr: str
    summary: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    session_id: str | None = None


class CliBackend(ABC):
    """Base class for CLI coding-agent backends."""
    name: str = ""
    display_name: str = ""

    def __init__(self) -> None:
        self.model: str = ""
        self.fallback_model: str = ""

    @abstractmethod
    def format_command(self, prompt_path: Path, workdir: Path | None,
                       prompt_text: str, resume_session_id: str | None = None) -> list[str]: ...

    @abstractmethod
    def parse_result(self, stdout: str) -> RunResult: ...

    def prepare_workdir(self, workdir: Path) -> None:
        """Optional hook to set up workdir before run (e.g. MCP config)."""
        pass

    @property
    def _uses_stdin(self) -> bool:
        return False

    def run(self, identifier: str, prompt_text: str, session_dir: Path,
            workdir: Path | None, proc_holder: dict | None = None,
            resume_session_id: str | None = None,
            extra_env: dict[str, str] | None = None) -> RunResult:
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.txt"
        prompt_path.write_text(prompt_text, encoding="utf-8")
        stdout_path = session_dir / "stdout.txt"
        stderr_path = session_dir / "stderr.txt"

        if workdir:
            self.prepare_workdir(workdir)

        cmd = self.format_command(prompt_path, workdir, prompt_text, resume_session_id)
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc_env = {**os.environ, **(extra_env or {})} if extra_env else None

        with open(stderr_path, "w", encoding="utf-8") as err_f:
            stdin_pipe = subprocess.PIPE if self._uses_stdin else subprocess.DEVNULL
            proc = subprocess.Popen(
                cmd, cwd=workdir, stdin=stdin_pipe,
                stdout=subprocess.PIPE, stderr=err_f,
                encoding="utf-8", errors="replace",
                creationflags=cflags, bufsize=1,
                env=proc_env,
            )
            if proc_holder is not None:
                proc_holder["proc"] = proc
            if self._uses_stdin:
                proc.stdin.write(prompt_text)
                proc.stdin.close()

            disk_full = False
            stdout_lines: list[str] = []
            with open(stdout_path, "w", encoding="utf-8") as out_f:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    stdout_lines.append(line)
                    if not disk_full:
                        try:
                            out_f.write(line)
                            out_f.flush()
                        except OSError:
                            disk_full = True
            proc.wait()

        # Use collected lines instead of re-reading the entire file from disk
        stdout = "".join(stdout_lines)
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        result = self.parse_result(stdout)
        result.returncode = proc.returncode
        result.stdout = stdout
        result.stderr = stderr
        # Extract session_id from stderr if not found in stdout
        # (Codex prints "To continue this session, run codex resume <ID>" to stderr)
        if not result.session_id and stderr:
            m = re.search(r'codex resume\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', stderr)
            if m:
                result.session_id = m.group(1)
        return result


class _SafeSubs(dict):
    def __missing__(self, key: str) -> str:
        return ""


# ═══════════════════════════════════════════════════════════════════
# Claude Code Backend
# ═══════════════════════════════════════════════════════════════════

class ClaudeBackend(CliBackend):
    name = "claude"
    display_name = "Claude Code"

    def __init__(self, command_template: str = "claude -p --prompt-file {prompt_path} --dangerously-skip-permissions --output-format stream-json",
                 prompt_via: str = "prompt_file", prompt_arg: str = "--prompt") -> None:
        super().__init__()
        self.command_template = command_template
        self.prompt_via = prompt_via
        self.prompt_arg = prompt_arg
        self.mcp_config: dict | None = None

    @property
    def _uses_stdin(self) -> bool:
        return self.prompt_via == "stdin"

    def format_command(self, prompt_path: Path, workdir: Path | None,
                       prompt_text: str, resume_session_id: str | None = None) -> list[str]:
        subs = {"prompt_path": str(prompt_path), "prompt_text": prompt_text.replace('"', '\\"'),
                "workdir": str(workdir) if workdir else "", "session_id": resume_session_id or ""}
        rendered = self.command_template.format_map(_SafeSubs(subs))
        rendered = re.sub(r'--resume\s+""\s*', '', rendered)
        rendered = re.sub(r"--resume\s+''\s*", '', rendered)
        rendered = re.sub(r'--session\s+""\s*', '', rendered)
        rendered = re.sub(r'--output-format\s+json\b', '--output-format stream-json', rendered)
        if 'stream-json' in rendered and '--verbose' not in rendered:
            rendered += ' --verbose'
        cmd = shlex.split(rendered)
        if self.model and "--model" not in cmd:
            cmd.extend(["--model", self.model])
        if self.fallback_model and "--fallback-model" not in cmd:
            cmd.extend(["--fallback-model", self.fallback_model])
        if resume_session_id and "--resume" not in cmd:
            cmd.extend(["--resume", resume_session_id])
        if self.prompt_via == "arg":
            cmd.extend([self.prompt_arg, prompt_text])
        return cmd

    def prepare_workdir(self, workdir: Path) -> None:
        if self.mcp_config:
            self._write_mcp_config(workdir)

    def parse_result(self, stdout: str) -> RunResult:
        text = (stdout or "").strip()
        if not text:
            return RunResult(returncode=-1, stdout="", stderr="", summary="No output captured.")
        result = RunResult(returncode=0, stdout=stdout, stderr="")
        lines = text.splitlines()

        # Single forward pass: collect assistant text and session_id as we go.
        # The "result" line is always last, so we check in reverse only for that.
        assistant_text: list[str] = []
        session_id: str | None = None
        uuid_pat = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                if not isinstance(obj, dict):
                    continue
                # Extract session_id early (appears in first few lines)
                if not session_id:
                    for key in ("session_id", "sessionId"):
                        val = obj.get(key, "")
                        if val and re.fullmatch(uuid_pat, val):
                            session_id = val
                            break
                if obj.get("type") == "assistant":
                    content = (obj.get("message") or {}).get("content", [])
                    for block in (content if isinstance(content, list) else []):
                        if block.get("type") == "text":
                            assistant_text.append(block["text"])
            except (json.JSONDecodeError, ValueError):
                continue

        # Extract cost/usage from the "result" event (last non-empty JSON line)
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict) and obj.get("type") == "result":
                    result.cost_usd = obj.get("total_cost_usd", 0.0) or 0.0
                    usage = obj.get("usage", {}) or {}
                    result.input_tokens = usage.get("input_tokens", 0) or 0
                    result.output_tokens = usage.get("output_tokens", 0) or 0
                    result.model = obj.get("model", "") or ""
                break
            except (json.JSONDecodeError, ValueError):
                continue

        # Use the FULL assistant text (all text blocks from the conversation),
        # not the short "result" summary which often truncates the real output.
        if assistant_text:
            result.summary = "\n".join(assistant_text).strip()[:10000]
        else:
            # Fallback: try result summary, then raw output
            for line in reversed(lines):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    if isinstance(obj, dict) and obj.get("type") == "result":
                        raw = obj.get("result", "")
                        if isinstance(raw, list):
                            result.summary = "".join(c.get("text", "") for c in raw if c.get("type") == "text").strip()
                        else:
                            result.summary = str(raw).strip()
                        break
                except (json.JSONDecodeError, ValueError):
                    continue
            if not result.summary:
                result.summary = text[:5000]
        result.session_id = session_id
        return result

    @staticmethod
    def _extract_session_id(stdout: str) -> str | None:
        uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    for key in ("session_id", "sessionId"):
                        val = obj.get(key, "")
                        if val and re.fullmatch(uuid_pattern, val):
                            return val
            except (json.JSONDecodeError, ValueError):
                continue
        m = re.search(r'session[_\-]?[iI]d["\s:]+["\']?(' + uuid_pattern + r')', stdout)
        return m.group(1) if m else None

    def _write_mcp_config(self, workdir: Path) -> None:
        if not self.mcp_config:
            return
        servers = self.mcp_config.get("mcpServers", {})
        if not servers:
            return
        config_path = Path.home() / ".claude.json"
        norm_path = str(workdir).replace("\\", "/")
        lock_path = config_path.with_suffix(".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "w") as lock_f:
                try:
                    import fcntl
                    fcntl.flock(lock_f, fcntl.LOCK_EX)
                except (ImportError, OSError):
                    pass
                existing: dict = {}
                if config_path.exists():
                    existing = json.loads(config_path.read_text(encoding="utf-8"))
                projects = existing.setdefault("projects", {})
                proj = projects.setdefault(norm_path, {})
                mcp = proj.setdefault("mcpServers", {})
                changed = False
                for name, cfg in servers.items():
                    if name not in mcp:
                        mcp[name] = cfg
                        changed = True
                if changed:
                    config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# Gemini CLI Backend
# ═══════════════════════════════════════════════════════════════════

class GeminiBackend(CliBackend):
    name = "gemini"
    display_name = "Gemini CLI"

    def __init__(self, command_template: str = "gemini -y -p {prompt_path}") -> None:
        super().__init__()
        self.command_template = command_template

    def format_command(self, prompt_path: Path, workdir: Path | None,
                       prompt_text: str, resume_session_id: str | None = None) -> list[str]:
        subs = {"prompt_path": str(prompt_path), "prompt_text": prompt_text.replace('"', '\\"'),
                "workdir": str(workdir) if workdir else "", "session_id": resume_session_id or ""}
        cmd = shlex.split(self.command_template.format_map(_SafeSubs(subs)))
        if self.model and "--model" not in cmd:
            cmd.extend(["--model", self.model])
        return cmd

    def parse_result(self, stdout: str) -> RunResult:
        text = (stdout or "").strip()
        if not text:
            return RunResult(returncode=-1, stdout="", stderr="", summary="No output captured.")
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    summary = obj.get("result", "") or obj.get("text", "") or obj.get("response", "")
                    if summary:
                        return RunResult(returncode=0, stdout=stdout, stderr="",
                                         summary=str(summary).strip()[:5000], model=obj.get("model", ""))
            except (json.JSONDecodeError, ValueError):
                continue
        return RunResult(returncode=0, stdout=stdout, stderr="", summary=text[:5000])


# ═══════════════════════════════════════════════════════════════════
# Codex CLI Backend
# ═══════════════════════════════════════════════════════════════════

class CodexBackend(CliBackend):
    name = "codex"
    display_name = "Codex CLI"

    # Codex exec: prompt piped via stdin (no arg = reads stdin as prompt).
    # --json outputs JSONL events for streaming.
    # --color never prevents ANSI escapes.
    # Resume: codex exec resume <SESSION_ID> with follow-up prompt on stdin.
    def __init__(self, command_template: str = "codex exec --json --color never --dangerously-bypass-approvals-and-sandbox") -> None:
        super().__init__()
        self.command_template = command_template

    @property
    def _uses_stdin(self) -> bool:
        return True

    def prepare_workdir(self, workdir: Path) -> None:
        """Ensure Codex trusts the workdir in its config.toml."""
        config_dir = Path.home() / ".codex"
        config_path = config_dir / "config.toml"
        norm_path = str(workdir).replace("\\", "/")
        section = f'[projects."{norm_path}"]\ntrust_level = "trusted"\n'

        try:
            config_dir.mkdir(parents=True, exist_ok=True)
            existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
            if f'[projects."{norm_path}"]' not in existing:
                with open(config_path, "a", encoding="utf-8") as f:
                    if existing and not existing.endswith("\n"):
                        f.write("\n")
                    f.write(f"\n{section}")
        except Exception:
            pass

    def format_command(self, prompt_path: Path, workdir: Path | None,
                       prompt_text: str, resume_session_id: str | None = None) -> list[str]:
        if resume_session_id:
            # Resume: codex exec resume <SESSION_ID> --json --color never ...
            # Follow-up prompt is still piped via stdin.
            base = self.command_template.replace("codex exec", f"codex exec resume {resume_session_id}", 1)
            cmd = shlex.split(base)
        else:
            subs = {"prompt_path": str(prompt_path), "prompt_text": prompt_text.replace('"', '\\"'),
                    "workdir": str(workdir) if workdir else "", "session_id": ""}
            cmd = shlex.split(self.command_template.format_map(_SafeSubs(subs)))
        if self.model and "--model" not in cmd:
            cmd.extend(["--model", self.model])
        return cmd

    def parse_result(self, stdout: str) -> RunResult:
        """Parse Codex JSONL output.

        Codex --json emits typed events:
        - item.completed with item.type == "agent_message" → assistant text
        - turn.completed → usage stats
        - turn.failed → error
        """
        text = (stdout or "").strip()
        if not text:
            return RunResult(returncode=-1, stdout="", stderr="", summary="No output captured.")

        result = RunResult(returncode=0, stdout=stdout, stderr="")
        agent_texts: list[str] = []
        session_id: str | None = None
        uuid_pat = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    continue
                evt_type = obj.get("type", "")

                # Extract session_id from thread.started event
                if evt_type == "thread.started" and not session_id:
                    tid = obj.get("thread_id", "")
                    if tid and re.fullmatch(uuid_pat, tid):
                        session_id = tid

                if evt_type in ("item.completed", "item.updated"):
                    item = obj.get("item", {})
                    if item.get("type") == "agent_message":
                        msg_text = item.get("text", "")
                        if msg_text:
                            agent_texts.append(msg_text)

                elif evt_type == "turn.completed":
                    usage = obj.get("usage", {})
                    result.input_tokens = usage.get("input_tokens", 0) or 0
                    result.output_tokens = usage.get("output_tokens", 0) or 0

                elif evt_type == "turn.failed":
                    error = obj.get("error", {})
                    err_msg = error.get("message", "") if isinstance(error, dict) else str(error)
                    if err_msg:
                        agent_texts.append(f"**Error:** {err_msg}")

            except (json.JSONDecodeError, ValueError):
                # Check non-JSON lines for session ID (stderr merged or plain text)
                m = re.search(r'codex resume\s+(' + uuid_pat + r')', line)
                if m and not session_id:
                    session_id = m.group(1)
                continue

        if agent_texts:
            result.summary = "\n".join(agent_texts).strip()[:10000]
        else:
            result.summary = text[:5000]
        result.session_id = session_id
        return result


# ═══════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════

BACKENDS: dict[str, type[CliBackend]] = {
    "claude": ClaudeBackend, "gemini": GeminiBackend, "codex": CodexBackend,
}

BACKEND_CHOICES = [
    {"value": "claude", "label": "Claude Code", "default_cmd": "claude -p --prompt-file {prompt_path} --dangerously-skip-permissions --output-format stream-json"},
    {"value": "gemini", "label": "Gemini CLI", "default_cmd": "gemini -y -p {prompt_path}"},
    {"value": "codex", "label": "Codex CLI", "default_cmd": "codex exec --json --color never --dangerously-bypass-approvals-and-sandbox"},
]


def create_backend(name: str, command_template: str = "", **kwargs: Any) -> CliBackend:
    cls = BACKENDS.get(name)
    if not cls:
        raise ValueError(f"Unknown backend: {name!r}. Available: {list(BACKENDS)}")
    init_kwargs: dict[str, Any] = {}
    if command_template:
        init_kwargs["command_template"] = command_template
    if name == "claude":
        for k in ("prompt_via", "prompt_arg"):
            if k in kwargs:
                init_kwargs[k] = kwargs[k]
    return cls(**init_kwargs)
