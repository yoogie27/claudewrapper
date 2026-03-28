from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    local_path TEXT NOT NULL,
    base_branch TEXT DEFAULT 'main',
    default_prompt TEXT DEFAULT '',
    github_repo_url TEXT DEFAULT '',
    github_token TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'feature',
    status TEXT NOT NULL DEFAULT 'open',
    priority TEXT DEFAULT 'medium',
    identifier TEXT NOT NULL UNIQUE,
    branch_name TEXT,
    worktree_path TEXT,
    claude_session_id TEXT,
    cli_backend TEXT DEFAULT 'claude',
    pr_url TEXT,
    pr_merged INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    run_id TEXT,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);
CREATE INDEX IF NOT EXISTS idx_messages_run ON messages(run_id);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    status TEXT NOT NULL DEFAULT 'pending',
    prompt TEXT,
    session_dir TEXT,
    claude_session_id TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    model TEXT DEFAULT '',
    started_at TEXT,
    ended_at TEXT,
    exit_code INTEGER,
    queue_position INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS prompt_library (
    id TEXT PRIMARY KEY,
    slash_command TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    prompt TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    is_builtin INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_prompt_library_command ON prompt_library(slash_command);
CREATE INDEX IF NOT EXISTS idx_prompt_library_category ON prompt_library(category);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate(self) -> None:
        """Drop old v1 tables if they exist, add new columns."""
        tables = {r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        old_tables = {"team_mappings", "issue_state", "job_queue", "sessions", "label_instructions"}
        for t in old_tables & tables:
            self._conn.execute(f"DROP TABLE IF EXISTS {t}")
        if old_tables & tables:
            self._conn.commit()

        # Add queue_position column to runs (if not present)
        if "runs" in tables:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "queue_position" not in cols:
                self._conn.execute("ALTER TABLE runs ADD COLUMN queue_position INTEGER DEFAULT 0")
                self._conn.commit()

        # Add cli_backend column to tasks (if not present)
        if "tasks" in tables:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()}
            if "cli_backend" not in cols:
                self._conn.execute("ALTER TABLE tasks ADD COLUMN cli_backend TEXT DEFAULT 'claude'")
                self._conn.commit()

        # Add github_token column to projects (if not present)
        if "projects" in tables:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(projects)").fetchall()}
            if "github_token" not in cols:
                self._conn.execute("ALTER TABLE projects ADD COLUMN github_token TEXT DEFAULT ''")
                self._conn.commit()

    def wal_checkpoint(self) -> None:
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        cur = self._conn
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
            cur.commit()
        except Exception:
            cur.rollback()
            raise

    # ── Config ──

    def get_config(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_config(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO config(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def delete_config(self, key: str) -> None:
        self._conn.execute("DELETE FROM config WHERE key = ?", (key,))
        self._conn.commit()

    # ── Projects ──

    def create_project(self, id: str, name: str, slug: str, local_path: str,
                       base_branch: str = "main", default_prompt: str = "",
                       github_repo_url: str = "", github_token: str = "") -> dict:
        now = utc_now()
        self._conn.execute(
            """INSERT INTO projects(id, name, slug, local_path, base_branch, default_prompt, github_repo_url, github_token, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, name, slug, local_path, base_branch, default_prompt, github_repo_url, github_token, now, now),
        )
        self._conn.commit()
        return self.get_project(id)  # type: ignore

    def get_project(self, id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (id,)).fetchone()
        return dict(row) if row else None

    def get_project_by_slug(self, slug: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        return dict(row) if row else None

    def list_projects(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT p.*,
                      MAX(m.created_at) as last_message_at,
                      EXISTS(
                          SELECT 1 FROM runs r
                          JOIN tasks t2 ON r.task_id = t2.id
                          WHERE t2.project_id = p.id AND r.status IN ('running', 'pending')
                      ) as has_active_run
               FROM projects p
               LEFT JOIN tasks t ON t.project_id = p.id
               LEFT JOIN messages m ON m.task_id = t.id
               GROUP BY p.id
               ORDER BY COALESCE(MAX(m.created_at), p.created_at) DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def update_project(self, id: str, **kwargs: str) -> None:
        kwargs["updated_at"] = utc_now()
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        self._conn.execute(f"UPDATE projects SET {sets} WHERE id=?", vals)
        self._conn.commit()

    def delete_project(self, id: str) -> None:
        with self.tx() as conn:
            task_ids = [r[0] for r in conn.execute(
                "SELECT id FROM tasks WHERE project_id=?", (id,)
            ).fetchall()]
            if task_ids:
                placeholders = ",".join("?" * len(task_ids))
                conn.execute(f"DELETE FROM messages WHERE task_id IN ({placeholders})", task_ids)
                conn.execute(f"DELETE FROM runs WHERE task_id IN ({placeholders})", task_ids)
                conn.execute(f"DELETE FROM tasks WHERE project_id=?", (id,))
            conn.execute("DELETE FROM projects WHERE id=?", (id,))
            conn.execute("DELETE FROM config WHERE key=?", (f"task_counter:{id}",))

    # ── Tasks ──

    def next_task_number(self, project_id: str) -> int:
        """Monotonic counter per project. Atomic via BEGIN IMMEDIATE."""
        counter_key = f"task_counter:{project_id}"
        with self.tx() as conn:
            row = conn.execute("SELECT value FROM config WHERE key = ?", (counter_key,)).fetchone()
            current = int(row[0]) if row else 0
            next_num = current + 1
            conn.execute(
                "INSERT INTO config(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (counter_key, str(next_num)),
            )
            return next_num

    def create_task(self, id: str, project_id: str, title: str, identifier: str,
                    description: str = "", mode: str = "feature",
                    priority: str = "medium", branch_name: str = "",
                    cli_backend: str = "claude") -> dict:
        now = utc_now()
        self._conn.execute(
            """INSERT INTO tasks(id, project_id, title, description, mode, status, priority, identifier, branch_name, cli_backend, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)""",
            (id, project_id, title, description, mode, priority, identifier, branch_name, cli_backend, now, now),
        )
        self._conn.commit()
        return self.get_task(id)  # type: ignore

    def get_task(self, id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
        return dict(row) if row else None

    def get_task_by_identifier(self, identifier: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE identifier = ?", (identifier,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, project_id: str, status: str | None = None) -> list[dict]:
        # Sort by latest message activity (tasks with recent messages float to top)
        status_filter = "AND t.status=?" if status else ""
        params: tuple = (project_id, status) if status else (project_id,)
        rows = self._conn.execute(
            f"""SELECT t.*,
                       MAX(m.created_at) as last_message_at,
                       COUNT(m.id) as message_count
                FROM tasks t
                LEFT JOIN messages m ON m.task_id = t.id
                WHERE t.project_id=? {status_filter}
                GROUP BY t.id
                ORDER BY COALESCE(MAX(m.created_at), t.created_at) DESC""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def update_task(self, id: str, **kwargs) -> None:
        kwargs["updated_at"] = utc_now()
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        self._conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
        self._conn.commit()

    def delete_task(self, id: str) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM messages WHERE task_id=?", (id,))
            conn.execute("DELETE FROM runs WHERE task_id=?", (id,))
            conn.execute("DELETE FROM tasks WHERE id=?", (id,))

    # ── Messages ──

    def create_message(self, id: str, task_id: str, role: str, content: str,
                       run_id: str | None = None, metadata: dict | None = None) -> dict:
        now = utc_now()
        meta_json = json.dumps(metadata or {})
        self._conn.execute(
            """INSERT INTO messages(id, task_id, role, content, run_id, metadata, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (id, task_id, role, content, run_id, meta_json, now),
        )
        self._conn.commit()
        return {"id": id, "task_id": task_id, "role": role, "content": content,
                "run_id": run_id, "metadata": metadata or {}, "created_at": now}

    def list_messages(self, task_id: str, limit: int = 0, before: str = "") -> list[dict]:
        if limit > 0:
            if before:
                rows = self._conn.execute(
                    "SELECT * FROM (SELECT * FROM messages WHERE task_id=? AND created_at < ? "
                    "ORDER BY created_at DESC LIMIT ?) sub ORDER BY created_at ASC",
                    (task_id, before, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM (SELECT * FROM messages WHERE task_id=? "
                    "ORDER BY created_at DESC LIMIT ?) sub ORDER BY created_at ASC",
                    (task_id, limit),
                ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE task_id=? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d["metadata"]) if d["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                d["metadata"] = {}
            result.append(d)
        return result

    # ── Runs ──

    def create_run(self, id: str, task_id: str, prompt: str = "",
                   status: str = "pending") -> dict:
        now = utc_now()
        self._conn.execute(
            """INSERT INTO runs(id, task_id, status, prompt, created_at)
               VALUES(?, ?, ?, ?, ?)""",
            (id, task_id, status, prompt, now),
        )
        self._conn.commit()
        return self.get_run(id)  # type: ignore

    def get_run(self, id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (id,)).fetchone()
        return dict(row) if row else None

    def get_latest_run(self, task_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_pending_run(self, project_id: str) -> dict | None:
        """Get the highest-priority pending run for any task in this project."""
        row = self._conn.execute(
            """SELECT r.* FROM runs r
               JOIN tasks t ON r.task_id = t.id
               WHERE t.project_id=? AND r.status='pending'
               ORDER BY r.queue_position ASC, r.created_at ASC LIMIT 1""",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_running_run(self, project_id: str) -> dict | None:
        """Get any currently running run for this project."""
        row = self._conn.execute(
            """SELECT r.* FROM runs r
               JOIN tasks t ON r.task_id = t.id
               WHERE t.project_id=? AND r.status='running'
               LIMIT 1""",
            (project_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_run(self, id: str, **kwargs) -> None:
        if not kwargs:
            return
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        self._conn.execute(f"UPDATE runs SET {sets} WHERE id=?", vals)
        self._conn.commit()

    def has_pending_runs(self, task_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM runs WHERE task_id=? AND status='pending' LIMIT 1",
            (task_id,),
        ).fetchone()
        return row is not None

    def get_active_run_for_task(self, task_id: str) -> dict | None:
        """Get the current active run for a task: prefer running, then oldest pending."""
        row = self._conn.execute(
            "SELECT * FROM runs WHERE task_id=? AND status='running' LIMIT 1",
            (task_id,),
        ).fetchone()
        if row:
            return dict(row)
        row = self._conn.execute(
            "SELECT * FROM runs WHERE task_id=? AND status='pending' ORDER BY created_at ASC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def consolidate_pending_runs(self, task_id: str, keep_run_id: str) -> int:
        """Cancel all pending runs for a task except the specified one.
        Their messages are already in the DB and will be included in the kept run's prompt."""
        result = self._conn.execute(
            "UPDATE runs SET status='cancelled', ended_at=? WHERE task_id=? AND status='pending' AND id!=?",
            (utc_now(), task_id, keep_run_id),
        )
        self._conn.commit()
        return result.rowcount

    def list_runs(self, task_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY created_at DESC",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_queue(self) -> list[dict]:
        """List active queue entries (one per task) with deterministic run selection."""
        rows = self._conn.execute(
            """SELECT repr.id as run_id, repr.status as run_status,
                      repr.queue_position, repr.created_at as run_created_at,
                      t.id as task_id, t.title as task_title, t.identifier, t.mode,
                      p.id as project_id, p.name as project_name, p.slug as project_slug,
                      CASE WHEN repr.status = 'running' THEN 1 ELSE 0 END as is_running,
                      agg.run_count
               FROM (
                   SELECT task_id, COUNT(*) as run_count
                   FROM runs WHERE status IN ('running', 'pending')
                   GROUP BY task_id
               ) agg
               JOIN (
                   SELECT * FROM runs r1
                   WHERE r1.status IN ('running', 'pending')
                     AND r1.id = (
                         SELECT r2.id FROM runs r2
                         WHERE r2.task_id = r1.task_id AND r2.status IN ('running', 'pending')
                         ORDER BY (r2.status = 'running') DESC, r2.queue_position ASC, r2.created_at ASC
                         LIMIT 1
                     )
               ) repr ON repr.task_id = agg.task_id
               JOIN tasks t ON t.id = agg.task_id
               JOIN projects p ON t.project_id = p.id
               ORDER BY is_running DESC, repr.queue_position ASC, repr.created_at ASC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def reorder_queue(self, run_ids: list[str]) -> None:
        """Set queue_position based on the ordered list of run IDs."""
        for pos, run_id in enumerate(run_ids):
            self._conn.execute(
                "UPDATE runs SET queue_position=? WHERE id=? AND status='pending'",
                (pos, run_id),
            )
        self._conn.commit()

    def fail_orphaned_runs(self) -> int:
        """Mark all 'running' runs as failed (used on startup for crash recovery)."""
        now = utc_now()
        with self.tx() as conn:
            result = conn.execute(
                "UPDATE runs SET status='failed', ended_at=?, exit_code=-1 WHERE status='running'",
                (now,),
            )
            # Also reset tasks that were in_progress with no remaining pending runs
            conn.execute("""
                UPDATE tasks SET status='failed'
                WHERE status='in_progress'
                  AND id NOT IN (SELECT task_id FROM runs WHERE status IN ('pending', 'running'))
            """)
            return result.rowcount

    def requeue_stale_runs(self, older_than_iso: str) -> list[str]:
        """Returns affected project IDs so caller can restart workers."""
        with self.tx() as conn:
            affected = conn.execute(
                """SELECT DISTINCT t.project_id FROM runs r
                   JOIN tasks t ON r.task_id = t.id
                   WHERE r.status='running' AND r.started_at < ?""",
                (older_than_iso,),
            ).fetchall()
            project_ids = [row[0] for row in affected]
            conn.execute(
                "UPDATE runs SET status='pending' WHERE status='running' AND started_at < ?",
                (older_than_iso,),
            )
            return project_ids

    # ── Usage / Aggregation ──

    def get_usage_by_project(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT p.id, p.name, p.slug,
                      COALESCE(SUM(r.input_tokens), 0) as total_input_tokens,
                      COALESCE(SUM(r.output_tokens), 0) as total_output_tokens,
                      COALESCE(SUM(r.cost_usd), 0.0) as total_cost_usd,
                      COUNT(r.id) as run_count
               FROM projects p
               LEFT JOIN tasks t ON t.project_id = p.id
               LEFT JOIN runs r ON r.task_id = t.id AND r.status = 'done'
               GROUP BY p.id ORDER BY total_cost_usd DESC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_usage_for_task(self, task_id: str) -> dict:
        row = self._conn.execute(
            """SELECT COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                      COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                      COALESCE(SUM(cost_usd), 0.0) as total_cost_usd,
                      COUNT(id) as run_count
               FROM runs WHERE task_id=? AND status='done'""",
            (task_id,),
        ).fetchone()
        return dict(row) if row else {"total_input_tokens": 0, "total_output_tokens": 0, "total_cost_usd": 0.0, "run_count": 0}

    def get_usage_over_time(self, days: int = 30) -> list[dict]:
        rows = self._conn.execute(
            """SELECT DATE(created_at) as date,
                      COALESCE(SUM(input_tokens), 0) as input_tokens,
                      COALESCE(SUM(output_tokens), 0) as output_tokens,
                      COALESCE(SUM(cost_usd), 0.0) as cost_usd,
                      COUNT(id) as runs
               FROM runs
               WHERE status='done' AND created_at > datetime('now', ?)
               GROUP BY DATE(created_at) ORDER BY date""",
            (f"-{days} days",),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Prompt Library ──

    def list_prompts(self, category: str | None = None) -> list[dict]:
        if category:
            rows = self._conn.execute(
                "SELECT * FROM prompt_library WHERE category=? ORDER BY title",
                (category,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM prompt_library ORDER BY category, title"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_prompt(self, id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM prompt_library WHERE id = ?", (id,)).fetchone()
        return dict(row) if row else None

    def get_prompt_by_command(self, slash_command: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM prompt_library WHERE slash_command = ?", (slash_command,)
        ).fetchone()
        return dict(row) if row else None

    def create_prompt(self, id: str, slash_command: str, title: str,
                      prompt: str, description: str = "", category: str = "general",
                      is_builtin: bool = False) -> dict:
        now = utc_now()
        self._conn.execute(
            """INSERT INTO prompt_library(id, slash_command, title, description, prompt, category, is_builtin, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, slash_command, title, description, prompt, category, int(is_builtin), now, now),
        )
        self._conn.commit()
        return self.get_prompt(id)  # type: ignore

    def update_prompt(self, id: str, **kwargs) -> None:
        kwargs["updated_at"] = utc_now()
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [id]
        self._conn.execute(f"UPDATE prompt_library SET {sets} WHERE id=?", vals)
        self._conn.commit()

    def delete_prompt(self, id: str) -> None:
        self._conn.execute("DELETE FROM prompt_library WHERE id=?", (id,))
        self._conn.commit()

    def seed_prompts(self, prompts: list[dict]) -> int:
        """Insert builtin prompts that don't already exist. Returns count of inserted."""
        count = 0
        for p in prompts:
            existing = self.get_prompt_by_command(p["slash_command"])
            if not existing:
                self.create_prompt(
                    id=p["id"], slash_command=p["slash_command"], title=p["title"],
                    prompt=p["prompt"], description=p.get("description", ""),
                    category=p.get("category", "general"), is_builtin=True,
                )
                count += 1
        return count

    # ── Cleanup ──

    def cleanup_old_runs(self, older_than_iso: str) -> list[dict]:
        """Delete expired runs AND their messages to prevent orphans.

        Also cleans up user messages (run_id IS NULL) for tasks whose
        runs are ALL being deleted, preventing gradual DB bloat.
        """
        rows = self._conn.execute(
            "SELECT r.id, r.session_dir, r.task_id, t.identifier FROM runs r JOIN tasks t ON r.task_id=t.id "
            "WHERE r.ended_at IS NOT NULL AND r.ended_at < ?",
            (older_than_iso,),
        ).fetchall()
        dirs = [{"session_dir": r["session_dir"], "identifier": r["identifier"]} for r in rows if r["session_dir"]]
        run_ids = [r["id"] for r in rows]
        if run_ids:
            placeholders = ",".join("?" * len(run_ids))
            self._conn.execute(f"DELETE FROM messages WHERE run_id IN ({placeholders})", run_ids)
            self._conn.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", run_ids)

            # Clean orphaned user messages (run_id IS NULL) for tasks that
            # have no remaining runs after cleanup.
            expired_task_ids = list({r["task_id"] for r in rows})
            task_ph = ",".join("?" * len(expired_task_ids))
            self._conn.execute(
                f"DELETE FROM messages WHERE task_id IN ({task_ph}) AND run_id IS NULL "
                f"AND task_id NOT IN (SELECT DISTINCT task_id FROM runs)",
                expired_task_ids,
            )
        self._conn.commit()
        return dirs
