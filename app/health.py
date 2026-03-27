"""System health checks."""
from __future__ import annotations

import shutil
import subprocess
import sys


def get_system_health() -> dict:
    """Collect system health metrics."""
    info: dict = {}

    # Disk space
    try:
        disk_root = "C:\\" if sys.platform == "win32" else "/"
        usage = shutil.disk_usage(disk_root)
        info["disk"] = {
            "total_gb": round(usage.total / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "used_pct": round((usage.used / usage.total) * 100, 1),
        }
    except Exception:
        info["disk"] = None

    # Memory
    try:
        if sys.platform == "win32":
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
        else:
            meminfo: dict[str, int] = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        meminfo[parts[0].rstrip(":")] = int(parts[1]) * 1024
            total = meminfo.get("MemTotal", 0)
            avail = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            info["memory"] = {
                "total_gb": round(total / (1024**3), 1),
                "free_gb": round(avail / (1024**3), 1),
                "used_pct": round(((total - avail) / total) * 100, 1) if total else 0,
            }
    except Exception:
        info["memory"] = None

    # Claude Code version
    info["claude_version"] = _run_version(["claude", "--version"])
    info["git_version"] = _run_version(["git", "--version"])
    info["python_version"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    return info


def _run_version(cmd: list[str]) -> str | None:
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
