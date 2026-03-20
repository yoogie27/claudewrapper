from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS team_mappings (
    team_id TEXT PRIMARY KEY,
    team_name TEXT NOT NULL,
    local_path TEXT NOT NULL,
    default_prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    auto_process INTEGER NOT NULL DEFAULT 1,
    auto_merge INTEGER NOT NULL DEFAULT 0,
    github_repo_url TEXT DEFAULT '',
    clone_status TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS issue_state (
    issue_id TEXT PRIMARY KEY,
    identifier TEXT NOT NULL,
    team_id TEXT NOT NULL,
    last_state_type TEXT,
    last_state_name TEXT,
    last_comment_at TEXT,
    last_seen_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    last_title TEXT
);

CREATE TABLE IF NOT EXISTS job_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    identifier TEXT NOT NULL,
    team_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    locked_by TEXT,
    locked_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    issue_id TEXT PRIMARY KEY,
    identifier TEXT NOT NULL,
    run_id INTEGER,
    status TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    last_activity_at TEXT,
    session_dir TEXT NOT NULL,
    last_error TEXT,
    claude_session_id TEXT,
    pr_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_identifier ON sessions(identifier);
CREATE INDEX IF NOT EXISTS idx_sessions_run_id ON sessions(run_id);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS label_instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT NOT NULL,
    label_name TEXT NOT NULL,
    instruction TEXT NOT NULL,
    UNIQUE(team_id, label_name)
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate(self) -> None:
        tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "sessions" in tables:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if "run_id" not in cols:
                # Legacy: no run_id at all — drop
                self._conn.execute("DROP TABLE sessions")
                self._conn.commit()
            elif "id" in cols:
                # Old schema: id autoincrement + run_id UNIQUE, multiple rows per ticket.
                # Migrate to issue_id PRIMARY KEY (one session per ticket).
                # Keep only the latest session per issue_id.
                self._conn.execute("""
                    CREATE TABLE sessions_new (
                        issue_id TEXT PRIMARY KEY,
                        identifier TEXT NOT NULL,
                        run_id INTEGER,
                        status TEXT NOT NULL,
                        started_at TEXT,
                        ended_at TEXT,
                        last_activity_at TEXT,
                        session_dir TEXT NOT NULL,
                        last_error TEXT,
                        claude_session_id TEXT,
                        pr_url TEXT
                    )
                """)
                self._conn.execute("""
                    INSERT OR IGNORE INTO sessions_new(
                        issue_id, identifier, run_id, status, started_at, ended_at,
                        last_activity_at, session_dir, last_error, claude_session_id, pr_url
                    )
                    SELECT issue_id, identifier, run_id, status, started_at, ended_at,
                           last_activity_at, session_dir, last_error, claude_session_id, pr_url
                    FROM sessions
                    ORDER BY last_activity_at DESC
                """)
                self._conn.execute("DROP TABLE sessions")
                self._conn.execute("ALTER TABLE sessions_new RENAME TO sessions")
                self._conn.commit()
            else:
                # Already new schema — add columns if missing
                if "claude_session_id" not in cols:
                    self._conn.execute("ALTER TABLE sessions ADD COLUMN claude_session_id TEXT")
                    self._conn.commit()
                if "pr_url" not in cols:
                    self._conn.execute("ALTER TABLE sessions ADD COLUMN pr_url TEXT")
                    self._conn.commit()
                if "pr_merged" not in cols:
                    self._conn.execute("ALTER TABLE sessions ADD COLUMN pr_merged INTEGER NOT NULL DEFAULT 0")
                    self._conn.commit()

        # team_mappings migrations
        if "team_mappings" in tables:
            tm_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(team_mappings)").fetchall()}
            if "auto_process" not in tm_cols:
                self._conn.execute("ALTER TABLE team_mappings ADD COLUMN auto_process INTEGER NOT NULL DEFAULT 1")
                self._conn.commit()
            if "auto_merge" not in tm_cols:
                self._conn.execute("ALTER TABLE team_mappings ADD COLUMN auto_merge INTEGER NOT NULL DEFAULT 0")
                self._conn.commit()
            if "github_repo_url" not in tm_cols:
                self._conn.execute("ALTER TABLE team_mappings ADD COLUMN github_repo_url TEXT DEFAULT ''")
                self._conn.commit()
            if "clone_status" not in tm_cols:
                self._conn.execute("ALTER TABLE team_mappings ADD COLUMN clone_status TEXT DEFAULT ''")
                self._conn.commit()

    def wal_checkpoint(self) -> None:
        """Force a WAL checkpoint to reclaim disk space."""
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

    def is_team_paused(self, team_id: str) -> bool:
        return self.get_config(f"paused:{team_id}") == "1"

    def set_team_paused(self, team_id: str, paused: bool) -> None:
        if paused:
            self.set_config(f"paused:{team_id}", "1")
        else:
            self.delete_config(f"paused:{team_id}")

    def upsert_team_mapping(self, team_id: str, team_name: str, local_path: str, default_prompt: str, enabled: bool, auto_process: bool = True, auto_merge: bool = False, github_repo_url: str = "") -> None:
        now = utc_now()
        self._conn.execute(
            """
            INSERT INTO team_mappings(team_id, team_name, local_path, default_prompt, enabled, created_at, updated_at, auto_process, auto_merge, github_repo_url)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_id) DO UPDATE SET
                team_name=excluded.team_name,
                local_path=excluded.local_path,
                default_prompt=excluded.default_prompt,
                enabled=excluded.enabled,
                updated_at=excluded.updated_at,
                auto_process=excluded.auto_process,
                auto_merge=excluded.auto_merge,
                github_repo_url=excluded.github_repo_url
            """,
            (team_id, team_name, local_path, default_prompt, 1 if enabled else 0, now, now, 1 if auto_process else 0, 1 if auto_merge else 0, github_repo_url),
        )
        self._conn.commit()

    def update_clone_status(self, team_id: str, status: str, local_path: str | None = None) -> None:
        if local_path:
            self._conn.execute(
                "UPDATE team_mappings SET clone_status=?, local_path=?, updated_at=? WHERE team_id=?",
                (status, local_path, utc_now(), team_id),
            )
        else:
            self._conn.execute(
                "UPDATE team_mappings SET clone_status=?, updated_at=? WHERE team_id=?",
                (status, utc_now(), team_id),
            )
        self._conn.commit()

    def list_team_mappings(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM team_mappings ORDER BY team_name").fetchall()

    def get_team_mapping(self, team_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM team_mappings WHERE team_id = ?", (team_id,)).fetchone()

    def upsert_issue_state(
        self,
        issue_id: str,
        identifier: str,
        team_id: str,
        state_type: str | None,
        state_name: str | None,
        last_comment_at: str | None,
        last_seen_at: str,
        last_updated_at: str,
        title: str | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO issue_state(issue_id, identifier, team_id, last_state_type, last_state_name, last_comment_at, last_seen_at, last_updated_at, last_title)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(issue_id) DO UPDATE SET
                identifier=excluded.identifier,
                team_id=excluded.team_id,
                last_state_type=excluded.last_state_type,
                last_state_name=excluded.last_state_name,
                last_comment_at=excluded.last_comment_at,
                last_seen_at=excluded.last_seen_at,
                last_updated_at=excluded.last_updated_at,
                last_title=excluded.last_title
            """,
            (issue_id, identifier, team_id, state_type, state_name, last_comment_at, last_seen_at, last_updated_at, title),
        )
        self._conn.commit()

    def get_issue_state(self, issue_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM issue_state WHERE issue_id = ?", (issue_id,)).fetchone()

    def enqueue_job(self, issue_id: str, identifier: str, team_id: str, reason: str) -> None:
        now = utc_now()
        existing = self._conn.execute(
            "SELECT 1 FROM job_queue WHERE issue_id = ? AND status IN ('pending','running')",
            (issue_id,),
        ).fetchone()
        if existing:
            return
        self._conn.execute(
            "INSERT INTO job_queue(issue_id, identifier, team_id, reason, created_at, status) VALUES(?, ?, ?, ?, ?, 'pending')",
            (issue_id, identifier, team_id, reason, now),
        )
        self._conn.commit()

    def dequeue_job(self, worker_id: str, max_per_team: int = 0) -> sqlite3.Row | None:
        with self.tx() as conn:
            if max_per_team > 0:
                # Only pick a job whose team has fewer than max_per_team running jobs.
                # This prevents merge conflicts: each job starts from the latest main
                # after the previous one has merged.
                row = conn.execute(
                    """
                    SELECT * FROM job_queue
                    WHERE status='pending'
                      AND (
                        SELECT COUNT(*) FROM job_queue j2
                        WHERE j2.team_id = job_queue.team_id AND j2.status = 'running'
                      ) < ?
                    ORDER BY created_at LIMIT 1
                    """,
                    (max_per_team,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM job_queue WHERE status='pending' ORDER BY created_at LIMIT 1"
                ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE job_queue SET status='running', locked_by=?, locked_at=? WHERE id=?",
                (worker_id, utc_now(), row["id"]),
            )
            return row

    def update_job_status(self, job_id: int, status: str) -> None:
        self._conn.execute(
            "UPDATE job_queue SET status=?, locked_by=NULL, locked_at=NULL WHERE id=?",
            (status, job_id),
        )
        self._conn.commit()

    def complete_job(self, job_id: int, ok: bool, error: str | None = None) -> None:
        status = "done" if ok else "failed"
        self._conn.execute(
            "UPDATE job_queue SET status=? WHERE id=?",
            (status, job_id),
        )
        if error:
            self._conn.execute(
                "UPDATE sessions SET last_error=? WHERE run_id=?",
                (error, job_id),
            )
        self._conn.commit()

    def requeue_stale_jobs(self, older_than_iso: str) -> int:
        with self.tx() as conn:
            result = conn.execute(
                "UPDATE job_queue SET status='pending', locked_by=NULL, locked_at=NULL "
                "WHERE status='running' AND locked_at < ?",
                (older_than_iso,),
            )
            return result.rowcount

    def list_jobs(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM job_queue ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def upsert_session(
        self,
        run_id: int,
        issue_id: str,
        identifier: str,
        status: str,
        session_dir: str,
        started_at: str | None,
        ended_at: str | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO sessions(issue_id, identifier, run_id, status, started_at, ended_at, last_activity_at, session_dir)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(issue_id) DO UPDATE SET
                run_id=excluded.run_id,
                status=excluded.status,
                started_at=COALESCE(excluded.started_at, sessions.started_at),
                ended_at=excluded.ended_at,
                last_activity_at=excluded.last_activity_at,
                session_dir=excluded.session_dir,
                last_error=NULL
            """,
            (issue_id, identifier, run_id, status, started_at, ended_at, utc_now(), session_dir),
        )
        self._conn.commit()

    def list_sessions(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM sessions ORDER BY last_activity_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def list_sessions_for_identifier(self, identifier: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE identifier = ?",
            (identifier,),
        ).fetchall()

    def get_latest_session_by_identifier(self, identifier: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE identifier = ?",
            (identifier,),
        ).fetchone()

    def get_session(self, issue_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE issue_id = ?",
            (issue_id,),
        ).fetchone()

    def set_claude_session_id(self, run_id: int, claude_session_id: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET claude_session_id=? WHERE run_id=?",
            (claude_session_id, run_id),
        )
        self._conn.commit()

    def set_session_pr_url(self, run_id: int, pr_url: str) -> None:
        self._conn.execute("UPDATE sessions SET pr_url=? WHERE run_id=?", (pr_url, run_id))
        self._conn.commit()

    def set_pr_merged(self, pr_url: str) -> None:
        """Mark a PR as merged (by URL, so it works regardless of how the merge happened)."""
        self._conn.execute("UPDATE sessions SET pr_merged=1 WHERE pr_url=?", (pr_url,))
        self._conn.commit()

    def get_open_pr_sessions(self) -> list[sqlite3.Row]:
        """Get sessions that have a PR URL but are not yet marked as merged."""
        return self._conn.execute(
            "SELECT * FROM sessions WHERE pr_url IS NOT NULL AND pr_url != '' AND pr_merged = 0"
        ).fetchall()

    def get_session_by_run_id(self, run_id: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM sessions WHERE run_id=?", (run_id,)).fetchone()

    def get_last_claude_session_id(self, identifier: str) -> str | None:
        """Get the Claude session UUID from the session for a ticket."""
        row = self._conn.execute(
            "SELECT claude_session_id FROM sessions WHERE identifier=? AND claude_session_id IS NOT NULL",
            (identifier,),
        ).fetchone()
        return row["claude_session_id"] if row else None

    def get_issue_by_identifier(self, identifier: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM issue_state WHERE identifier = ?", (identifier,)).fetchone()

    def cleanup_sessions(self, older_than_iso: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT session_dir, identifier FROM sessions WHERE last_activity_at < ?",
            (older_than_iso,),
        ).fetchall()
        self._conn.execute("DELETE FROM sessions WHERE last_activity_at < ?", (older_than_iso,))
        self._conn.commit()
        return [{"session_dir": r["session_dir"], "identifier": r["identifier"]} for r in rows]

    def purge_jobs(self, older_than_iso: str) -> None:
        self._conn.execute("DELETE FROM job_queue WHERE created_at < ?", (older_than_iso,))
        self._conn.commit()

    # Label instructions

    def upsert_label_instruction(self, team_id: str, label_name: str, instruction: str) -> None:
        self._conn.execute(
            """
            INSERT INTO label_instructions(team_id, label_name, instruction)
            VALUES(?, ?, ?)
            ON CONFLICT(team_id, label_name) DO UPDATE SET instruction=excluded.instruction
            """,
            (team_id, label_name, instruction),
        )
        self._conn.commit()

    def delete_label_instruction(self, team_id: str, label_name: str) -> None:
        self._conn.execute(
            "DELETE FROM label_instructions WHERE team_id=? AND label_name=?",
            (team_id, label_name),
        )
        self._conn.commit()

    def list_label_instructions(self, team_id: str | None = None) -> list[sqlite3.Row]:
        if team_id:
            return self._conn.execute(
                "SELECT * FROM label_instructions WHERE team_id=? ORDER BY label_name",
                (team_id,),
            ).fetchall()
        return self._conn.execute("SELECT * FROM label_instructions ORDER BY team_id, label_name").fetchall()
