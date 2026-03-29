from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db import Database, utc_now
from app.orchestrator import Orchestrator
from app.task_modes import detect_mode, MODE_LABELS, MODE_COLORS
from app.cli_backend import BACKEND_CHOICES
from app.ssh import setup_ssh
from app.prompt_seeds import BUILTIN_PROMPTS


db = Database(settings.data_path() / "app.db")
orchestrator = Orchestrator(settings, db)

# Seed built-in prompts on import (idempotent)
db.seed_prompts(BUILTIN_PROMPTS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Always ensure SSH keys exist (auto-creates if needed)
    try:
        setup_ssh(settings.ssh_key_path())
    except Exception as exc:
        logging.getLogger("claudewrapper").warning("SSH setup failed: %s", exc)
    await orchestrator.start()
    yield
    # Signal background loops (_reaper_loop, _cleanup_loop) to stop
    orchestrator.stop_event.set()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# ── HTML Pages ──

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Any:
    projects = db.list_projects()
    return templates.TemplateResponse(request, "app.html", {
        "projects": projects,
        "mode_labels": MODE_LABELS,
        "mode_colors": MODE_COLORS,
        "backends_json": json.dumps([{"value": b["value"], "label": b["label"]} for b in BACKEND_CHOICES]),
    })


@app.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request) -> Any:
    usage = db.get_usage_by_project()
    timeline = db.get_usage_over_time(30)
    return templates.TemplateResponse(request, "usage.html", {
        "usage": usage,
        "timeline": timeline,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Any:
    projects = db.list_projects()
    return templates.TemplateResponse(request, "settings.html", {
        "settings": settings,
        "projects": projects,
        "workspace_path": settings.workspace_path(),
    })


# ── Project API ──

def _sanitize_project(p: dict) -> dict:
    """Strip secret fields from project dict before returning to client."""
    out = {k: v for k, v in p.items() if k != "github_token"}
    out["has_github_token"] = bool(p.get("github_token"))
    return out


@app.get("/api/projects")
async def list_projects() -> Any:
    return [_sanitize_project(p) for p in db.list_projects()]


@app.post("/api/projects")
async def create_project(request: Request) -> Any:
    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Name is required"}, 400)

    github_url = data.get("github_repo_url", "").strip()

    # Generate slug
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        slug = "project"
    if db.get_project_by_slug(slug):
        i = 2
        while db.get_project_by_slug(f"{slug}-{i}"):
            i += 1
        slug = f"{slug}-{i}"

    # Auto-assign workspace path (always computed from slug, never stored as source of truth)
    repo_path = settings.project_repo_path(slug)
    repo_path.mkdir(parents=True, exist_ok=True)

    # Clone from GitHub URL if provided and directory is empty
    clone_error = ""
    if github_url and not (repo_path / ".git").exists():
        ssh_env = orchestrator._get_git_ssh_env()
        env = {**os.environ, **(ssh_env or {})}
        try:
            cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            result = subprocess.run(
                ["git", "clone", github_url, str(repo_path)],
                capture_output=True, text=True, timeout=120, env=env, creationflags=cflags,
            )
            if result.returncode != 0:
                clone_error = result.stderr.strip() or result.stdout.strip()
        except Exception as exc:
            clone_error = str(exc)

    # Init a bare repo if no git repo exists (no URL or clone failed)
    if not (repo_path / ".git").exists() and not clone_error:
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.run(["git", "init", str(repo_path)], capture_output=True, creationflags=cflags)

    project_id = uuid.uuid4().hex
    try:
        project = db.create_project(
            id=project_id,
            name=name,
            slug=slug,
            local_path=str(repo_path),
            base_branch=data.get("base_branch", "main") or "main",
            default_prompt=data.get("default_prompt", ""),
            github_repo_url=github_url,
            github_token=data.get("github_token", "").strip(),
        )
    except sqlite3.IntegrityError:
        # UNIQUE constraint race: slug was taken between check and insert
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"
        project = db.create_project(
            id=project_id,
            name=name,
            slug=slug,
            local_path=str(repo_path),
            base_branch=data.get("base_branch", "main") or "main",
            default_prompt=data.get("default_prompt", ""),
            github_repo_url=github_url,
            github_token=data.get("github_token", "").strip(),
        )

    result = _sanitize_project(project)
    if clone_error:
        result["clone_error"] = clone_error
    return result


@app.post("/api/projects/{project_id}/reclone")
async def reclone_project(project_id: str) -> Any:
    """Wipe the workspace directory and re-clone from the project's GitHub URL."""
    project = db.get_project(project_id)
    if not project:
        return JSONResponse({"error": "Not found"}, 404)

    github_url = project.get("github_repo_url", "").strip()
    if not github_url:
        return JSONResponse({"error": "No Git URL configured for this project"}, 400)

    repo_path = settings.project_repo_path(project["slug"])

    # Wipe existing directory
    if repo_path.exists():
        shutil.rmtree(str(repo_path), ignore_errors=True)
    repo_path.mkdir(parents=True, exist_ok=True)

    # Clone
    ssh_env = orchestrator._get_git_ssh_env()
    env = {**os.environ, **(ssh_env or {})}
    try:
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            ["git", "clone", github_url, str(repo_path)],
            capture_output=True, text=True, timeout=180, env=env, creationflags=cflags,
        )
        if result.returncode != 0:
            error = result.stderr.strip() or result.stdout.strip()
            return {"ok": False, "error": error}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True}


@app.put("/api/projects/{project_id}")
async def update_project(project_id: str, request: Request) -> Any:
    data = await request.json()
    project = db.get_project(project_id)
    if not project:
        return JSONResponse({"error": "Not found"}, 404)

    allowed = {"name", "base_branch", "default_prompt", "github_repo_url", "github_token"}
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}
    if "name" in updates and not updates["name"].strip():
        return JSONResponse({"error": "Name cannot be empty"}, 400)
    if updates:
        db.update_project(project_id, **updates)
    return _sanitize_project(db.get_project(project_id))


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> Any:
    project = db.get_project(project_id)
    if not project:
        return JSONResponse({"error": "Not found"}, 404)
    db.delete_project(project_id)
    return {"ok": True}


# ── Task API ──

@app.get("/api/projects/{project_id}/tasks")
async def list_tasks(project_id: str, status: str | None = None) -> Any:
    tasks = db.list_tasks(project_id, status)
    if tasks:
        task_ids = [t["id"] for t in tasks]
        usage_map = db.get_usage_for_tasks(task_ids)
        for t in tasks:
            u = usage_map.get(t["id"])
            t["total_cost_usd"] = u["total_cost_usd"] if u else 0.0
            t["run_count"] = u["run_count"] if u else 0
    return tasks


@app.post("/api/projects/{project_id}/tasks")
async def create_task(project_id: str, request: Request) -> Any:
    project = db.get_project(project_id)
    if not project:
        return JSONResponse({"error": "Project not found"}, 404)

    data = await request.json()
    title = data.get("title", "").strip()
    if not title:
        return JSONResponse({"error": "Title is required"}, 400)

    description = data.get("description", "").strip()
    mode = data.get("mode", "").strip()
    if not mode:
        mode = detect_mode(title + " " + description)
    priority = data.get("priority", "medium").strip()
    cli_backend = data.get("cli_backend", "claude").strip() or "claude"
    valid_backends = {b["value"] for b in BACKEND_CHOICES}
    if cli_backend not in valid_backends:
        cli_backend = "claude"

    num = db.next_task_number(project_id)
    identifier = f"{project['slug']}-{num:03d}"
    branch_name = f"ticket/{identifier}"

    task_id = uuid.uuid4().hex
    task = db.create_task(
        id=task_id,
        project_id=project_id,
        title=title,
        description=description,
        mode=mode,
        priority=priority,
        identifier=identifier,
        branch_name=branch_name,
        cli_backend=cli_backend,
    )
    return task


DERIVE_PURPOSES = {
    "double_check": {
        "label": "Double Check",
        "prompt": "You are reviewing a proposal from another AI. Critically evaluate it:\n\n"
                  "1. **Correctness** — Are there factual errors, wrong assumptions, or flawed logic?\n"
                  "2. **Completeness** — Is anything missing? Edge cases? Error handling?\n"
                  "3. **Risks** — What could go wrong with this approach?\n"
                  "4. **Verdict** — Would you approve this plan as-is, or what needs to change?\n\n"
                  "Be direct and specific. If the plan is solid, say so briefly.",
    },
    "improve": {
        "label": "Improve Plan",
        "prompt": "You are improving a proposal from another AI. Build on it:\n\n"
                  "1. **Strengths** — What's good about this approach? Keep those parts.\n"
                  "2. **Weaknesses** — What's suboptimal, overcomplicated, or missing?\n"
                  "3. **Improved Plan** — Rewrite the plan with your improvements. Be concrete: name files, functions, steps.\n\n"
                  "Output a complete, improved plan — not just a list of suggestions.",
    },
    "implement": {
        "label": "Implement",
        "prompt": "You are implementing a plan that was designed by another AI. The plan is provided below.\n\n"
                  "Follow the plan closely. If you find issues during implementation, fix them and note what you changed and why.\n"
                  "Do NOT re-plan or re-analyze — just build it.",
    },
    "custom": {
        "label": "Custom",
        "prompt": "",
    },
}


@app.post("/api/tasks/{task_id}/derive")
async def derive_task(task_id: str, request: Request) -> Any:
    """Create a new task derived from a message in an existing task."""
    source_task = db.get_task(task_id)
    if not source_task:
        return JSONResponse({"error": "Source task not found"}, 404)

    data = await request.json()
    message_id = data.get("message_id", "").strip()
    purpose = data.get("purpose", "double_check").strip()
    backend = data.get("cli_backend", "claude").strip() or "claude"
    custom_prompt = data.get("custom_prompt", "").strip()

    if purpose not in DERIVE_PURPOSES:
        return JSONResponse({"error": f"Unknown purpose: {purpose}"}, 400)
    valid_backends = {b["value"] for b in BACKEND_CHOICES}
    if backend not in valid_backends:
        return JSONResponse({"error": f"Unknown backend: {backend}"}, 400)

    # Collect the last 2 messages up to and including the target message
    all_msgs = db.list_messages(task_id)
    if message_id:
        # Find the target message index
        target_idx = next((i for i, m in enumerate(all_msgs) if m["id"] == message_id), None)
        if target_idx is None:
            return JSONResponse({"error": "Message not found"}, 404)
        # Take the target message + the one before it (usually user prompt + assistant response)
        start = max(0, target_idx - 1)
        context_msgs = all_msgs[start:target_idx + 1]
    else:
        # No specific message — take last 2
        context_msgs = all_msgs[-2:] if len(all_msgs) >= 2 else all_msgs

    purpose_info = DERIVE_PURPOSES[purpose]
    purpose_prompt = custom_prompt if purpose == "custom" and custom_prompt else purpose_info["prompt"]

    source_context = json.dumps({
        "source_task_id": task_id,
        "source_identifier": source_task["identifier"],
        "purpose": purpose,
        "purpose_prompt": purpose_prompt,
        "messages": [{"role": m["role"], "content": m["content"]} for m in context_msgs],
    })

    project = db.get_project(source_task["project_id"])
    if not project:
        return JSONResponse({"error": "Project not found"}, 404)

    purpose_label = purpose_info["label"]
    title = f"{purpose_label}: {source_task['title']}"
    mode = "plan" if purpose in ("double_check", "improve") else source_task["mode"]

    num = db.next_task_number(source_task["project_id"])
    identifier = f"{project['slug']}-{num:03d}"

    new_task = db.create_task(
        id=uuid.uuid4().hex,
        project_id=source_task["project_id"],
        title=title,
        description=f"Derived from {source_task['identifier']} ({purpose_label})",
        mode=mode,
        priority=source_task.get("priority", "medium"),
        identifier=identifier,
        branch_name=f"ticket/{identifier}",
        cli_backend=backend,
        source_context=source_context,
    )
    return new_task


@app.put("/api/tasks/{task_id}")
async def update_task(task_id: str, request: Request) -> Any:
    data = await request.json()
    task = db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Not found"}, 404)

    allowed = {"title", "description", "mode", "priority", "status"}
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}
    if updates:
        db.update_task(task_id, **updates)
    return db.get_task(task_id)


@app.post("/api/tasks/{task_id}/retry-pr")
async def retry_pr(task_id: str) -> Any:
    task = db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Not found"}, 404)
    project = db.get_project(task["project_id"])
    if not project:
        return JSONResponse({"error": "Project not found"}, 404)
    pr_url, pr_error = await orchestrator.retry_pr(task, project)
    if pr_url:
        return {"ok": True, "pr_url": pr_url}
    return JSONResponse({"ok": False, "error": pr_error or "PR creation failed"}, 500)


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str) -> Any:
    task = db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Not found"}, 404)
    orchestrator.cleanup_task(task_id)
    db.delete_task(task_id)
    return {"ok": True}


# ── Image Upload ──

@app.post("/api/tasks/{task_id}/upload")
async def upload_image(task_id: str, request: Request) -> Any:
    """Accept image upload (multipart or raw base64 JSON). Returns URL."""
    task = db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, 404)

    upload_dir = settings.data_path() / "uploads" / task_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    allowed_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

    content_type = request.headers.get("content-type", "")
    if "multipart" in content_type:
        from starlette.datastructures import UploadFile as _UF
        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, "read"):
            return JSONResponse({"error": "No file uploaded"}, 400)
        ext = (Path(file.filename).suffix if file.filename else ".png").lower() or ".png"
        if ext not in allowed_exts:
            return JSONResponse({"error": f"File type {ext} not allowed"}, 400)
        fname = f"{uuid.uuid4().hex[:12]}{ext}"
        fpath = upload_dir / fname
        data = await file.read()
        fpath.write_bytes(data)
    else:
        body = await request.json()
        b64 = body.get("data", "")
        if not b64:
            return JSONResponse({"error": "No image data"}, 400)
        # Strip data URI prefix if present
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        try:
            data = base64.b64decode(b64)
        except (binascii.Error, ValueError):
            return JSONResponse({"error": "Invalid base64 data"}, 400)
        ext = body.get("ext", ".png").lower()
        if ext not in allowed_exts:
            return JSONResponse({"error": f"File type {ext} not allowed"}, 400)
        fname = f"{uuid.uuid4().hex[:12]}{ext}"
        fpath = upload_dir / fname
        fpath.write_bytes(data)

    url = f"/api/uploads/{task_id}/{fname}"
    return {"ok": True, "url": url, "filename": fname}


@app.get("/api/uploads/{task_id}/{filename}")
async def serve_upload(task_id: str, filename: str) -> Any:
    """Serve an uploaded image."""
    from fastapi.responses import FileResponse
    upload_root = settings.data_path() / "uploads"
    fpath = (upload_root / task_id / filename).resolve()
    if not str(fpath).startswith(str(upload_root.resolve()) + "/"):
        return PlainTextResponse("Forbidden", status_code=403)
    if not fpath.exists():
        return PlainTextResponse("Not found", status_code=404)
    ext_to_media = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp",
        ".svg": "image/svg+xml",
    }
    suffix = Path(filename).suffix.lower()
    media = ext_to_media.get(suffix, "image/png")
    return FileResponse(fpath, media_type=media)


# ── Messages & Chat API ──

@app.get("/api/tasks/{task_id}/messages")
async def list_messages(task_id: str, request: Request) -> Any:
    try:
        limit = int(request.query_params.get("limit", "0"))
    except (ValueError, TypeError):
        limit = 0
    before = request.query_params.get("before", "")
    return db.list_messages(task_id, limit=limit, before=before)


@app.post("/api/tasks/{task_id}/messages")
async def send_message(task_id: str, request: Request) -> Any:
    data = await request.json()
    content = data.get("content", "").strip()
    if not content:
        return JSONResponse({"error": "Content is required"}, 400)

    task = db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, 404)

    # Check if task already has an active run (message will be queued)
    active = db.get_active_run_for_task(task_id)
    queued = active is not None

    run = await orchestrator.enqueue_message(task_id, content)
    return {"ok": True, "run_id": run["id"], "task_id": task_id, "queued": queued}


def _parse_sse_line(line: str):
    """Parse a single stream-json line into SSE events. Yields (event, data) tuples."""
    line = line.strip()
    if not line:
        return
    try:
        obj = json.loads(line)
        msg_type = obj.get("type", "")
        if msg_type == "assistant":
            content_blocks = (obj.get("message") or {}).get("content", [])
            for block in (content_blocks if isinstance(content_blocks, list) else []):
                if block.get("type") == "text":
                    yield "text", {"text": block["text"]}
                elif block.get("type") == "tool_use":
                    # Send only key hints, not the full input (can be huge)
                    inp = block.get("input", {})
                    hint = {}
                    for k in ("command", "file_path", "pattern", "query", "content", "old_string"):
                        if k in inp:
                            v = str(inp[k])
                            hint[k] = v[:200] if len(v) > 200 else v
                            break
                    yield "tool", {"tool": block.get("name", ""), "input": hint}
        elif msg_type == "result":
            yield "result", {"cost_usd": obj.get("total_cost_usd", 0), "usage": obj.get("usage", {})}
    except (json.JSONDecodeError, ValueError):
        # Non-JSON output (Codex, Gemini, etc.) — stream as raw text
        yield "text", {"text": line + "\n"}


@app.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: str) -> Any:
    """SSE endpoint for live Claude output on a task.
    Follows active runs until all complete, supporting queued messages."""

    async def generate():
        current_run_id = None
        pos = 0
        stderr_pos = 0
        line_buffer = ""
        idle = 0
        stale_ticks = 0  # ticks with no new output

        while True:
            # Find the current active run for this task (running > oldest pending)
            active = db.get_active_run_for_task(task_id)

            if not active:
                idle += 1
                if idle > 10:  # 5s with no active run → done
                    yield "event: done\ndata: {}\n\n"
                    return
                await asyncio.sleep(0.5)
                continue

            idle = 0

            # Switched to a different run? Signal previous run complete, reset read state
            if active["id"] != current_run_id:
                if current_run_id is not None:
                    yield f"event: run_complete\ndata: {{}}\n\n"
                current_run_id = active["id"]
                pos = 0
                stderr_pos = 0
                line_buffer = ""
                stale_ticks = 0

            # Read only NEW output from this run's stdout file (seek to pos)
            had_output = False
            session_dir = active.get("session_dir")
            if session_dir:
                stdout_path = Path(session_dir) / "stdout.txt"
                if stdout_path.exists():
                    try:
                        with open(stdout_path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(pos)
                            chunk = f.read()
                            new_pos = f.tell()
                        if chunk:
                            had_output = True
                            pos = new_pos
                            chunk = line_buffer + chunk
                            if chunk.endswith("\n"):
                                lines = chunk.splitlines()
                                line_buffer = ""
                            else:
                                lines = chunk.splitlines()
                                line_buffer = lines.pop() if lines else chunk
                            for ln in lines:
                                for event, data in _parse_sse_line(ln):
                                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                    except Exception:
                        pass

                # Also stream stderr
                stderr_path = Path(session_dir) / "stderr.txt"
                if stderr_path.exists():
                    try:
                        with open(stderr_path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(stderr_pos)
                            err_chunk = f.read()
                            stderr_pos = f.tell()
                        if err_chunk:
                            had_output = True
                            for err_line in err_chunk.splitlines():
                                err_line = err_line.strip()
                                if err_line:
                                    yield f"event: text\ndata: {json.dumps({'text': err_line + chr(10), 'stderr': True})}\n\n"
                    except Exception:
                        pass

            # Re-check this run's status (may have finished since we read active)
            current = db.get_run(current_run_id)
            if current and current["status"] not in ("pending", "running"):
                # Flush remaining buffer
                if line_buffer.strip():
                    for event, data in _parse_sse_line(line_buffer):
                        yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                # Don't exit — loop back to check for more queued runs
                current_run_id = None
                pos = 0
                stderr_pos = 0
                line_buffer = ""
                stale_ticks = 0
                await asyncio.sleep(1)
                continue

            # Detect stale runs with no output
            if not had_output:
                stale_ticks += 1
                alive = orchestrator.is_run_alive(current_run_id)
                if active["status"] == "running" and stale_ticks >= 30 and not alive:
                    # Process exited but run wasn't cleaned up — mark failed
                    db.update_run(current_run_id, status="failed", ended_at=utc_now(), exit_code=-1)
                    task = db.get_task(task_id)
                    if task and not db.has_pending_runs(task_id):
                        db.update_task(task_id, status="failed")
                    current_run_id = None
                    pos = 0
                    stderr_pos = 0
                    line_buffer = ""
                    stale_ticks = 0
                    await asyncio.sleep(0.5)
                    continue
                elif active["status"] == "pending" and stale_ticks >= 120:
                    # Pending run not picked up after ~60s — disconnect and let
                    # auto-refresh reconnect once the worker processes it
                    yield "event: done\ndata: {}\n\n"
                    return
            else:
                stale_ticks = 0

            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Run Control ──

@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task_run(task_id: str) -> Any:
    run = db.get_active_run_for_task(task_id)
    if run and run["status"] == "running":
        ok = orchestrator.cancel_run(run["id"])
        return {"ok": ok}
    # No running process — but task may be stuck in "in_progress".
    # Reset the task status so the user can unstick it.
    task = db.get_task(task_id)
    if task and task["status"] == "in_progress":
        db.update_task(task_id, status="failed")
        return {"ok": True, "note": "No active run found; task status reset to failed"}
    return JSONResponse({"error": "No running job to cancel"}, 400)


@app.get("/api/tasks/{task_id}/runs")
async def list_runs(task_id: str) -> Any:
    return db.list_runs(task_id)


# ── Usage API ──

@app.get("/api/usage")
async def get_usage() -> Any:
    by_project = db.get_usage_by_project()
    over_time = db.get_usage_over_time(30)
    total_cost = sum(p["total_cost_usd"] for p in by_project)
    total_runs = sum(p["run_count"] for p in by_project)
    return {
        "total_cost_usd": total_cost,
        "total_runs": total_runs,
        "by_project": by_project,
        "over_time": over_time,
    }


# ── Queue Control ──

@app.get("/api/queue")
async def queue_status() -> Any:
    paused = db.get_config("queue_paused") == "1"
    return {"paused": paused}


@app.post("/api/queue/pause")
async def pause_queue() -> Any:
    db.set_config("queue_paused", "1")
    return {"ok": True}


@app.post("/api/queue/resume")
async def resume_queue() -> Any:
    db.delete_config("queue_paused")
    return {"ok": True}


@app.get("/api/queue/items")
async def queue_items() -> Any:
    return db.list_queue()


@app.post("/api/queue/reorder")
async def reorder_queue(request: Request) -> Any:
    data = await request.json()
    run_ids = data.get("run_ids", [])
    if not isinstance(run_ids, list):
        return JSONResponse({"error": "run_ids must be a list"}, 400)
    db.reorder_queue(run_ids)
    return {"ok": True}


# ── Diagnostics API ──

@app.get("/api/diagnostics/github")
async def diagnose_github() -> Any:
    """Validate GitHub token: check auth, scopes, rate limit."""
    token = settings.github_token
    if not token:
        return {"ok": False, "error": "GITHUB_TOKEN not set",
                "hint": "Set GITHUB_TOKEN in your .env file. See the setup guide on the Settings page."}

    import httpx
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get("https://api.github.com/user", headers=headers)
            if resp.status_code == 401:
                return {"ok": False, "error": "Token is invalid or expired (401)",
                        "hint": "Generate a new token at github.com/settings/tokens"}
            if resp.status_code == 403:
                return {"ok": False, "error": "Token forbidden (403) — may be IP-blocked or SSO required",
                        "hint": "If using a GitHub org with SAML SSO, you must authorize the token for that org."}
            if resp.status_code != 200:
                return {"ok": False, "error": f"GitHub API returned {resp.status_code}"}

            user = resp.json()
            scopes = resp.headers.get("x-oauth-scopes", "")
            rate_remaining = resp.headers.get("x-ratelimit-remaining", "?")

            # Check required scopes
            scope_list = [s.strip() for s in scopes.split(",") if s.strip()] if scopes else []
            has_repo = "repo" in scope_list
            is_fine_grained = not scopes  # Fine-grained tokens don't return x-oauth-scopes

            return {
                "ok": True,
                "user": user.get("login", "unknown"),
                "name": user.get("name", ""),
                "scopes": scope_list,
                "has_repo_scope": has_repo,
                "is_fine_grained": is_fine_grained,
                "rate_remaining": rate_remaining,
                "token_prefix": token[:4] + "..." + token[-4:] if len(token) > 12 else "***",
            }
    except Exception as exc:
        return {"ok": False, "error": f"Connection failed: {exc}",
                "hint": "Check network connectivity to api.github.com"}


@app.get("/api/diagnostics/repo/{project_id}")
async def diagnose_repo(project_id: str) -> Any:
    """Check git health for a specific project: remote, branches, fetch, push access."""
    project = db.get_project(project_id)
    if not project:
        return JSONResponse({"error": "Project not found"}, 404)

    from app.git_worktree import parse_github_remote, parse_github_url, get_default_branch, _run, GitWorktreeError
    import asyncio

    repo = settings.project_repo_path(project["slug"])
    checks = []

    # 1. Path exists
    exists = repo.exists()
    checks.append({"name": "Repository path exists", "ok": exists,
                    "detail": str(repo) if exists else f"Path not found: {repo}. Try re-cloning the project."})

    # Filesystem checks (only if path exists)
    gh = None
    if exists:
        git_dir = (repo / ".git").exists()
        checks.append({"name": "Is a git repository", "ok": git_dir,
                        "detail": "Yes" if git_dir else "No .git directory found"})

        if git_dir:
            # 3. Remote URL
            try:
                remote_url = await asyncio.to_thread(_run, ["git", "-C", str(repo), "remote", "get-url", "origin"])
                checks.append({"name": "Remote 'origin' configured", "ok": True, "detail": remote_url})
            except GitWorktreeError as e:
                checks.append({"name": "Remote 'origin' configured", "ok": False, "detail": str(e)})
                remote_url = ""

            # 4. GitHub remote parsed
            gh = await asyncio.to_thread(parse_github_remote, repo)
            if gh:
                checks.append({"name": "GitHub remote detected", "ok": True, "detail": f"{gh[0]}/{gh[1]}"})
            elif remote_url:
                checks.append({"name": "GitHub remote detected", "ok": False,
                                "detail": f"Could not parse GitHub owner/repo from: {remote_url}"})

            # 5. Default branch
            try:
                default_br = await asyncio.to_thread(get_default_branch, repo)
                base = project.get("base_branch") or default_br
                checks.append({"name": "Base branch", "ok": True, "detail": base})
            except Exception as e:
                checks.append({"name": "Base branch", "ok": False, "detail": str(e)})
                base = "main"

            # 6. Fetch from origin
            ssh_env = orchestrator._get_git_ssh_env()
            try:
                await asyncio.to_thread(_run, ["git", "-C", str(repo), "fetch", "origin", base],
                                        None, ssh_env)
                checks.append({"name": f"Fetch origin/{base}", "ok": True, "detail": "Success"})
            except GitWorktreeError as e:
                err = str(e)
                hint = ""
                if "permission denied" in err.lower() or "publickey" in err.lower():
                    hint = " — Check SSH keys or switch to HTTPS with a token"
                elif "could not resolve" in err.lower():
                    hint = " — Network/DNS issue"
                checks.append({"name": f"Fetch origin/{base}", "ok": False, "detail": err + hint})

    # Fallback: parse GitHub info from DB URL if filesystem didn't find it
    if not gh:
        gh = parse_github_url(project.get("github_repo_url", ""))
        if gh:
            checks.append({"name": "GitHub repo (from URL)", "ok": True, "detail": f"{gh[0]}/{gh[1]}"})

    # 7. GitHub API access (works even without local checkout)
    effective_token = project.get("github_token", "").strip() or settings.github_token
    if gh and effective_token:
        import httpx
        owner, repo_name = gh
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo_name}",
                    headers={
                        "Authorization": f"Bearer {effective_token}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    perms = data.get("permissions", {})
                    can_push = perms.get("push", False)
                    checks.append({"name": "GitHub API repo access", "ok": True,
                                   "detail": f"Push: {'yes' if can_push else 'NO'}, Admin: {'yes' if perms.get('admin') else 'no'}"})
                    if not can_push:
                        checks.append({"name": "Push permission", "ok": False,
                                        "detail": "Token does not have push access to this repo. For fine-grained tokens: enable 'Contents: Read and write'. For classic tokens: enable 'repo' scope."})
                elif resp.status_code == 404:
                    checks.append({"name": "GitHub API repo access", "ok": False,
                                   "detail": "404 — repo not found or token has no access. For org repos with SSO, authorize the token for the org."})
                elif resp.status_code == 403:
                    checks.append({"name": "GitHub API repo access", "ok": False,
                                   "detail": "403 — forbidden. If this is an org repo with SAML SSO, you must authorize the token."})
                else:
                    checks.append({"name": "GitHub API repo access", "ok": False,
                                   "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"})
        except Exception as exc:
            checks.append({"name": "GitHub API repo access", "ok": False, "detail": str(exc)})

    return {"project": project["name"], "checks": checks}


# ── SSH Key API ──

@app.get("/api/ssh/public-key")
async def get_ssh_public_key() -> Any:
    """Get the SSH public key, generating one if it doesn't exist."""
    from app.ssh import get_public_key, ensure_ssh_keypair
    key_dir = settings.ssh_key_path()
    try:
        ensure_ssh_keypair(key_dir)
        pub = get_public_key(key_dir)
        return {"ok": True, "public_key": pub, "key_dir": str(key_dir)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Model Config API ──

@app.get("/api/model-config")
async def get_model_config() -> Any:
    """Get current model selection settings."""
    return {
        "model": db.get_config("claude_model", "") or "",
        "fallback_model": db.get_config("claude_fallback_model", "") or "",
    }


@app.put("/api/model-config")
async def set_model_config(request: Request) -> Any:
    """Set model selection. Send empty string to clear."""
    data = await request.json()
    for key in ("model", "fallback_model"):
        if key in data:
            value = data[key].strip()
            db_key = f"claude_{key}"
            if value:
                db.set_config(db_key, value)
            else:
                db.delete_config(db_key)
    return {"ok": True}


# ── Mode Prompts API ──

@app.get("/api/mode-prompts")
async def get_mode_prompts() -> Any:
    """Get all mode prompts (custom overrides + defaults)."""
    from app.task_modes import get_mode_prompt, get_default_mode_prompt, MODE_LABELS
    result = {}
    for mode in ("bug", "feature", "redesign"):
        custom = db.get_config(f"mode_prompt:{mode}") or ""
        result[mode] = {
            "label": MODE_LABELS.get(mode, mode),
            "prompt": custom.strip() if custom.strip() else get_default_mode_prompt(mode),
            "is_custom": bool(custom.strip()),
            "default": get_default_mode_prompt(mode),
        }
    return result


@app.put("/api/mode-prompts/{mode}")
async def set_mode_prompt(mode: str, request: Request) -> Any:
    """Set a custom prompt for a mode. Send empty string to reset to default."""
    if mode not in ("bug", "feature", "redesign"):
        return JSONResponse({"error": "Invalid mode"}, 400)
    data = await request.json()
    prompt = data.get("prompt", "").strip()
    if prompt:
        db.set_config(f"mode_prompt:{mode}", prompt)
    else:
        db.delete_config(f"mode_prompt:{mode}")
    return {"ok": True, "is_custom": bool(prompt)}


# ── GitHub Actions Status API ──

@app.get("/api/projects/{project_id}/actions")
async def get_actions_status(project_id: str) -> Any:
    """Get recent GitHub Actions workflow runs for a project."""
    project = db.get_project(project_id)
    if not project:
        return JSONResponse({"error": "Not found"}, 404)
    effective_token = project.get("github_token", "").strip() or settings.github_token
    if not effective_token:
        return {"runs": [], "error": "No GitHub token configured"}

    # Parse GitHub owner/repo from the stored URL (no filesystem needed)
    from app.git_worktree import parse_github_url, parse_github_remote
    gh = parse_github_url(project.get("github_repo_url", ""))
    # Fallback: try reading from the local git remote
    if not gh:
        repo_path = settings.project_repo_path(project["slug"])
        if repo_path.exists():
            gh = parse_github_remote(repo_path)
    if not gh:
        return {"runs": [], "error": "No GitHub repo URL configured"}

    owner, repo_name = gh
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo_name}/actions/runs",
                headers={
                    "Authorization": f"Bearer {effective_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"per_page": 5},
            )
            if resp.status_code == 403:
                return {"runs": [], "error": "Token needs Actions: Read permission (fine-grained) or repo scope (classic)"}
            if resp.status_code == 404:
                return {"runs": [], "error": "Repo not found or no Actions access — check token permissions"}
            if resp.status_code != 200:
                return {"runs": [], "error": f"GitHub API {resp.status_code}"}

            data = resp.json()
            runs = []
            for r in data.get("workflow_runs", []):
                runs.append({
                    "id": r["id"],
                    "name": r.get("name", ""),
                    "branch": r.get("head_branch", ""),
                    "status": r.get("status", ""),          # queued, in_progress, completed
                    "conclusion": r.get("conclusion", ""),   # success, failure, cancelled, etc.
                    "url": r.get("html_url", ""),
                    "created_at": r.get("created_at", ""),
                    "updated_at": r.get("updated_at", ""),
                    "commit_msg": (r.get("head_commit") or {}).get("message", "")[:80],
                })
            return {"runs": runs, "repo": f"{owner}/{repo_name}"}
    except Exception as exc:
        return {"runs": [], "error": str(exc)}


# ── Prompt Library API ──

@app.get("/api/prompts")
async def list_prompts(category: str | None = None) -> Any:
    return db.list_prompts(category)


@app.post("/api/prompts")
async def create_prompt(request: Request) -> Any:
    data = await request.json()
    slash_command = data.get("slash_command", "").strip().lstrip("/")
    title = data.get("title", "").strip()
    prompt = data.get("prompt", "").strip()
    if not slash_command or not title or not prompt:
        return JSONResponse({"error": "slash_command, title, and prompt are required"}, 400)
    # Validate slug format
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", slash_command):
        return JSONResponse({"error": "Slash command must be lowercase alphanumeric with hyphens"}, 400)
    if db.get_prompt_by_command(slash_command):
        return JSONResponse({"error": f"/{slash_command} already exists"}, 409)

    prompt_id = uuid.uuid4().hex
    result = db.create_prompt(
        id=prompt_id,
        slash_command=slash_command,
        title=title,
        prompt=prompt,
        description=data.get("description", ""),
        category=data.get("category", "general"),
    )
    return result


@app.put("/api/prompts/{prompt_id}")
async def update_prompt(prompt_id: str, request: Request) -> Any:
    existing = db.get_prompt(prompt_id)
    if not existing:
        return JSONResponse({"error": "Not found"}, 404)
    data = await request.json()
    allowed = {"title", "description", "prompt", "category", "slash_command"}
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}
    if "slash_command" in updates:
        cmd = updates["slash_command"].strip().lstrip("/")
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", cmd):
            return JSONResponse({"error": "Slash command must be lowercase alphanumeric with hyphens"}, 400)
        other = db.get_prompt_by_command(cmd)
        if other and other["id"] != prompt_id:
            return JSONResponse({"error": f"/{cmd} already exists"}, 409)
        updates["slash_command"] = cmd
    if updates:
        db.update_prompt(prompt_id, **updates)
    return db.get_prompt(prompt_id)


@app.delete("/api/prompts/{prompt_id}")
async def delete_prompt(prompt_id: str) -> Any:
    existing = db.get_prompt(prompt_id)
    if not existing:
        return JSONResponse({"error": "Not found"}, 404)
    db.delete_prompt(prompt_id)
    return {"ok": True}


# ── Mode Detection API ──

@app.get("/api/backends")
async def list_backends() -> Any:
    result = []
    for b in BACKEND_CHOICES:
        custom_cmd = db.get_config(f"backend_cmd:{b['value']}", "") or ""
        env_json = db.get_config(f"backend_env:{b['value']}", "") or ""
        env_vars: dict = {}
        if env_json:
            try:
                env_vars = json.loads(env_json)
            except (json.JSONDecodeError, ValueError):
                pass
        result.append({**b, "custom_cmd": custom_cmd, "env_vars": env_vars})
    return result


@app.put("/api/backends/{backend_name}/config")
async def update_backend_config(backend_name: str, request: Request) -> Any:
    valid = {b["value"] for b in BACKEND_CHOICES}
    if backend_name not in valid:
        return JSONResponse({"error": f"Unknown backend: {backend_name}"}, 400)
    data = await request.json()
    cmd = data.get("command_template", "").strip()
    if cmd:
        db.set_config(f"backend_cmd:{backend_name}", cmd)
    else:
        db.delete_config(f"backend_cmd:{backend_name}")
    # Persist environment variables (JSON dict)
    if "env_vars" in data:
        env_vars = data["env_vars"]
        if isinstance(env_vars, dict):
            # Remove empty-value entries
            env_vars = {k: v for k, v in env_vars.items() if k.strip() and v.strip()}
            if env_vars:
                db.set_config(f"backend_env:{backend_name}", json.dumps(env_vars))
            else:
                db.delete_config(f"backend_env:{backend_name}")
    return {"ok": True}


_BACKEND_UPDATE_CMDS: dict[str, list[str]] = {
    "claude": ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
    "gemini": ["npm", "install", "-g", "@google/gemini-cli@latest"],
    "codex": ["npm", "install", "-g", "@openai/codex@latest"],
}

@app.post("/api/backends/{backend_name}/update")
async def update_backend_cli(backend_name: str) -> Any:
    cmd = _BACKEND_UPDATE_CMDS.get(backend_name)
    if not cmd:
        return JSONResponse({"ok": False, "error": f"Unknown backend: {backend_name}"}, 400)

    cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    def _run() -> dict:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                creationflags=cflags,
            )
            output = (result.stdout + "\n" + result.stderr).strip()
            if result.returncode == 0:
                return {"ok": True, "output": output}
            return {"ok": False, "error": f"Exit code {result.returncode}", "output": output}
        except FileNotFoundError:
            return {"ok": False, "error": "npm not found. Please install Node.js/npm first."}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Update timed out after 120s"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    return await asyncio.get_event_loop().run_in_executor(None, _run)


@app.post("/api/detect-mode")
async def detect_mode_api(request: Request) -> Any:
    data = await request.json()
    text = data.get("text", "")
    mode = detect_mode(text)
    return {"mode": mode, "label": MODE_LABELS.get(mode, "Feature"), "color": MODE_COLORS.get(mode, "#e11d48")}


# ── Entrypoint ──

def run() -> None:
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.web_host,
        port=settings.web_port,
        log_level="info",
    )


if __name__ == "__main__":
    run()
