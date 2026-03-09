from __future__ import annotations

import os
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    linear_api_key: str = Field(default="", alias="LINEAR_API_KEY")

    poll_interval_seconds: int = Field(default=60, alias="POLL_INTERVAL_SECONDS")
    worker_count: int = Field(default=3, alias="WORKER_COUNT")
    session_ttl_days: int = Field(default=30, alias="SESSION_TTL_DAYS")
    max_issues_per_poll: int = Field(default=100, alias="MAX_ISSUES_PER_POLL")

    data_dir: str = Field(default="./data", alias="DATA_DIR")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8645, alias="WEB_PORT")
    test_mode: bool = Field(default=False, alias="TEST_MODE")
    repo_roots: str = Field(default="", alias="REPO_ROOTS")
    repo_max_depth: int = Field(default=4, alias="REPO_MAX_DEPTH")
    repo_ignore_dirs: str = Field(default=".git;node_modules;dist;build;.venv;.idea;.vscode", alias="REPO_IGNORE_DIRS")
    hitl_state_name: str = Field(default="", alias="HITL_STATE_NAME")
    review_state_name: str = Field(default="", alias="REVIEW_STATE_NAME")
    done_state_name: str = Field(default="", alias="DONE_STATE_NAME")
    error_state_name: str = Field(default="", alias="ERROR_STATE_NAME")

    claude_command_template: str = Field(
        default="claude -p --prompt-file {prompt_path} --dangerously-skip-permissions --output-format stream-json",
        alias="CLAUDE_COMMAND_TEMPLATE",
    )
    claude_prompt_via: str = Field(default="prompt_file", alias="CLAUDE_PROMPT_VIA")
    claude_prompt_arg: str = Field(default="--prompt", alias="CLAUDE_PROMPT_ARG")
    claude_workdir_mode: str = Field(default="team_path", alias="CLAUDE_WORKDIR_MODE")
    use_git_worktrees: bool = Field(default=True, alias="USE_GIT_WORKTREES")
    worktree_root: str = Field(default="./data/worktrees", alias="WORKTREE_ROOT")

    linear_mcp_enabled: bool = Field(default=False, alias="LINEAR_MCP_ENABLED")
    linear_mcp_command: str = Field(default="", alias="LINEAR_MCP_COMMAND")
    linear_mcp_workdir: str = Field(default="", alias="LINEAR_MCP_WORKDIR")

    ignore_comment_author_ids: str = Field(default="", alias="IGNORE_COMMENT_AUTHOR_IDS")
    ignore_comment_author_emails: str = Field(default="", alias="IGNORE_COMMENT_AUTHOR_EMAILS")

    max_concurrent_per_team: int = Field(default=1, alias="MAX_CONCURRENT_PER_TEAM")
    stale_job_timeout_minutes: int = Field(default=240, alias="STALE_JOB_TIMEOUT_MINUTES")
    linear_webhook_secret: str = Field(default="", alias="LINEAR_WEBHOOK_SECRET")
    github_token: str = Field(default="", alias="GITHUB_TOKEN")


    def data_path(self) -> Path:
        return Path(self.data_dir).resolve()

    def ensure_dirs(self) -> None:
        base = self.data_path()
        for sub in ["sessions", "logs", "cache"]:
            (base / sub).mkdir(parents=True, exist_ok=True)

    def ignored_author_ids(self) -> set[str]:
        return {x.strip() for x in self.ignore_comment_author_ids.split(",") if x.strip()}

    def ignored_author_emails(self) -> set[str]:
        return {x.strip().lower() for x in self.ignore_comment_author_emails.split(",") if x.strip()}

    def repo_root_paths(self) -> list[Path]:
        roots = []
        for raw in self.repo_roots.split(";"):
            raw = raw.strip()
            if not raw:
                continue
            roots.append(Path(raw).resolve())
        return roots

    def repo_ignore_set(self) -> set[str]:
        return {x.strip().lower() for x in self.repo_ignore_dirs.split(";") if x.strip()}


settings = Settings()
