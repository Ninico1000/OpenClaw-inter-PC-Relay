"""
agent_core.py – Gemeinsame Agent-Logik

Enthält alle Aktionen und den Nachrichten-Handler,
die von agent.py (Root) und client/agent.py geteilt werden.

Unterstützte Aktionen:
    run_code        – Code in python/bash/shell/powershell ausführen
    open_app        – Anwendung plattformspezifisch starten
    read_file       – Dateiinhalt lesen
    write_file      – Datei schreiben oder anhängen
    list_directory  – Verzeichnisinhalt auflisten
    get_system_info – CPU, RAM, Disk, Plattform-Infos
    screenshot      – Screenshot als Base64-PNG
    get_processes   – Laufende Prozesse auflisten
"""

import asyncio
import base64
import io
import json
import logging
import os
import platform
import subprocess
import sys
from datetime import datetime
from typing import Any, Optional

import websockets
import websockets.exceptions

_SYSTEM: str = platform.system().lower()
log = logging.getLogger("agent")

_MAX_FILE_BYTES: int = 5 * 1024 * 1024  # 5 MB Lesegrenzen für read_file


# ---------------------------------------------------------------------------
# Aktion: run_code
# ---------------------------------------------------------------------------
async def run_code(payload: dict[str, Any]) -> dict[str, Any]:
    """Führt Code in python/bash/shell/powershell aus."""
    language: str = payload.get("language", "bash").lower()
    code: str = payload["code"]
    timeout: int = int(payload.get("timeout", 30))
    cwd: Optional[str] = payload.get("cwd") or None

    if language == "python":
        cmd = [sys.executable, "-c", code]
    elif language == "bash":
        cmd = ["bash", "-c", code] if _SYSTEM == "windows" else ["/bin/bash", "-c", code]
    elif language == "shell":
        cmd = ["cmd", "/c", code] if _SYSTEM == "windows" else ["/bin/sh", "-c", code]
    elif language == "powershell":
        binary = "powershell" if _SYSTEM == "windows" else "pwsh"
        cmd = [binary, "-NonInteractive", "-NoProfile", "-Command", code]
    else:
        return {
            "stdout": "",
            "stderr": f"Unbekannte Sprache: {language!r}",
            "exit_code": -1,
            "error": f"unknown_language:{language}",
        }

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=float(timeout)
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {
                "stdout": "",
                "stderr": f"Prozess nach {timeout}s abgebrochen (Timeout)",
                "exit_code": -1,
                "error": "timeout",
            }

        return {
            "stdout": stdout_b.decode("utf-8", errors="replace"),
            "stderr": stderr_b.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode,
            "error": None,
        }

    except FileNotFoundError:
        return {
            "stdout": "",
            "stderr": f"Interpreter für '{language}' nicht gefunden",
            "exit_code": -1,
            "error": "interpreter_not_found",
        }
    except Exception as exc:
        return {"stdout": "", "stderr": str(exc), "exit_code": -1, "error": "exception"}


# ---------------------------------------------------------------------------
# Aktion: open_app
# ---------------------------------------------------------------------------
def open_app_sync(
    app_name: str,
    args: list[str],
    app_whitelist: Optional[list[str]],
) -> dict[str, Any]:
    """Startet eine Anwendung plattformspezifisch im Hintergrund (synchron, Thread-Pool)."""
    if app_whitelist is not None and app_name not in app_whitelist:
        return {"pid": None, "error": f"App '{app_name}' steht nicht auf der Whitelist"}

    try:
        if _SYSTEM == "windows":
            proc = subprocess.Popen(
                [app_name] + args,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        elif _SYSTEM == "darwin":
            try:
                proc = subprocess.Popen(["open", "-a", app_name] + args, start_new_session=True)
            except FileNotFoundError:
                proc = subprocess.Popen([app_name] + args, start_new_session=True)
        else:
            proc = subprocess.Popen([app_name] + args, start_new_session=True, close_fds=True)

        return {"pid": proc.pid, "error": None}

    except FileNotFoundError:
        return {"pid": None, "error": f"App '{app_name}' nicht gefunden"}
    except Exception as exc:
        return {"pid": None, "error": str(exc)}


async def open_app(
    payload: dict[str, Any],
    app_whitelist: Optional[list[str]],
) -> dict[str, Any]:
    """Async-Wrapper: führt open_app_sync im Thread-Pool aus."""
    app_name: str = payload["app"]
    args: list[str] = payload.get("args", [])
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, open_app_sync, app_name, args, app_whitelist)


# ---------------------------------------------------------------------------
# Aktion: read_file
# ---------------------------------------------------------------------------
async def read_file(payload: dict[str, Any]) -> dict[str, Any]:
    """Liest den Inhalt einer Datei (max. 5 MB)."""
    path: str = os.path.expanduser(payload.get("path", ""))
    encoding: str = payload.get("encoding", "utf-8")
    max_bytes: int = min(int(payload.get("max_bytes", _MAX_FILE_BYTES)), _MAX_FILE_BYTES)

    if not path:
        return {"content": None, "size": 0, "error": "Kein Pfad angegeben"}

    try:
        size = os.path.getsize(path)
        if size > max_bytes:
            return {
                "content": None,
                "size": size,
                "error": f"Datei zu groß ({size} Bytes, Limit: {max_bytes} Bytes)",
            }
        with open(path, "r", encoding=encoding, errors="replace") as f:
            content = f.read()
        return {"content": content, "size": size, "error": None}
    except FileNotFoundError:
        return {"content": None, "size": 0, "error": f"Datei nicht gefunden: {path}"}
    except PermissionError:
        return {"content": None, "size": 0, "error": f"Keine Leseberechtigung: {path}"}
    except Exception as exc:
        return {"content": None, "size": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# Aktion: write_file
# ---------------------------------------------------------------------------
async def write_file(payload: dict[str, Any]) -> dict[str, Any]:
    """Schreibt oder hängt Inhalt an eine Datei an."""
    path: str = os.path.expanduser(payload.get("path", ""))
    content: str = payload.get("content", "")
    mode: str = payload.get("mode", "w")
    encoding: str = payload.get("encoding", "utf-8")

    if not path:
        return {"bytes_written": 0, "error": "Kein Pfad angegeben"}
    if mode not in ("w", "a"):
        return {"bytes_written": 0, "error": f"Ungültiger Modus: {mode!r} (erlaubt: w, a)"}

    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, mode, encoding=encoding) as f:
            f.write(content)
        return {"bytes_written": len(content.encode(encoding)), "error": None}
    except PermissionError:
        return {"bytes_written": 0, "error": f"Keine Schreibberechtigung: {path}"}
    except Exception as exc:
        return {"bytes_written": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# Aktion: list_directory
# ---------------------------------------------------------------------------
async def list_directory(payload: dict[str, Any]) -> dict[str, Any]:
    """Listet den Inhalt eines Verzeichnisses auf."""
    path: str = os.path.expanduser(payload.get("path", "."))
    pattern: Optional[str] = payload.get("pattern")

    try:
        if not os.path.isdir(path):
            return {"entries": [], "error": f"Kein Verzeichnis: {path}"}

        entries = []
        for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            if pattern and not _fnmatch(entry.name, pattern):
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
                entries.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir(follow_symlinks=False)
                            else ("symlink" if entry.is_symlink() else "file"),
                    "size": stat.st_size if entry.is_file(follow_symlinks=False) else None,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
            except OSError:
                entries.append({"name": entry.name, "type": "unknown", "size": None, "modified": None})

        return {"entries": entries, "error": None}
    except PermissionError:
        return {"entries": [], "error": f"Keine Leseberechtigung: {path}"}
    except Exception as exc:
        return {"entries": [], "error": str(exc)}


def _fnmatch(name: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


# ---------------------------------------------------------------------------
# Aktion: get_system_info
# ---------------------------------------------------------------------------
async def get_system_info(payload: dict[str, Any]) -> dict[str, Any]:
    """Gibt CPU-, RAM-, Disk- und Plattforminformationen zurück."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_system_info_sync)


def _get_system_info_sync() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": platform.system(),
        "platform_version": platform.version(),
        "hostname": platform.node(),
        "python_version": sys.version,
        "error": None,
    }
    try:
        import psutil  # type: ignore
        info["cpu_percent"] = psutil.cpu_percent(interval=0.5)
        info["cpu_count"] = psutil.cpu_count()
        vm = psutil.virtual_memory()
        info["ram_total_mb"] = round(vm.total / 1024 / 1024)
        info["ram_used_mb"] = round(vm.used / 1024 / 1024)
        info["ram_percent"] = vm.percent
        info["disk_usage"] = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                info["disk_usage"].append({
                    "mountpoint": part.mountpoint,
                    "total_gb": round(usage.total / 1024**3, 1),
                    "used_gb": round(usage.used / 1024**3, 1),
                    "percent": usage.percent,
                })
            except (PermissionError, OSError):
                pass
    except ImportError:
        info["error"] = "psutil nicht installiert (pip install psutil)"
    except Exception as exc:
        info["error"] = str(exc)
    return info


# ---------------------------------------------------------------------------
# Aktion: screenshot
# ---------------------------------------------------------------------------
async def screenshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Erstellt einen Screenshot und gibt ihn als Base64-kodiertes PNG zurück."""
    fmt: str = payload.get("format", "png").lower()
    quality: int = int(payload.get("quality", 85))
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _screenshot_sync, fmt, quality)


def _screenshot_sync(fmt: str, quality: int) -> dict[str, Any]:
    try:
        import mss  # type: ignore
        import mss.tools

        with mss.mss() as sct:
            monitor = sct.monitors[0]  # Alle Monitore zusammen
            sct_img = sct.grab(monitor)

        # mss liefert BGRA – in PIL-kompatibles Format konvertieren
        try:
            from PIL import Image  # type: ignore
            img = Image.frombytes("RGBA", sct_img.size, sct_img.bgra, "raw", "BGRA")
            img = img.convert("RGB")
            buf = io.BytesIO()
            if fmt == "jpeg":
                img.save(buf, format="JPEG", quality=quality)
            else:
                img.save(buf, format="PNG")
            image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except ImportError:
            # Fallback: mss-eigenes PNG (ohne Pillow)
            buf = io.BytesIO()
            mss.tools.to_png(sct_img.rgb, sct_img.size, output=buf)
            image_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            fmt = "png"

        return {
            "image_base64": image_b64,
            "format": fmt,
            "width": sct_img.size[0],
            "height": sct_img.size[1],
            "error": None,
        }
    except ImportError:
        return {
            "image_base64": None,
            "format": None,
            "width": 0,
            "height": 0,
            "error": "mss nicht installiert (pip install mss)",
        }
    except Exception as exc:
        return {"image_base64": None, "format": None, "width": 0, "height": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# Aktion: get_processes
# ---------------------------------------------------------------------------
async def get_processes(payload: dict[str, Any]) -> dict[str, Any]:
    """Gibt eine Liste laufender Prozesse zurück."""
    filter_name: Optional[str] = payload.get("filter_name")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _get_processes_sync, filter_name)


def _get_processes_sync(filter_name: Optional[str]) -> dict[str, Any]:
    try:
        import psutil  # type: ignore
        processes = []
        for proc in psutil.process_iter(["pid", "name", "status", "cpu_percent", "memory_info"]):
            try:
                info = proc.info
                name = info.get("name") or ""
                if filter_name and filter_name.lower() not in name.lower():
                    continue
                mem_mb = round(info["memory_info"].rss / 1024 / 1024, 1) if info.get("memory_info") else None
                processes.append({
                    "pid": info["pid"],
                    "name": name,
                    "status": info.get("status"),
                    "cpu_percent": info.get("cpu_percent"),
                    "memory_mb": mem_mb,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        processes.sort(key=lambda p: p["name"].lower())
        return {"processes": processes, "error": None}
    except ImportError:
        return {"processes": [], "error": "psutil nicht installiert (pip install psutil)"}
    except Exception as exc:
        return {"processes": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# Nachrichten-Handler (mit Audit-Logging)
# ---------------------------------------------------------------------------
async def handle_message(
    raw: str,
    ws: websockets.WebSocketClientProtocol,
    app_whitelist: Optional[list[str]],
) -> None:
    """
    Verarbeitet eine eingehende Nachricht vom Server und sendet die Antwort zurück.
    Wird als eigenständiger Task gestartet, damit parallele Requests möglich sind.
    """
    try:
        msg: dict[str, Any] = json.loads(raw)
        request_id: str = msg["request_id"]
        action: str = msg["action"]
        payload: dict[str, Any] = msg.get("payload", {})
    except (json.JSONDecodeError, KeyError) as exc:
        log.error("Ungültige Nachricht empfangen: %s – %s", raw[:200], exc)
        return

    # Audit-Log
    if action == "run_code":
        lang = payload.get("language", "?")
        snippet = payload.get("code", "")[:200].replace("\n", "\\n")
        log.info("AUDIT | action=run_code | lang=%s | request_id=%s | code=%r", lang, request_id, snippet)
    else:
        log.info("AUDIT | action=%s | request_id=%s | payload=%s", action, request_id, str(payload)[:200])

    if action == "run_code":
        result = await run_code(payload)
    elif action == "open_app":
        result = await open_app(payload, app_whitelist)
    elif action == "read_file":
        result = await read_file(payload)
    elif action == "write_file":
        result = await write_file(payload)
    elif action == "list_directory":
        result = await list_directory(payload)
    elif action == "get_system_info":
        result = await get_system_info(payload)
    elif action == "screenshot":
        result = await screenshot(payload)
    elif action == "get_processes":
        result = await get_processes(payload)
    else:
        result = {"error": f"Unbekannte Aktion: {action!r}"}
        log.warning("Unbekannte Aktion empfangen: %s", action)

    try:
        await ws.send(json.dumps({"request_id": request_id, "result": result}))
    except websockets.exceptions.WebSocketException as exc:
        log.warning("Konnte Antwort nicht senden (Verbindung verloren?): %s", exc)
