# ClaudeWrapper

**Autonomous issue resolution: Linear tickets → Claude Code → merged PRs.**

ClaudeWrapper is a self-hosted orchestration server that watches your Linear board and automatically dispatches Claude Code to work through your tickets — one isolated git worktree per issue, streamed logs in your browser, results posted back to Linear.

---

## The story

At some point in vibecoding you shift gears.

The first phase is generative — you're building fast, telling Claude what to make, watching things appear. You have GitHub Actions wired up, deploys are automatic, the feedback loop is tight.

Then you hit the *testing phase*. You're not building from scratch anymore — you're recording issues. A bug here, a UX rough edge there, something that needs polishing. You open Linear tickets because it's faster than context-switching back to the IDE for every small thing. You're in flow, recording ideas and problems as they surface.

And then you think: *why am I not just letting Claude work through these?*

You already have the repo, the CI, the deployment pipeline. Claude Code already knows your codebase. You're just missing the bridge: something that watches the board, picks up tickets, and runs Claude on each one while you sleep or focus on something else.

That's ClaudeWrapper.

---

## What it does

- **Polls Linear** for new or updated issues matching your configured teams
- **Builds a rich prompt** from the ticket title, description, comments, and your custom system instructions
- **Runs Claude Code** in a subprocess with full tool access — it reads files, writes code, runs tests, makes commits
- **Streams output live** to a web UI so you can watch progress in real time
- **Posts results back** to Linear as a comment and optionally transitions the ticket state
- **Creates a GitHub PR** automatically if the session produced commits
- **Isolates work** using `git worktree` — each ticket gets its own branch, no cross-contamination

---

## Features

- **Multi-team support** — map multiple Linear teams to different local repos
- **Git worktrees** — isolated branches per ticket, rebased from main on every retry
- **Auto PR creation** — pushes branch and opens a GitHub PR when the session produces commits
- **Live log streaming** — SSE-powered terminal in the browser, output appears as Claude types
- **Label instructions** — append extra context per label (e.g., label `bug` → "Make sure to add a regression test")
- **Session resume** — Claude sessions are resumed on retry using the Claude session UUID, preserving context
- **Human-in-the-loop** — optional HITL state: Claude pauses and waits for your comment before continuing
- **Webhook support** — Linear webhooks for instant trigger (no polling delay)
- **MCP server management** — checks and installs workspace MCP servers from the UI
- **Test mode** — dry-run mode that captures prompts but never runs Claude
- **Pause / Start All** — pause a team's queue without losing state, or bulk-enqueue all backlog tickets
- **System health dashboard** — disk, memory, Claude version, Git version, active jobs at a glance
- **File browser** — browse worktree files directly in the UI
- **REST status API** — `/api/status` and `/api/health` for external monitoring

---

## Architecture

```
Linear ──poll/webhook──▶ job_queue (SQLite)
                              │
                     N worker tasks
                              │
                    build prompt.txt
                              │
                 claude -p --prompt-file ...
                    (subprocess + PIPE)
                              │
               stdout.txt ◀──┘ (flushed per line)
                    │
              SSE endpoint ──▶ browser terminal
                    │
              parse stream-json
                    │
         post comment to Linear
         transition ticket state
         push branch + create PR
```

**Key files:**

| File | Purpose |
|---|---|
| `app/orchestrator.py` | Core loop: polling, workers, prompt building, state transitions, PR creation |
| `app/db.py` | All SQLite access — WAL mode, single connection |
| `app/linear_client.py` | Async GraphQL client for Linear |
| `app/claude_runner.py` | Launches Claude subprocess, streams output to disk line by line |
| `app/git_worktree.py` | Manages `git worktree add/remove`, branch pushing, GitHub remote parsing |
| `app/health.py` | System health + MCP workspace verification |
| `app/config.py` | All settings, pydantic-validated from `.env` |

**Data layout:**

```
data/
  app.db                         # SQLite — jobs, sessions, mappings, state
  sessions/<ticket>/<job_id>/
    prompt.txt                   # full prompt sent to Claude
    stdout.txt                   # live-streamed output
    stderr.txt
    worktree.json                # repo + worktree paths
  worktrees/<ticket>/            # git worktrees, one per ticket
  logs/app.log
```

---

## Prerequisites

- Python 3.11+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated (`claude` on PATH)
- A [Linear](https://linear.app) account with API access
- Git (for worktree features)
- A GitHub token (for auto-PR)

---

## Quick start

### 1. Install

```bash
git clone https://github.com/yourname/claudewrapper.git
cd claudewrapper
python -m pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
```

Minimum required:

```env
LINEAR_API_KEY=lin_api_...
```

Everything else has sensible defaults.

### 3. Run

```bash
python -m app.main
# or after install:
claudewrapper
```

Open **http://localhost:8645**.

### 4. Map a team

Go to **Teams**, pick a Linear team, browse to your local repo, save. ClaudeWrapper will start picking up tickets from that team.

---

## Configuration reference

All settings live in `.env`.

### Core

| Variable | Default | Description |
|---|---|---|
| `LINEAR_API_KEY` | *(required)* | Linear personal API key — Settings → API keys |
| `POLL_INTERVAL_SECONDS` | `60` | How often to poll Linear for new issues |
| `WORKER_COUNT` | `3` | Parallel Claude sessions |
| `DATA_DIR` | `./data` | Where sessions, worktrees, and the DB live |
| `WEB_HOST` | `0.0.0.0` | Bind address |
| `WEB_PORT` | `8645` | Port |
| `TEST_MODE` | `false` | Dry-run — captures prompts, never runs Claude |
| `SESSION_TTL_DAYS` | `30` | Age at which sessions are cleaned up |
| `MAX_ISSUES_PER_POLL` | `100` | Max issues fetched per poll cycle |
| `STALE_JOB_TIMEOUT_MINUTES` | `240` | Jobs stuck in `running` longer than this are requeued (crash recovery) |

### Repository discovery

| Variable | Default | Description |
|---|---|---|
| `REPO_ROOTS` | *(empty)* | Semicolon-separated root paths for the folder picker (e.g. `c:/code;d:/projects`) |
| `REPO_MAX_DEPTH` | `4` | How deep to scan for repos |
| `REPO_IGNORE_DIRS` | `.git;node_modules;...` | Directories to skip during scanning |

### Claude integration

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_COMMAND_TEMPLATE` | `claude -p --prompt-file {prompt_path} --dangerously-skip-permissions --output-format stream-json` | Full command; supports `{prompt_path}`, `{prompt_text}`, `{workdir}`, `{session_id}` |
| `CLAUDE_PROMPT_VIA` | `prompt_file` | How to deliver the prompt: `prompt_file`, `stdin`, or `arg` |
| `CLAUDE_WORKDIR_MODE` | `team_path` | Working directory for Claude: `team_path`, `repo_root`, or `none` |
| `USE_GIT_WORKTREES` | `true` | Create an isolated `git worktree` per ticket |
| `WORKTREE_ROOT` | `./data/worktrees` | Where worktrees are created |

> **Note:** `--output-format stream-json` is required for real-time log streaming. ClaudeWrapper automatically upgrades `--output-format json` to `stream-json` if you forget.

### Linear workflow states

Set these to the *exact names* of your workflow states to enable automatic state transitions:

| Variable | Description |
|---|---|
| `DONE_STATE_NAME` | State to set when Claude succeeds (e.g. `Done`, `In Review`) |
| `ERROR_STATE_NAME` | State to set when Claude fails |
| `HITL_STATE_NAME` | State that triggers a pause and waits for a human comment |
| `REVIEW_STATE_NAME` | State to set when a PR is created |

### Filtering

| Variable | Description |
|---|---|
| `IGNORE_COMMENT_AUTHOR_IDS` | Comma-separated Linear user IDs to ignore (prevents Claude from reacting to its own comments) |
| `IGNORE_COMMENT_AUTHOR_EMAILS` | Same, by email |

### Webhooks (optional)

| Variable | Description |
|---|---|
| `LINEAR_WEBHOOK_SECRET` | Secret to verify Linear webhook signatures. Set this, then point a Linear webhook at `POST /api/webhook/linear` |

Webhooks give you instant trigger on issue create/update — no polling delay.

### GitHub auto-PR (optional)

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | Personal access token with `repo` scope. When set, ClaudeWrapper pushes the branch and opens a PR after a successful session that produced commits. |

### MCP servers (optional)

| Variable | Description |
|---|---|
| `LINEAR_MCP_ENABLED` | `true` to inject the Linear MCP server into every workspace |
| `LINEAR_MCP_COMMAND` | Command to run the Linear MCP server |
| `LINEAR_MCP_WORKDIR` | Working directory for the MCP server |

You can also check and install MCP servers manually from the **Teams** UI.

---

## The default prompt

ClaudeWrapper ships with a structured default prompt that gives Claude:

- The ticket identifier, title, priority, and labels
- The full description and recent comments
- The target repository path
- Your custom system instructions (configurable per team)
- HITL instructions (if enabled) — Claude knows to stop and post a comment asking for human input when it hits ambiguity

You can override the system prompt per team from the **Teams** page. Click **Reset to default** to restore the built-in one.

---

## Label instructions

Add per-label extra context to the prompt. Examples:

| Label | Instruction |
|---|---|
| `bug` | Make sure to add a regression test that fails before your fix and passes after. |
| `performance` | Profile before and after. Include benchmark numbers in your Linear comment. |
| `security` | Follow OWASP guidelines. Do not introduce SQL injection, XSS, or command injection. |
| `frontend` | Run the dev server and verify the change visually before committing. |

Configured per team from the **Teams** page.

---

## Git worktrees

When `USE_GIT_WORKTREES=true` (default), each ticket gets its own isolated branch:

```
ticket/ENG-123   ← created from origin/main
ticket/ENG-124   ← separate worktree, no cross-contamination
```

- Worktrees are **reused on retry** — the branch persists, so Claude can build on prior work
- When `GITHUB_TOKEN` is set, the branch is pushed and a PR is opened automatically after a successful session with commits
- Worktrees are cleaned up after `SESSION_TTL_DAYS`

---

## Human-in-the-loop (HITL)

Set `HITL_STATE_NAME` to a workflow state name (e.g. `Needs Clarification`). When Claude encounters ambiguity, it transitions the ticket to that state and posts a comment asking for guidance. When you reply and move the ticket back to an active state, ClaudeWrapper picks it up again and resumes the Claude session with your answer in context.

---

## Test mode

```env
TEST_MODE=true
```

In test mode ClaudeWrapper:
- Processes tickets normally (polling, prompt building, worktrees)
- Writes `prompt.txt` so you can inspect exactly what would be sent
- **Never launches Claude**
- Marks sessions as `test` status instead of `done`/`failed`

Use this to tune prompts and verify the pipeline without burning tokens.

---

## Web UI

| Page | Description |
|---|---|
| **Dashboard** | System health, team mappings, job queue, recent sessions, manual trigger |
| **Teams** | Configure team → repo mappings, custom prompts, label instructions, MCP servers |
| **Tickets** | Browse all tickets from your enabled teams; manually trigger any ticket |
| **Sessions** | View session logs with live streaming output, cancel running jobs |
| **Files** | Browse worktree and repo files directly in the browser |
| **Settings** | View and edit runtime configuration |

### Live log streaming

Session logs stream in real time as Claude works. ClaudeWrapper parses `stream-json` output and renders readable text — you see Claude's reasoning, tool calls, and responses as they happen, not just a final dump.

### Manual trigger

From the dashboard, enter any ticket identifier (e.g. `ENG-42`), click **Lookup** to auto-fill the issue and team IDs, then **Trigger** to queue it immediately without waiting for the poll cycle.

---

## REST API

| Endpoint | Description |
|---|---|
| `GET /api/status` | JSON snapshot of jobs, sessions, mappings |
| `GET /api/health` | Disk, memory, Claude version, Git version |
| `GET /sessions/{id}/log/stream` | SSE stream of live stdout |
| `POST /api/trigger` | Manually trigger a ticket |
| `POST /api/cancel` | Cancel a running job |
| `POST /api/retry` | Retry a failed session |
| `POST /api/webhook/linear` | Linear webhook endpoint |
| `GET /api/workspace/check` | Check MCP server status for a path |
| `POST /api/workspace/install-mcp` | Install an MCP server into a workspace |

---

## Running as a service

### systemd (Linux/macOS)

```ini
[Unit]
Description=ClaudeWrapper
After=network.target

[Service]
WorkingDirectory=/opt/claudewrapper
EnvironmentFile=/opt/claudewrapper/.env
ExecStart=claudewrapper
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Windows Task Scheduler / NSSM

Use `nssm install claudewrapper claudewrapper` and set the working directory to the repo root.

### Docker (bring your own Dockerfile)

The server binds to `0.0.0.0:8645` by default. Mount your `.env` and `./data` volume. Claude Code must be installed inside the image and authenticated (mount `~/.claude` from the host or bake credentials in at build time).

---

## Contributing

Issues, PRs, and ideas welcome. The codebase is intentionally small — around 1500 lines of Python across 8 files, no magic, easy to fork and adapt.

If you're extending ClaudeWrapper, the main extension points are:

- **Custom prompt templates** — edit `DEFAULT_PROMPT` in `orchestrator.py` or override per-team in the UI
- **Additional trigger sources** — `orchestrator.enqueue()` accepts any `(identifier, issue_id, team_id, reason)` tuple; wire it to Jira, GitHub Issues, Notion, whatever
- **Output parsing** — `claude_runner.py` writes raw `stream-json` to `stdout.txt`; post-process it however you like
- **State machine** — workflow state transitions live in `orchestrator._handle_result()`; add new states, approval flows, escalations

---

## License

MIT
