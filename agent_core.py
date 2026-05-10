"""
agent_core.py – Gemeinsame Agent-Logik

Enthält die Aktionen (run_code, open_app) und den Nachrichten-Handler,
die von agent.py (Root) und client/agent.py geteilt werden.
"""

import asyncio
import json
import logging
import platform
import subprocess
import sys
from typing import Any, Optional

import websockets
import websockets.exceptions

_SYSTEM: str = platform.system().lower()
log = logging.getLogger("agent")


# ---------------------------------------------------------------------------
# Aktionen
# ---------------------------------------------------------------------------
async def run_code(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Führt Code in der angegebenen Sprache aus und gibt stdout/stderr/exit_code zurück.
    Unterstützte Sprachen: python, bash, shell, powershell
    """
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


def open_app_sync(
    app_name: str,
    args: list[str],
    app_whitelist: Optional[list[str]],
) -> dict[str, Any]:
    """
    Startet eine Anwendung plattformspezifisch im Hintergrund (non-blocking).
    Wird im Thread-Pool ausgeführt, damit der Event-Loop frei bleibt.
    """
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

    # Audit-Log: jede ausgeführte Aktion nachvollziehbar erfassen
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
    else:
        result = {"error": f"Unbekannte Aktion: {action!r}"}
        log.warning("Unbekannte Aktion empfangen: %s", action)

    try:
        await ws.send(json.dumps({"request_id": request_id, "result": result}))
    except websockets.exceptions.WebSocketException as exc:
        log.warning("Konnte Antwort nicht senden (Verbindung verloren?): %s", exc)
