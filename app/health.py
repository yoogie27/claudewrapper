"""System health checks and workspace MCP verification."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

# Claude Code config locations
_CLAUDE_JSON = Path.home() / ".claude.json"          # `claude mcp add` writes here (projects section)
_USER_SETTINGS = Path.home() / ".claude" / "settings.json"  # plugins, user-level mcpServers

# MCP servers that should be present in every workspace
REQUIRED_MCP_SERVERS = {
    "linear": {
        "label": "Linear",
        "description": "Linear project management integration",
        "install_cmd": ["claude", "mcp", "add", "--transport", "http", "linear", "https://mcp.linear.app/mcp"],
        "plugin_id": "linear@claude-plugins-official",
    },
}

RECOMMENDED_MCP_SERVERS = {
    "memory": {
        "label": "Memory",
        "description": "Persistent memory across sessions",
        "install_cmd": ["claude", "mcp", "add", "memory", "--", "npx", "-y", "@anthropic/claude-code-memory"],
    },
}


def get_system_health() -> dict:
    """Collect system health metrics."""
    info: dict = {}

    # Disk space
    try:
        usage = shutil.disk_usage("C:\\")
        info["disk"] = {
            "total_gb": round(usage.total / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "used_pct": round((usage.used / usage.total) * 100, 1),
        }
    except Exception:
        info["disk"] = None

    # Memory (Windows via ctypes)
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        info["memory"] = {
            "total_gb": round(stat.ullTotalPhys / (1024**3), 1),
            "free_gb": round(stat.ullAvailPhys / (1024**3), 1),
            "used_pct": stat.dwMemoryLoad,
        }
    except Exception:
        info["memory"] = None

    # Claude Code version
    info["claude_version"] = _run_version("claude", ["claude", "--version"])

    # Git version
    info["git_version"] = _run_version("git", ["git", "--version"])

    # Node version
    info["node_version"] = _run_version("node", ["node", "--version"])

    # Python version
    info["python_version"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    return info


def _run_version(name: str, cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0]
    except Exception:
        pass
    return None


def check_workspace_mcp(workspace_path: str) -> dict:
    """Check which MCP servers are configured for a workspace.

    Claude Code stores MCP config in multiple locations:
    1. `~/.claude.json` projects[path].mcpServers  (`claude mcp add` default, --scope local)
    2. `.mcp.json` in the project root  (alternative local scope)
    3. `~/.claude/settings.json` enabledPlugins  (marketplace plugins)
    """
    p = Path(workspace_path)
    # Normalize the path for lookup in ~/.claude.json (uses forward slashes)
    norm_path = str(p).replace("\\", "/")

    # 1) Project-scoped MCP from ~/.claude.json
    project_servers: dict[str, dict] = {}
    if _CLAUDE_JSON.exists():
        try:
            cdata = json.loads(_CLAUDE_JSON.read_text(encoding="utf-8"))
            projects = cdata.get("projects", {})
            # Try exact match, then case-insensitive
            proj = projects.get(norm_path) or projects.get(str(p))
            if not proj:
                for k, v in projects.items():
                    if k.replace("\\", "/").lower() == norm_path.lower():
                        proj = v
                        break
            if proj:
                project_servers = proj.get("mcpServers", {})
        except Exception:
            pass

    # 2) Local .mcp.json in project root (rarely used, but supported)
    local_config = p / ".mcp.json"
    local_servers: dict[str, dict] = {}
    if local_config.exists():
        try:
            data = json.loads(local_config.read_text(encoding="utf-8"))
            local_servers = data if isinstance(data, dict) else {}
        except Exception:
            pass

    # 3) User-level plugins from ~/.claude/settings.json
    enabled_plugins: dict[str, bool] = {}
    if _USER_SETTINGS.exists():
        try:
            udata = json.loads(_USER_SETTINGS.read_text(encoding="utf-8"))
            enabled_plugins = udata.get("enabledPlugins", {})
        except Exception:
            pass

    # Merge all known servers
    all_installed = {**local_servers, **project_servers}

    results: dict[str, dict] = {}

    for key, info in REQUIRED_MCP_SERVERS.items():
        present = _is_server_present(key, all_installed)
        plugin_id = info.get("plugin_id", "")
        if plugin_id and enabled_plugins.get(plugin_id):
            present = True
        source = _detect_source(key, project_servers, local_servers, plugin_id, enabled_plugins)
        results[key] = {
            "label": info["label"],
            "description": info["description"],
            "required": True,
            "installed": present,
            "source": source,
        }

    for key, info in RECOMMENDED_MCP_SERVERS.items():
        present = _is_server_present(key, all_installed)
        plugin_id = info.get("plugin_id", "")
        if plugin_id and enabled_plugins.get(plugin_id):
            present = True
        source = _detect_source(key, project_servers, local_servers, plugin_id, enabled_plugins)
        results[key] = {
            "label": info["label"],
            "description": info["description"],
            "required": False,
            "installed": present,
            "source": source,
        }

    config_exists = bool(project_servers) or local_config.exists()
    return {
        "workspace": workspace_path,
        "config_exists": config_exists,
        "servers": results,
        "all_required_ok": all(
            r["installed"] for r in results.values() if r["required"]
        ),
    }


def _detect_source(
    key: str,
    project_servers: dict,
    local_servers: dict,
    plugin_id: str,
    enabled_plugins: dict,
) -> str:
    """Return where a server is configured."""
    if _is_server_present(key, project_servers):
        return "project (~/.claude.json)"
    if _is_server_present(key, local_servers):
        return "local (.mcp.json)"
    if plugin_id and enabled_plugins.get(plugin_id):
        return "plugin"
    return ""


def _is_server_present(key: str, installed: dict) -> bool:
    """Check if a server key (or close variant) is in the installed dict."""
    if key in installed:
        return True
    # Fuzzy match: "linear-server" matches "linear", etc.
    for name in installed:
        if key in name or name in key:
            return True
    return False


def install_mcp_server(workspace_path: str, server_key: str) -> dict:
    """Install an MCP server into a workspace via `claude mcp add`."""
    all_servers = {**REQUIRED_MCP_SERVERS, **RECOMMENDED_MCP_SERVERS}
    if server_key not in all_servers:
        return {"ok": False, "error": f"Unknown server: {server_key}"}

    info = all_servers[server_key]
    cmd = info["install_cmd"]

    try:
        result = subprocess.run(
            cmd,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode == 0:
            return {"ok": True, "output": result.stdout.strip()}
        return {"ok": False, "error": result.stderr.strip() or result.stdout.strip()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
