from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


class _SafeSubs(dict):
    """Dict that returns empty string for missing keys instead of raising KeyError.

    Prevents crashes when CLAUDE_COMMAND_TEMPLATE contains unknown placeholders.
    """
    def __missing__(self, key: str) -> str:
        return ""


class ClaudeRunner:
    def __init__(self, command_template: str, prompt_via: str, prompt_arg: str, workdir_mode: str) -> None:
        self.command_template = command_template
        self.prompt_via = prompt_via
        self.prompt_arg = prompt_arg
        self.workdir_mode = workdir_mode
        # MCP config to inject into workdir (set by orchestrator)
        self.mcp_config: dict | None = None
        # Model selection (set by orchestrator before each run)
        self.model: str = ""
        self.fallback_model: str = ""

    def _format_command(self, prompt_path: Path, workdir: Path | None, prompt_text: str, resume_session_id: str | None = None) -> list[str]:
        # Build substitution dict — provide all possible placeholders so old
        # .env templates with {session_id} etc. don't crash with KeyError.
        subs = {
            "prompt_path": str(prompt_path),
            "prompt_text": prompt_text.replace('"', '\\"'),
            "workdir": str(workdir) if workdir else "",
            "session_id": resume_session_id or "",
        }
        rendered = self.command_template.format_map(_SafeSubs(subs))

        # Strip out empty --resume "" left over from old templates
        rendered = re.sub(r'--resume\s+""\s*', '', rendered)
        rendered = re.sub(r"--resume\s+''\s*", '', rendered)
        rendered = re.sub(r'--session\s+""\s*', '', rendered)

        # Force stream-json instead of plain json — plain json emits a single
        # blob at exit which makes real-time streaming impossible.
        rendered = re.sub(r'--output-format\s+json\b', '--output-format stream-json', rendered)

        # stream-json requires --verbose when using --print (-p)
        if 'stream-json' in rendered and '--verbose' not in rendered:
            rendered = rendered + ' --verbose'

        cmd = shlex.split(rendered)
        # Inject --model and --fallback-model if configured (and not already in template)
        if self.model and "--model" not in cmd:
            cmd.extend(["--model", self.model])
        if self.fallback_model and "--fallback-model" not in cmd:
            cmd.extend(["--fallback-model", self.fallback_model])
        # Append --resume if we have a prior Claude session UUID to continue
        # (only if not already present from the template)
        if resume_session_id and "--resume" not in cmd:
            cmd.extend(["--resume", resume_session_id])
        return cmd

    def run(
        self,
        identifier: str,
        prompt_text: str,
        session_dir: Path,
        workdir: Path | None,
        proc_holder: dict | None = None,
        resume_session_id: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        session_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = session_dir / "prompt.txt"
        prompt_path.write_text(prompt_text, encoding="utf-8")

        stdout_path = session_dir / "stdout.txt"
        stderr_path = session_dir / "stderr.txt"

        # Inject MCP config into workdir so Claude auto-discovers it
        if self.mcp_config and workdir:
            self._write_mcp_config(workdir)

        cmd = self._format_command(prompt_path, workdir, prompt_text, resume_session_id)
        if self.prompt_via == "arg":
            cmd = cmd + [self.prompt_arg, prompt_text]

        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc_env = {**os.environ, **extra_env} if extra_env else None

        # Use PIPE for stdout so we can flush each line to disk immediately.
        # Writing directly to a file (stdout=file) buffers on Windows and
        # prevents the SSE log endpoint from streaming output in real time.
        with open(stderr_path, "w", encoding="utf-8") as err_f:
            stdin_pipe = subprocess.PIPE if self.prompt_via == "stdin" else None
            proc = subprocess.Popen(
                cmd, cwd=workdir, stdin=stdin_pipe,
                stdout=subprocess.PIPE, stderr=err_f,
                encoding="utf-8", errors="replace",
                creationflags=cflags, bufsize=1,  # line-buffered
                env=proc_env,
            )
            if proc_holder is not None:
                proc_holder["proc"] = proc

            if self.prompt_via == "stdin":
                proc.stdin.write(prompt_text)
                proc.stdin.close()

            # Read stdout line by line, flush each to disk immediately.
            # Use readline() instead of iterating — the iterator uses an
            # internal read buffer that delays output on Windows.
            disk_full = False
            with open(stdout_path, "w", encoding="utf-8") as out_f:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    if not disk_full:
                        try:
                            out_f.write(line)
                            out_f.flush()
                        except OSError:
                            disk_full = True
                            # Stop writing but keep draining stdout so the
                            # subprocess doesn't block on a full pipe.
            proc.wait()

        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

        # Extract Claude session UUID from output for future --resume
        result.claude_session_id = self._extract_session_id(stdout)
        return result

    @staticmethod
    def _extract_session_id(stdout: str) -> str | None:
        """Extract Claude session UUID from stream-json or json output."""
        # stream-json: look for {"type":"system","sessionId":"<uuid>",...}
        # json: look for {"session_id":"<uuid>",...}
        uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                # stream-json format
                if isinstance(obj, dict):
                    for key in ("session_id", "sessionId"):
                        val = obj.get(key, "")
                        if val and re.fullmatch(uuid_pattern, val):
                            return val
            except (json.JSONDecodeError, ValueError):
                continue
        # Fallback: scan for any UUID-like string after "session" keyword
        m = re.search(r'session[_\-]?[iI]d["\s:]+["\']?(' + uuid_pattern + r')', stdout)
        if m:
            return m.group(1)
        return None

    def _write_mcp_config(self, workdir: Path) -> None:
        """Ensure MCP servers are configured for the workspace in ~/.claude.json.

        Claude Code stores project-scoped MCP config in ~/.claude.json under
        projects[path].mcpServers (this is where `claude mcp add` writes).
        """
        if not self.mcp_config:
            return
        servers = self.mcp_config.get("mcpServers", {})
        if not servers:
            return

        config_path = Path.home() / ".claude.json"
        norm_path = str(workdir).replace("\\", "/")

        try:
            existing: dict = {}
            if config_path.exists():
                existing = json.loads(config_path.read_text(encoding="utf-8"))

            projects = existing.setdefault("projects", {})
            proj = projects.setdefault(norm_path, {})
            mcp = proj.setdefault("mcpServers", {})

            # Only add servers that aren't already configured
            changed = False
            for name, cfg in servers.items():
                if name not in mcp:
                    mcp[name] = cfg
                    changed = True

            if changed:
                config_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        except Exception:
            pass  # Don't fail the run if MCP config injection fails
