# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable)
python -m pip install -e .

# Run the server
python -m app.main
# or
claudewrapper

# Test mode (captures prompts without running Claude)
TEST_MODE=true python -m app.main
```

There are no automated tests. Verify changes manually via the web UI at `http://localhost:8645`.

## Architecture

ClaudeWrapper is a FastAPI server providing a mobile-first chat interface for managing tasks across multiple projects, with Claude Code CLI as the backend AI engine.

**Flow:**
1. User creates a project (pointing to a local git repo)
2. User creates tasks within projects (auto-detected as Bug/Feature/Redesign)
3. User sends chat messages on tasks — each message enqueues a Claude run
4. Per-project worker picks up runs sequentially, builds a prompt (mode-specific + conversation history), runs Claude via subprocess
5. Claude's stream-json output is parsed and streamed to the chat UI via SSE
6. Results are stored as messages; token usage is tracked per run

**Key files:**
- `app/main.py` — FastAPI routes: REST API for projects/tasks/messages, SSE streaming, HTML pages
- `app/orchestrator.py` — Per-project worker loops, prompt building, run execution, PR creation
- `app/db.py` — SQLite with WAL; tables: projects, tasks, messages, runs, config
- `app/claude_runner.py` — Launches Claude as subprocess; writes stdout/stderr to files for SSE streaming
- `app/git_worktree.py` — Git worktree create/reset/push per task; ensures fresh base branch each run
- `app/task_modes.py` — Mode detection (bug/feature/redesign) and mode-specific prompt templates
- `app/config.py` — Pydantic settings from .env

**Data layout (`DATA_DIR`, default `./data`):**
```
data/
  app.db                       # SQLite database
  sessions/<identifier>/<run_id>/
    prompt.txt                 # prompt sent to Claude
    stdout.txt                 # streamed live via SSE
    stderr.txt
    worktree.json              # repo + worktree paths
  worktrees/<identifier>/      # git worktrees (one per task)
  logs/app.log
```

**SQLite tables:** `projects`, `tasks`, `messages`, `runs`, `config`

**Task modes:** Bug (fix-focused), Feature (build-focused), Redesign (refactor-focused) — auto-detected from title keywords, each with different system prompt sections.

**Session continuity:** Tasks reuse worktrees and Claude sessions (via `--resume`). Before each run, the worktree is reset to latest `origin/{base_branch}` to prevent merge conflicts.

**`pyproject.toml` note:** File must be saved without BOM (UTF-8 plain). `[tool.setuptools.packages.find]` is set to `include = ["app*"]` to exclude the `data/` directory from the build.
