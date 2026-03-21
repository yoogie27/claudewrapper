from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.db import Database
from app.health import get_system_health, check_workspace_mcp, install_mcp_server
from app.linear_client import LinearClient
from app.orchestrator import Orchestrator, DEFAULT_PROMPT
from app.ssh import setup_ssh, get_public_key, get_git_ssh_env
from app.repo_manager import clone_or_fetch, get_clone_status, parse_repo_info
from app.sanitize import validate_identifier


db = Database(settings.data_path() / "app.db")
orchestrator = Orchestrator(settings, db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SSH keys if configured
    if settings.ssh_key_dir:
        try:
            setup_ssh(Path(settings.ssh_key_dir))
        except Exception as exc:
            import logging
            logging.getLogger("claudewrapper").warning("SSH setup failed: %s", exc)
    await orchestrator.start()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Any:
    jobs = db.list_jobs(50)
    sessions = db.list_sessions(50)
    mappings = db.list_team_mappings()
    last_poll = db.get_config("last_poll")
    # Build identifier -> title lookup from issue_state
    identifiers = {s["identifier"] for s in sessions} | {j["identifier"] for j in jobs}
    titles = {}
    for ident in identifiers:
        row = db.get_issue_by_identifier(ident)
        if row and row["last_title"]:
            titles[ident] = row["last_title"]
    queue_paused = orchestrator.is_queue_paused()
    queue_paused_reason = db.get_config("queue_paused_reason") or ""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "jobs": jobs,
            "sessions": sessions,
            "mappings": mappings,
            "last_poll": last_poll,
            "settings": settings,
            "titles": titles,
            "active": "dashboard",
            "queue_paused": queue_paused,
            "queue_paused_reason": queue_paused_reason,
        },
    )


@app.get("/teams", response_class=HTMLResponse)
async def teams(request: Request) -> Any:
    async with LinearClient(settings.linear_api_key) as client:
        try:
            teams = await client.get_teams()
        except Exception as exc:
            teams = []
            error = str(exc)
        else:
            error = None

    mappings = {m["team_id"]: m for m in db.list_team_mappings()}
    root_dirs = [r for r in settings.repo_root_paths() if r.exists()]

    # Build per-team label instructions map
    all_instructions = db.list_label_instructions()
    label_map: dict[str, list] = {}
    for row in all_instructions:
        label_map.setdefault(row["team_id"], []).append(row)

    # Build paused state map for each team
    paused_teams: dict[str, bool] = {}
    for t in teams:
        paused_teams[t["id"]] = db.is_team_paused(t["id"])

    return templates.TemplateResponse(
        "teams.html",
        {
            "request": request,
            "teams": teams,
            "mappings": mappings,
            "error": error,
            "root_dirs": root_dirs,
            "label_map": label_map,
            "paused_teams": paused_teams,
            "active": "teams",
            "default_prompt": DEFAULT_PROMPT,
        },
    )


@app.post("/api/mapping")
async def set_mapping(
    team_id: str = Form(...),
    team_name: str = Form(...),
    local_path: str = Form(""),
    default_prompt: str = Form(""),
    enabled: int = Form(0),
    auto_process: int = Form(0),
    auto_merge: int = Form(0),
    github_repo_url: str = Form(""),
    base_branch: str = Form(""),
) -> Any:
    github_repo_url = github_repo_url.strip()
    base_branch = base_branch.strip()

    # If a GitHub URL is provided and local_path is empty, or repos_dir is configured, trigger auto-clone
    if github_repo_url and (not local_path or settings.repos_dir):
        # Use configured REPOS_DIR if set, else fall back to DATA_DIR/repos
        repos_dir = Path(settings.repos_dir) if settings.repos_dir else settings.data_path() / "repos"
        ssh_env = _get_ssh_env()
        db.upsert_team_mapping(
            team_id, team_name, local_path, default_prompt,
            bool(int(enabled)), auto_process=bool(auto_process),
            auto_merge=bool(auto_merge), github_repo_url=github_repo_url,
            base_branch=base_branch,
        )
        db.update_clone_status(team_id, "cloning")

        async def _do_clone():
            try:
                ok, msg, path = await asyncio.to_thread(clone_or_fetch, github_repo_url, repos_dir, ssh_env)
                if ok and path:
                    db.update_clone_status(team_id, "cloned", str(path))
                else:
                    db.update_clone_status(team_id, f"error: {msg}")
            except Exception as exc:
                db.update_clone_status(team_id, f"error: {exc}")

        asyncio.create_task(_do_clone())
    else:
        db.upsert_team_mapping(
            team_id, team_name, local_path, default_prompt,
            bool(int(enabled)), auto_process=bool(auto_process),
            auto_merge=bool(auto_merge), github_repo_url=github_repo_url,
            base_branch=base_branch,
        )
    return RedirectResponse(url="/teams", status_code=303)


def _get_ssh_env() -> dict[str, str] | None:
    """Helper to build SSH env from settings."""
    if not settings.ssh_key_dir:
        return None
    key_dir = Path(settings.ssh_key_dir)
    if (key_dir / "id_ed25519").exists():
        return get_git_ssh_env(key_dir)
    return None


@app.post("/api/label-instruction")
async def save_label_instruction(
    team_id: str = Form(...),
    label_name: str = Form(...),
    instruction: str = Form(...),
) -> Any:
    db.upsert_label_instruction(team_id, label_name.strip(), instruction.strip())
    return RedirectResponse(url="/teams", status_code=303)


@app.post("/api/label-instruction/delete")
async def delete_label_instruction(
    team_id: str = Form(...),
    label_name: str = Form(...),
) -> Any:
    db.delete_label_instruction(team_id, label_name)
    return RedirectResponse(url="/teams", status_code=303)


@app.get("/files", response_class=HTMLResponse)
async def file_browser(request: Request, path: str | None = None) -> Any:
    roots = [Path(m["local_path"]).resolve() for m in db.list_team_mappings() if m["enabled"]]
    roots += [r for r in settings.repo_root_paths() if r.exists()]
    wt_root = Path(settings.worktree_root).resolve()
    if wt_root.exists() and wt_root not in roots:
        roots.append(wt_root)
    roots = [r for r in roots if r.exists()]

    if not path:
        return templates.TemplateResponse(
            "files.html",
            {"request": request, "entries": [], "path": "", "roots": roots, "parent_path": None, "active": "files"},
        )

    p = Path(path).resolve()
    def _allowed(target: Path, root: Path) -> bool:
        try:
            return target.is_relative_to(root)
        except Exception:
            return False

    if not any(_allowed(p, r) for r in roots):
        return PlainTextResponse("Path not allowed", status_code=403)

    if p.is_file():
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            content = "(binary or unreadable)"
        return templates.TemplateResponse(
            "files.html",
            {"request": request, "entries": [], "path": str(p), "roots": roots, "file_content": content, "parent_path": str(p.parent), "active": "files"},
        )

    entries = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        entries.append({"name": child.name, "path": str(child), "is_dir": child.is_dir()})

    return templates.TemplateResponse(
        "files.html",
        {"request": request, "entries": entries, "path": str(p), "roots": roots, "parent_path": str(p.parent), "active": "files"},
    )


@app.get("/api/status")
async def status() -> Any:
    jobs = [dict(r) for r in db.list_jobs(50)]
    sessions = [dict(r) for r in db.list_sessions(50)]
    mappings = [dict(r) for r in db.list_team_mappings()]
    # Build identifier -> title lookup
    identifiers = {s["identifier"] for s in sessions} | {j["identifier"] for j in jobs}
    titles = {}
    for ident in identifiers:
        row = db.get_issue_by_identifier(ident)
        if row and row["last_title"]:
            titles[ident] = row["last_title"]
    return JSONResponse(
        {
            "jobs": jobs,
            "sessions": sessions,
            "mappings": mappings,
            "titles": titles,
            "last_poll": db.get_config("last_poll"),
            "test_mode": settings.test_mode,
            "queue_paused": orchestrator.is_queue_paused(),
            "queue_paused_reason": db.get_config("queue_paused_reason") or "",
            "status_mapping": {
                "hitl": db.get_config("status_hitl") or settings.hitl_state_name,
                "review": db.get_config("status_review") or settings.review_state_name,
                "done": db.get_config("status_done") or settings.done_state_name,
                "error": db.get_config("status_error") or settings.error_state_name,
            },
        }
    )


@app.get("/api/health")
async def api_health() -> Any:
    """System health metrics — polled by the dashboard."""
    health = await asyncio.to_thread(get_system_health)
    return JSONResponse(health)


@app.get("/api/ssh/public-key")
async def api_ssh_public_key() -> Any:
    """Return the SSH public key for display in the UI."""
    if not settings.ssh_key_dir:
        return JSONResponse({"error": "SSH_KEY_DIR not configured"}, status_code=404)
    key = get_public_key(Path(settings.ssh_key_dir))
    if not key:
        return JSONResponse({"error": "No SSH key generated yet"}, status_code=404)
    return JSONResponse({"public_key": key})


@app.get("/api/repo/status")
async def api_repo_status(team_id: str) -> Any:
    """Check clone status for a team's GitHub repo."""
    mapping = db.get_team_mapping(team_id)
    if not mapping or not mapping["github_repo_url"]:
        return JSONResponse({"status": "not_configured", "message": "No GitHub repo URL configured"})
    if not settings.repos_dir:
        return JSONResponse({"status": "error", "message": "REPOS_DIR not configured"})
    status = get_clone_status(Path(settings.repos_dir), mapping["github_repo_url"])
    status["clone_status"] = mapping["clone_status"] or ""
    return JSONResponse(status)


@app.post("/api/repo/clone")
async def api_repo_clone(team_id: str = Form(...)) -> Any:
    """Trigger a clone/fetch for a team's GitHub repo."""
    mapping = db.get_team_mapping(team_id)
    if not mapping or not mapping["github_repo_url"]:
        return JSONResponse({"ok": False, "error": "No GitHub repo URL configured"}, status_code=400)

    # Use configured REPOS_DIR if set, else fall back to DATA_DIR/repos
    repos_dir = Path(settings.repos_dir) if settings.repos_dir else settings.data_path() / "repos"
    ssh_env = _get_ssh_env()
    db.update_clone_status(team_id, "cloning")

    async def _do_clone():
        try:
            ok, msg, path = await asyncio.to_thread(clone_or_fetch, mapping["github_repo_url"], repos_dir, ssh_env)
            if ok and path:
                db.update_clone_status(team_id, "cloned", str(path))
            else:
                db.update_clone_status(team_id, f"error: {msg}")
        except Exception as exc:
            db.update_clone_status(team_id, f"error: {exc}")

    asyncio.create_task(_do_clone())
    return JSONResponse({"ok": True, "message": "Clone started"})


@app.get("/api/workspace/check")
async def api_workspace_check(path: str) -> Any:
    """Check MCP server status for a workspace."""
    result = await asyncio.to_thread(check_workspace_mcp, path)
    return JSONResponse(result)


@app.post("/api/workspace/install-mcp")
async def api_install_mcp(
    path: str = Form(...),
    server: str = Form(...),
) -> Any:
    """Install an MCP server into a workspace."""
    result = await asyncio.to_thread(install_mcp_server, path, server)
    return JSONResponse(result)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> Any:
    values = {
        "hitl": db.get_config("status_hitl") or settings.hitl_state_name,
        "review": db.get_config("status_review") or settings.review_state_name,
        "done": db.get_config("status_done") or settings.done_state_name,
        "error": db.get_config("status_error") or settings.error_state_name,
    }
    # Fetch available states from the first enabled team for reference
    available_states: list[dict] = []
    enabled_mappings = [m for m in db.list_team_mappings() if m["enabled"]]
    if enabled_mappings and settings.linear_api_key:
        try:
            async with LinearClient(settings.linear_api_key) as client:
                available_states = await client.get_workflow_states(enabled_mappings[0]["team_id"])
        except Exception:
            pass
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "values": values,
        "available_states": available_states,
        "active": "settings",
        "settings": settings,
    })


@app.post("/api/settings")
async def update_settings(
    status_hitl: str = Form(""),
    status_review: str = Form(""),
    status_done: str = Form(""),
    status_error: str = Form(""),
) -> Any:
    db.set_config("status_hitl", status_hitl.strip())
    db.set_config("status_review", status_review.strip())
    db.set_config("status_done", status_done.strip())
    db.set_config("status_error", status_error.strip())
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/api/team/start-all")
async def api_team_start_all(team_id: str = Form(...)) -> Any:
    """Enqueue all open tickets for a team."""
    await orchestrator.enqueue_team_tickets(team_id)
    return RedirectResponse("/teams", status_code=303)


@app.post("/api/team/pause")
async def api_team_pause(team_id: str = Form(...)) -> Any:
    """Pause/resume processing for a team."""
    currently_paused = db.is_team_paused(team_id)
    db.set_team_paused(team_id, not currently_paused)
    return RedirectResponse("/teams", status_code=303)


@app.post("/api/poll-now")
async def poll_now() -> Any:
    """Trigger an immediate Linear poll."""
    try:
        found = await orchestrator.poll_now()
        return JSONResponse({"ok": True, "found": found})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/queue/pause")
async def queue_pause() -> Any:
    """Pause the global job queue."""
    orchestrator.set_queue_paused(True, "Manual pause via dashboard")
    return JSONResponse({"ok": True, "paused": True})


@app.post("/api/queue/resume")
async def queue_resume() -> Any:
    """Resume the global job queue."""
    orchestrator.set_queue_paused(False)
    return JSONResponse({"ok": True, "paused": False})


@app.post("/api/cancel")
async def cancel_job(request: Request, job_id: int = Form(...)) -> Any:
    sent = orchestrator.cancel_job(job_id)
    if not sent:
        # Process not found — job may have died without updating the DB.
        # Force-mark it as failed so it stops showing as running.
        db.update_job_status(job_id, "failed")
        db._conn.execute(
            "UPDATE sessions SET status='failed', ended_at=datetime('now'), last_activity_at=datetime('now') WHERE run_id=?",
            (job_id,),
        )
        db._conn.commit()
    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)


@app.post("/api/merge")
async def merge_pr(request: Request, run_id: int = Form(...)) -> Any:
    ok, msg = await orchestrator.merge_session_pr(run_id)
    # If the request came from a form (not fetch), redirect back
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        status_code = 200 if ok else 400
        return JSONResponse({"ok": ok, "message": msg}, status_code=status_code)
    if not ok:
        # Redirect back with error as query param so the UI can show a toast
        referer = request.headers.get("referer", "/")
        sep = "&" if "?" in referer else "?"
        return RedirectResponse(url=f"{referer}{sep}error={msg}", status_code=303)
    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)


@app.post("/api/retry")
async def retry_ticket(identifier: str = Form(...)) -> Any:
    logger.info("Retry requested for: %s", identifier)
    issue = db.get_issue_by_identifier(identifier)
    if not issue:
        logger.warning("Issue not found: %s", identifier)
        return PlainTextResponse("Unknown ticket", status_code=404)
    logger.info("Enqueueing retry job for %s (issue_id=%s, team_id=%s)", identifier, issue["issue_id"], issue["team_id"])
    db.enqueue_job(issue["issue_id"], issue["identifier"], issue["team_id"], "retry", force=True)
    logger.info("Retry job enqueued successfully for %s", identifier)
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/retry-job")
async def retry_job(identifier: str = Form(...)) -> Any:
    issue = db.get_issue_by_identifier(identifier)
    if not issue:
        return PlainTextResponse("Unknown ticket", status_code=404)
    db.enqueue_job(issue["issue_id"], issue["identifier"], issue["team_id"], "retry_job", force=True)
    return RedirectResponse(url="/", status_code=303)


@app.post("/api/reprocess")
async def reprocess_session(request: Request, identifier: str = Form(...)) -> Any:
    """Re-run post-processing (comment, state, PR) without re-running Claude."""
    ok, msg = await orchestrator.reprocess_session(identifier)
    if not ok:
        return PlainTextResponse(msg, status_code=400)
    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)


@app.post("/api/cleanup")
async def cleanup_session(request: Request, identifier: str = Form(...)) -> Any:
    """Remove worktree, session files, and DB row for a ticket."""
    ok, msg = orchestrator.cleanup_session(identifier)
    # Check if the request came from a form (not fetch)
    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        status_code = 200 if ok else 400
        return JSONResponse({"ok": ok, "message": msg}, status_code=status_code)
    if not ok:
        return PlainTextResponse(msg, status_code=400)
    referer = request.headers.get("referer", "/")
    return RedirectResponse(url=referer, status_code=303)


@app.post("/api/trigger")
async def trigger_ticket(
    identifier: str = Form(""),
    issue_id: str = Form(""),
    team_id: str = Form(""),
) -> Any:
    if issue_id and team_id and identifier:
        try:
            identifier = validate_identifier(identifier)
        except ValueError:
            return PlainTextResponse("Invalid identifier format", status_code=400)
        db.enqueue_job(issue_id, identifier, team_id, "manual_trigger")
        return RedirectResponse(url="/", status_code=303)
    return PlainTextResponse("Provide identifier, issue_id and team_id", status_code=400)


@app.post("/api/lookup")
async def lookup_identifier(identifier: str = Form(...)) -> Any:
    async with LinearClient(settings.linear_api_key) as client:
        try:
            issue = await client.get_issue_by_identifier(identifier)
        except Exception as exc:
            return PlainTextResponse(f"Lookup failed: {exc}", status_code=400)
    return JSONResponse(
        {
            "identifier": issue["identifier"],
            "issue_id": issue["id"],
            "team_id": issue["team"]["id"],
            "team_name": issue["team"]["name"],
            "title": issue["title"],
        }
    )


@app.get("/sessions/{identifier}/prompt", response_class=HTMLResponse)
async def session_prompt(request: Request, identifier: str) -> Any:
    session = db.get_latest_session_by_identifier(identifier)
    if not session:
        return PlainTextResponse("Session not found", status_code=404)
    prompt_path = Path(session["session_dir"]) / "prompt.txt"
    if not prompt_path.exists():
        return PlainTextResponse("Prompt not found", status_code=404)
    content = prompt_path.read_text(encoding="utf-8", errors="replace")
    return templates.TemplateResponse(
        "prompt.html",
        {"request": request, "identifier": identifier, "content": content},
    )


@app.get("/sessions/{identifier}/log", response_class=HTMLResponse)
async def session_log(request: Request, identifier: str) -> Any:
    session = db.get_latest_session_by_identifier(identifier)
    if not session:
        return PlainTextResponse("Session not found", status_code=404)
    stdout_path = Path(session["session_dir"]) / "stdout.txt"
    stderr_path = Path(session["session_dir"]) / "stderr.txt"
    existing = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    stderr_content = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    pr_url = session["pr_url"] or ""
    return templates.TemplateResponse(
        "log.html",
        {
            "request": request,
            "identifier": identifier,
            "status": session["status"],
            "existing": existing,
            "stderr_content": stderr_content,
            "job_id": session["run_id"],
            "pr_url": pr_url,
        },
    )


@app.get("/sessions/{identifier}/log/stream")
async def stream_log(identifier: str) -> Any:
    session = db.get_latest_session_by_identifier(identifier)
    if not session:
        return PlainTextResponse("Session not found", status_code=404)

    stdout_path = Path(session["session_dir"]) / "stdout.txt"

    async def generate():
        pos = 0
        while True:
            if stdout_path.exists():
                try:
                    content = stdout_path.read_text(encoding="utf-8", errors="replace")
                    if len(content) > pos:
                        chunk = content[pos:]
                        pos = len(content)
                        yield f"data: {json.dumps(chunk)}\n\n"
                except Exception:
                    pass

            current = db.get_latest_session_by_identifier(identifier)
            if not current or current["status"] not in ("running",):
                # Flush any final content
                if stdout_path.exists():
                    try:
                        content = stdout_path.read_text(encoding="utf-8", errors="replace")
                        if len(content) > pos:
                            yield f"data: {json.dumps(content[pos:])}\n\n"
                    except Exception:
                        pass
                yield "event: done\ndata: {}\n\n"
                return

            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/tickets", response_class=HTMLResponse)
async def tickets_page(request: Request) -> Any:
    mappings = [m for m in db.list_team_mappings() if m["enabled"]]
    return templates.TemplateResponse(
        "tickets.html",
        {"request": request, "mappings": mappings, "active": "tickets"},
    )


@app.get("/api/tickets")
async def api_tickets(team_id: str, status: str = "open") -> Any:
    """Fetch tickets for a team. status: open|active|backlog|closed|all"""
    state_map = {
        "open": ["started", "unstarted", "backlog", "triage"],
        "active": ["started", "unstarted"],
        "backlog": ["backlog", "triage"],
        "closed": ["completed", "canceled"],
        "all": None,
    }
    state_types = state_map.get(status)
    async with LinearClient(settings.linear_api_key) as client:
        try:
            issues = await client.get_team_issues(team_id, state_types=state_types, first=80)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"issues": issues})


@app.get("/api/ticket/{issue_id}")
async def api_ticket_detail(issue_id: str) -> Any:
    """Fetch full ticket details including comments."""
    async with LinearClient(settings.linear_api_key) as client:
        try:
            issue = await client.get_issue_details(issue_id)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    # Check if we have a local session for this ticket
    session = db.get_session(issue_id)
    return JSONResponse({
        "issue": issue,
        "session": dict(session) if session else None,
    })


@app.get("/api/search")
async def api_search(q: str) -> Any:
    """Search issues across all teams."""
    async with LinearClient(settings.linear_api_key) as client:
        try:
            results = await client.search_issues(q, first=30)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"issues": results})


@app.post("/api/run")
async def run_ticket(
    issue_id: str = Form(...),
    identifier: str = Form(...),
    team_id: str = Form(...),
) -> Any:
    """Enqueue a Claude run for a ticket (from the ticket browser)."""
    # Ensure issue state is tracked
    db.upsert_issue_state(
        issue_id=issue_id,
        identifier=identifier,
        team_id=team_id,
        state_type="started",
        state_name="In Progress",
        last_comment_at=None,
        last_seen_at="",
        last_updated_at="",
        title=identifier,
    )
    db.enqueue_job(issue_id, identifier, team_id, "manual_run")
    return JSONResponse({"ok": True, "message": f"Queued {identifier}"})


@app.get("/api/preview")
async def preview_prompt(issue_id: str) -> Any:
    async with LinearClient(settings.linear_api_key) as client:
        issue = await client.get_issue_details(issue_id)
        mapping = db.get_team_mapping(issue["team"]["id"])
        if not mapping:
            return PlainTextResponse("No team mapping for issue team", status_code=400)
        closed = await client.get_recent_closed_issues(issue["team"]["id"], first=20)
    label_instructions = db.list_label_instructions(issue["team"]["id"])
    prompt = orchestrator._build_prompt(issue, closed, mapping["default_prompt"], "preview", label_instructions)
    return JSONResponse({"prompt": prompt})


@app.get("/picker", response_class=HTMLResponse)
async def picker(request: Request, team_id: str, team_name: str, path: str | None = None) -> Any:
    roots = [r for r in settings.repo_root_paths() if r.exists()]
    if not roots:
        return PlainTextResponse("No REPO_ROOTS configured", status_code=400)

    def _allowed(target: Path, root: Path) -> bool:
        try:
            return target.is_relative_to(root)
        except Exception:
            return False

    if not path:
        entries = [{"name": r.name, "path": str(r), "is_dir": True} for r in roots]
        return templates.TemplateResponse(
            "picker.html",
            {"request": request, "entries": entries, "path": "", "team_id": team_id, "team_name": team_name, "parent_path": None},
        )

    p = Path(path).resolve()
    root = next((r for r in roots if _allowed(p, r)), None)
    if not root:
        return PlainTextResponse("Path not allowed", status_code=403)

    if not p.is_dir():
        return PlainTextResponse("Not a directory", status_code=400)

    entries = []
    ignore = settings.repo_ignore_set()
    max_depth = settings.repo_max_depth
    rel_depth = len(p.relative_to(root).parts)
    if rel_depth > max_depth:
        return PlainTextResponse("Max depth reached", status_code=400)
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if child.is_dir() and child.name.lower() not in ignore:
            entries.append({"name": child.name, "path": str(child), "is_dir": True})

    mapping = db.get_team_mapping(team_id)
    default_prompt = mapping["default_prompt"] if mapping else DEFAULT_PROMPT
    enabled = 1 if (mapping and mapping["enabled"]) else 0
    auto_process = mapping["auto_process"] if mapping else 1
    auto_merge = mapping["auto_merge"] if mapping else 0

    return templates.TemplateResponse(
        "picker.html",
        {
            "request": request,
            "entries": entries,
            "path": str(p),
            "team_id": team_id,
            "team_name": team_name,
            "parent_path": str(p.parent),
            "default_prompt": default_prompt,
            "enabled": enabled,
            "auto_process": auto_process,
            "auto_merge": auto_merge,
        },
    )


@app.post("/api/webhook/linear")
async def linear_webhook(request: Request) -> Any:
    body = await request.body()

    if settings.linear_webhook_secret:
        signature = request.headers.get("Linear-Signature", "")
        expected = hmac.new(
            settings.linear_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return PlainTextResponse("Invalid signature", status_code=401)

    try:
        payload = json.loads(body)
    except Exception:
        return PlainTextResponse("Invalid JSON", status_code=400)

    action = payload.get("action")
    event_type = payload.get("type")
    data = payload.get("data", {})

    if event_type == "Issue" and action in ("create", "update"):
        issue_id = data.get("id")
        identifier = data.get("identifier")
        team = data.get("team") or {}
        team_id = team.get("id")
        state = data.get("state") or {}
        state_type = state.get("type")

        if not (issue_id and identifier and team_id):
            return JSONResponse({"ok": True})

        # Validate identifier format to prevent path traversal / injection
        try:
            identifier = validate_identifier(identifier)
        except ValueError:
            return JSONResponse({"ok": False, "error": "Invalid identifier"}, status_code=400)

        mapping = db.get_team_mapping(team_id)
        if not (mapping and mapping["enabled"]):
            return JSONResponse({"ok": True})

        reason = "new" if action == "create" else None
        if reason is None and state_type not in ("completed", "canceled"):
            prev = db.get_issue_state(issue_id)
            if prev and prev["last_state_type"] in ("completed", "canceled"):
                reason = "reopened"

        if reason:
            db.upsert_issue_state(
                issue_id=issue_id,
                identifier=identifier,
                team_id=team_id,
                state_type=state_type,
                state_name=state.get("name"),
                last_comment_at=None,
                last_seen_at=data.get("updatedAt", ""),
                last_updated_at=data.get("updatedAt", ""),
                title=data.get("title"),
            )
            db.enqueue_job(issue_id, identifier, team_id, reason)

    elif event_type == "Comment" and action == "create":
        issue_id = data.get("issueId")
        user = data.get("user") or {}
        user_id = user.get("id", "")
        user_email = user.get("email", "").lower()

        # Skip comments we posted ourselves (identified by marker in body)
        if Orchestrator.is_own_comment(data.get("body", "")):
            return JSONResponse({"ok": True})

        ignored_ids = settings.ignored_author_ids()
        ignored_emails = settings.ignored_author_emails()
        if user_id in ignored_ids or user_email in ignored_emails:
            return JSONResponse({"ok": True})

        if issue_id:
            issue_state = db.get_issue_state(issue_id)
            if issue_state:
                mapping = db.get_team_mapping(issue_state["team_id"])
                if mapping and mapping["enabled"]:
                    db.enqueue_job(issue_id, issue_state["identifier"], issue_state["team_id"], "comment")

    return JSONResponse({"ok": True})


def run() -> None:
    import uvicorn
    import logging

    # Custom access log filter to suppress noisy health check endpoints
    class HealthCheckFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            # Only log if it's NOT a health check endpoint
            msg = record.getMessage()
            return "/api/status" not in msg and "/api/health" not in msg

    uvicorn_logger = logging.getLogger("uvicorn.access")
    uvicorn_logger.addFilter(HealthCheckFilter())

    uvicorn.run(app, host=settings.web_host, port=settings.web_port)


if __name__ == "__main__":
    run()
