# ClaudeWrapper

**Multi-project task management with Claude Code chat interface.**

A self-hosted web app for managing coding tasks across multiple projects. Create tasks, chat with Claude, and get work done — from your phone or desktop.

## What it does

- **Multi-project**: Add as many git repos as you want, each with its own task queue
- **Task management**: Create tasks as Bug, Feature, or Redesign (auto-detected from title)
- **Project scratchpad**: Per-project notes/TODO list with instant autosave in backend
- **Chat interface**: Send messages to Claude, see streaming responses with markdown + code highlighting
- **Git isolation**: Each task gets its own git worktree branched from latest main — zero merge conflicts
- **Session continuity**: Follow-up messages resume the same Claude session with full context
- **Auto-PR**: Pushes branches and creates GitHub PRs automatically
- **Token tracking**: See cost per run, per project, and over time
- **GitHub Actions**: Live workflow status widget in the task panel
- **Mobile-first**: Gorgeous chat UI optimized for phones, works great on desktop too

## Quick start

```bash
pip install -e .
python -m app.main
# Open http://localhost:8645
```

1. Click **+** to add a project (paste a GitHub URL or start blank)
2. Create a task — type a title and the mode (Bug/Feature/Redesign) is auto-detected
3. Send a message and Claude gets to work

## Configuration

All settings via `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_HOST` | `0.0.0.0` | Listen address |
| `WEB_PORT` | `8645` | Listen port |
| `DATA_DIR` | `./data` | Data directory (DB, sessions, uploads) |
| `WORKSPACE_ROOT` | `./data/workspace` | Where project repos live |
| `TEST_MODE` | `false` | Skip Claude, capture prompts only |
| `GITHUB_TOKEN` | | Token for auto-PR creation (see Settings page for setup guide) |
| `SSH_KEY_DIR` | | Path to SSH keys for git operations |
| `USE_GIT_WORKTREES` | `true` | Isolated branch per task |
| `SESSION_TTL_DAYS` | `30` | Auto-cleanup old sessions |
| `STALE_JOB_TIMEOUT_MINUTES` | `240` | Requeue crashed jobs after this |

## Architecture

```
Browser ──────▶ FastAPI (port 8645)
                  │
                  ├─ Project/Task/Message CRUD (SQLite)
                  ├─ SSE streaming (live Claude output)
                  └─ Per-project worker
                       │
                       ├─ git worktree setup (isolated branch)
                       ├─ Claude Code CLI (subprocess)
                       ├─ Parse stream-json output
                       ├─ Store result as chat message
                       └─ Push + create GitHub PR
```

**Key files:**

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI routes, SSE streaming, HTML pages |
| `app/orchestrator.py` | Workers, prompt building, Claude execution, PR creation |
| `app/db.py` | SQLite schema + CRUD (projects, tasks, messages, runs) |
| `app/claude_runner.py` | Subprocess management for Claude CLI |
| `app/git_worktree.py` | Git worktree create/reset/push |
| `app/task_modes.py` | Bug/Feature/Redesign detection + prompt templates |
| `app/config.py` | Pydantic settings from .env |
