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

ClaudeWrapper is a FastAPI server that bridges Linear tickets to Claude Code CLI sessions.

**Flow:**
1. `_poll_loop` (or webhook `POST /api/webhook/linear`) detects new/updated Linear issues
2. Matching issues are enqueued in `job_queue` (SQLite)
3. N async worker tasks (`_worker_loop`) dequeue jobs, build a prompt, and run Claude via `subprocess.Popen` inside `asyncio.to_thread`
4. Results are posted back to Linear as comments and the ticket state is updated

**Key files:**
- `app/orchestrator.py` — the core loop: polling, workers, prompt building, state transitions, stale-job reaper, GitHub PR creation
- `app/db.py` — all SQLite access; single connection with WAL mode; every DB call runs on the event loop thread (never from worker threads)
- `app/linear_client.py` — async GraphQL client with a persistent `httpx.AsyncClient` and an in-process workflow-state cache per team
- `app/claude_runner.py` — launches Claude as a subprocess; writes stdout/stderr directly to files so the SSE log endpoint can stream them live
- `app/git_worktree.py` — manages `git worktree add/remove` per ticket; also handles GitHub remote parsing and branch pushing for auto-PR

**Data layout (`DATA_DIR`, default `./data`):**
```
data/
  app.db                       # SQLite database
  sessions/<identifier>/<job_id>/
    prompt.txt                 # prompt sent to Claude
    stdout.txt                 # streamed live via /sessions/{id}/log/stream
    stderr.txt
    worktree.json              # repo + worktree paths (if USE_GIT_WORKTREES=true)
  worktrees/<identifier>/      # git worktrees (one per ticket, reused on retry)
  logs/app.log
```

**SQLite tables:** `team_mappings`, `issue_state`, `job_queue`, `sessions` (one row per run, keyed by `run_id` = job_id), `label_instructions`, `config`

**Session lifecycle:** `pending` → `running` → `done` | `failed` | `awaiting_feedback` (HITL). The `_reaper_loop` resets jobs stuck in `running` after `STALE_JOB_TIMEOUT_MINUTES` (default 4h) — used for crash recovery, not as a timeout.

**Prompt delivery modes** (`CLAUDE_PROMPT_VIA`): `prompt_file` (default), `arg`, or `stdin`. Controlled by `CLAUDE_COMMAND_TEMPLATE`.

**`pyproject.toml` note:** file must be saved without BOM (UTF-8 plain). `[tool.setuptools.packages.find]` is set to `include = ["app*"]` to exclude the `data/` directory from the build.
