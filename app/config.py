from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: str = Field(default="./data", alias="DATA_DIR")
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8645, alias="WEB_PORT")
    test_mode: bool = Field(default=False, alias="TEST_MODE")

    claude_command_template: str = Field(
        default="claude -p --prompt-file {prompt_path} --dangerously-skip-permissions --output-format stream-json",
        alias="CLAUDE_COMMAND_TEMPLATE",
    )
    claude_prompt_via: str = Field(default="prompt_file", alias="CLAUDE_PROMPT_VIA")
    claude_prompt_arg: str = Field(default="--prompt", alias="CLAUDE_PROMPT_ARG")
    claude_workdir_mode: str = Field(default="project_path", alias="CLAUDE_WORKDIR_MODE")

    workspace_root: str = Field(default="", alias="WORKSPACE_ROOT")
    use_git_worktrees: bool = Field(default=True, alias="USE_GIT_WORKTREES")
    worktree_root: str = Field(default="", alias="WORKTREE_ROOT")

    max_concurrent_per_project: int = Field(default=1, alias="MAX_CONCURRENT_PER_PROJECT")
    stale_job_timeout_minutes: int = Field(default=240, alias="STALE_JOB_TIMEOUT_MINUTES")
    session_ttl_days: int = Field(default=30, alias="SESSION_TTL_DAYS")

    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    ssh_key_dir: str = Field(default="", alias="SSH_KEY_DIR")

    def ssh_key_path(self) -> Path:
        """SSH key directory. Falls back to {DATA_DIR}/ssh if not explicitly set."""
        if self.ssh_key_dir:
            return Path(self.ssh_key_dir).resolve()
        return self.data_path() / "ssh"

    def data_path(self) -> Path:
        return Path(self.data_dir).resolve()

    def workspace_path(self) -> Path:
        if self.workspace_root:
            return Path(self.workspace_root).resolve()
        return self.data_path() / "workspace"

    def worktree_path(self) -> Path:
        if self.worktree_root:
            return Path(self.worktree_root).resolve()
        return self.data_path() / "worktrees"

    def project_repo_path(self, slug: str) -> Path:
        """Canonical repo path for a project. This is the single source of truth —
        never rely on stored local_path from the DB for file operations."""
        return self.workspace_path() / slug

    def ensure_dirs(self) -> None:
        base = self.data_path()
        for sub in ["sessions", "logs"]:
            (base / sub).mkdir(parents=True, exist_ok=True)
        self.workspace_path().mkdir(parents=True, exist_ok=True)
        self.worktree_path().mkdir(parents=True, exist_ok=True)


settings = Settings()
