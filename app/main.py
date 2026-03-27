from __future__ import annotations

import asyncio
import json
import logging
import re
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
from app.db import Database
from app.orchestrator import Orchestrator
from app.task_modes import detect_mode, MODE_LABELS, MODE_COLORS
from app.ssh import setup_ssh


db = Database(settings.data_path() / "app.db")
orchestrator = Orchestrator(settings, db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Always ensure SSH keys exist (auto-creates if needed)
    try:
        setup_ssh(settings.ssh_key_path())
    except Exception as exc:
        logging.getLogger("claudewrapper").warning("SSH setup failed: %s", exc)
    await orchestrator.start()
    yield


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

@app.get("/api/projects")
async def list_projects() -> Any:
    return db.list_projects()


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
        import subprocess, sys
        ssh_env = orchestrator._get_git_ssh_env()
        env = {**__import__('os').environ, **(ssh_env or {})}
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
        import subprocess, sys
        cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        subprocess.run(["git", "init", str(repo_path)], capture_output=True, creationflags=cflags)

    project_id = uuid.uuid4().hex
    project = db.create_project(
        id=project_id,
        name=name,
        slug=slug,
        local_path=str(repo_path),
        base_branch=data.get("base_branch", "main") or "main",
        default_prompt=data.get("default_prompt", ""),
        github_repo_url=github_url,
    )

    if clone_error:
        project["clone_error"] = clone_error
    return project


@app.put("/api/projects/{project_id}")
async def update_project(project_id: str, request: Request) -> Any:
    data = await request.json()
    project = db.get_project(project_id)
    if not project:
        return JSONResponse({"error": "Not found"}, 404)

    allowed = {"name", "base_branch", "default_prompt", "github_repo_url"}
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}
    if updates:
        db.update_project(project_id, **updates)
    return db.get_project(project_id)


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
    # Attach cost info per task
    for t in tasks:
        usage = db.get_usage_for_task(t["id"])
        t["total_cost_usd"] = usage["total_cost_usd"]
        t["run_count"] = usage["run_count"]
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

    # Generate identifier
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
    )
    return task


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

    content_type = request.headers.get("content-type", "")
    if "multipart" in content_type:
        from starlette.datastructures import UploadFile as _UF
        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, "read"):
            return JSONResponse({"error": "No file uploaded"}, 400)
        ext = Path(file.filename).suffix or ".png"
        fname = f"{uuid.uuid4().hex[:12]}{ext}"
        fpath = upload_dir / fname
        data = await file.read()
        fpath.write_bytes(data)
    else:
        body = await request.json()
        b64 = body.get("data", "")
        if not b64:
            return JSONResponse({"error": "No image data"}, 400)
        import base64
        # Strip data URI prefix if present
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        data = base64.b64decode(b64)
        ext = body.get("ext", ".png")
        fname = f"{uuid.uuid4().hex[:12]}{ext}"
        fpath = upload_dir / fname
        fpath.write_bytes(data)

    url = f"/api/uploads/{task_id}/{fname}"
    return {"ok": True, "url": url, "filename": fname}


@app.get("/api/uploads/{task_id}/{filename}")
async def serve_upload(task_id: str, filename: str) -> Any:
    """Serve an uploaded image."""
    from fastapi.responses import FileResponse
    fpath = settings.data_path() / "uploads" / task_id / filename
    if not fpath.exists():
        return PlainTextResponse("Not found", status_code=404)
    media = "image/png"
    if filename.endswith(".jpg") or filename.endswith(".jpeg"):
        media = "image/jpeg"
    elif filename.endswith(".gif"):
        media = "image/gif"
    elif filename.endswith(".webp"):
        media = "image/webp"
    return FileResponse(fpath, media_type=media)


# ── Messages & Chat API ──

@app.get("/api/tasks/{task_id}/messages")
async def list_messages(task_id: str) -> Any:
    return db.list_messages(task_id)


@app.post("/api/tasks/{task_id}/messages")
async def send_message(task_id: str, request: Request) -> Any:
    data = await request.json()
    content = data.get("content", "").strip()
    if not content:
        return JSONResponse({"error": "Content is required"}, 400)

    task = db.get_task(task_id)
    if not task:
        return JSONResponse({"error": "Task not found"}, 404)

    run = await orchestrator.enqueue_message(task_id, content)
    return {"ok": True, "run_id": run["id"], "task_id": task_id}


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
        pass  # Skip unparseable lines (partial JSON, system messages, etc.)


@app.get("/api/tasks/{task_id}/stream")
async def stream_task(task_id: str) -> Any:
    """SSE endpoint for live Claude output on a task's latest run."""
    run = db.get_latest_run(task_id)
    if not run:
        return PlainTextResponse("No runs found", status_code=404)

    run_id = run["id"]

    async def generate():
        pos = 0
        line_buffer = ""
        while True:
            current_run = db.get_run(run_id)
            if not current_run:
                yield "event: done\ndata: {}\n\n"
                return

            session_dir = current_run.get("session_dir")
            if session_dir:
                stdout_path = Path(session_dir) / "stdout.txt"
                if stdout_path.exists():
                    try:
                        raw = stdout_path.read_text(encoding="utf-8", errors="replace")
                        if len(raw) > pos:
                            chunk = raw[pos:]
                            pos = len(raw)
                            # Only process complete lines (avoid partial JSON)
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

            if current_run["status"] not in ("pending", "running"):
                # Flush remaining buffer
                if line_buffer.strip():
                    for event, data in _parse_sse_line(line_buffer):
                        yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                yield "event: done\ndata: {}\n\n"
                return

            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Run Control ──

@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task_run(task_id: str) -> Any:
    run = db.get_latest_run(task_id)
    if not run or run["status"] != "running":
        return JSONResponse({"error": "No running job to cancel"}, 400)
    ok = orchestrator.cancel_run(run["id"])
    return {"ok": ok}


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

    from app.git_worktree import parse_github_remote, get_default_branch, _run, GitWorktreeError
    import asyncio

    repo = settings.project_repo_path(project["slug"])
    checks = []

    # 1. Path exists
    exists = repo.exists()
    checks.append({"name": "Repository path exists", "ok": exists,
                    "detail": str(repo) if exists else f"Path not found: {repo}"})
    if not exists:
        return {"project": project["name"], "checks": checks}

    # 2. Is a git repo
    git_dir = (repo / ".git").exists()
    checks.append({"name": "Is a git repository", "ok": git_dir,
                    "detail": "Yes" if git_dir else "No .git directory found"})
    if not git_dir:
        return {"project": project["name"], "checks": checks}

    # 3. Remote URL
    try:
        remote_url = await asyncio.to_thread(_run, ["git", "-C", str(repo), "remote", "get-url", "origin"])
        checks.append({"name": "Remote 'origin' configured", "ok": True, "detail": remote_url})
    except GitWorktreeError as e:
        checks.append({"name": "Remote 'origin' configured", "ok": False, "detail": str(e)})
        return {"project": project["name"], "checks": checks}

    # 4. GitHub remote parsed
    gh = parse_github_remote(repo)
    if gh:
        checks.append({"name": "GitHub remote detected", "ok": True, "detail": f"{gh[0]}/{gh[1]}"})
    else:
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

    # 7. GitHub API access (if token + GitHub remote)
    if gh and settings.github_token:
        import httpx
        owner, repo_name = gh
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{owner}/{repo_name}",
                    headers={
                        "Authorization": f"Bearer {settings.github_token}",
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


# ── GitHub Actions Status API ──

@app.get("/api/projects/{project_id}/actions")
async def get_actions_status(project_id: str) -> Any:
    """Get recent GitHub Actions workflow runs for a project."""
    project = db.get_project(project_id)
    if not project:
        return JSONResponse({"error": "Not found"}, 404)
    if not settings.github_token:
        return {"runs": [], "error": "No GitHub token configured"}

    from app.git_worktree import parse_github_remote
    repo_path = settings.project_repo_path(project["slug"])
    if not repo_path.exists():
        return {"runs": [], "error": "Repo path not found"}

    gh = parse_github_remote(repo_path)
    if not gh:
        return {"runs": [], "error": "Not a GitHub repo"}

    owner, repo_name = gh
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo_name}/actions/runs",
                headers={
                    "Authorization": f"Bearer {settings.github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"per_page": 5},
            )
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


# ── Mode Detection API ──

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
