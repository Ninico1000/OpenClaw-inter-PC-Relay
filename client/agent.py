"""
client/agent.py – Loop A: WebSocket Agent

Verbindet sich mit dem Relay-Server, empfängt Befehle und führt sie aus.
Reconnect mit exponentiellem Backoff (max. 5 Minuten).

Unterstützte Aktionen:
    run_code   – Code in python/bash/shell/powershell ausführen
    open_app   – Anwendung plattformspezifisch starten

Konfiguration via Umgebungsvariablen (aus .env):
    RELAY_SERVER_URL   – WebSocket-URL des Servers
    AGENT_ID           – Eindeutige ID dieses Clients (Standard: Hostname)
    AGENT_TOKEN        – Muss mit OPENCLAW_AGENT_TOKEN des Servers übereinstimmen
    APP_WHITELIST      – Kommaseparierte App-Namen (leer = alle erlaubt)
"""

import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
from typing import Any, Optional

import websockets
import websockets.exceptions

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
RELAY_SERVER_URL: str = os.getenv("RELAY_SERVER_URL", "ws://localhost:8765/ws/agent")
AGENT_ID: str = os.getenv("AGENT_ID", platform.node())
AGENT_TOKEN: str = os.getenv("AGENT_TOKEN", "change-me")
_WHITELIST_RAW: str = os.getenv("APP_WHITELIST", "")

APP_WHITELIST: Optional[list[str]] = (
    [a.strip() for a in _WHITELIST_RAW.split(",") if a.strip()]
    if _WHITELIST_RAW.strip()
    else None
)

_DEFAULT_TOKEN = "change-me"
_SYSTEM: str = platform.system().lower()

# Maximaler Backoff: 5 Minuten
_MAX_BACKOFF: float = 300.0

log = logging.getLogger("agent")

if AGENT_TOKEN == _DEFAULT_TOKEN:
    log.warning("⚠  AGENT_TOKEN ist noch der Default-Wert – bitte in .env ändern!")


# ---------------------------------------------------------------------------
# Aktionen
# ---------------------------------------------------------------------------
async def _run_code(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Führt Code in der angegebenen Sprache aus.

    Args:
        payload: Dict mit keys language, code, timeout (opt.), cwd (opt.)

    Returns:
        Dict mit stdout, stderr, exit_code, error
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


def _open_app_sync(app_name: str, args: list[str]) -> dict[str, Any]:
    """
    Startet eine Anwendung plattformspezifisch im Hintergrund.
    Synchron – wird im Thread-Pool aufgerufen.

    Args:
        app_name: Name oder Pfad der Anwendung
        args:     Zusätzliche Argumente

    Returns:
        Dict mit pid (int|None) und error (str|None)
    """
    if APP_WHITELIST is not None and app_name not in APP_WHITELIST:
        return {
            "pid": None,
            "error": f"App '{app_name}' steht nicht auf der Whitelist",
        }

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


async def _open_app(payload: dict[str, Any]) -> dict[str, Any]:
    """Async-Wrapper: führt _open_app_sync im Thread-Pool aus."""
    app_name: str = payload["app"]
    args: list[str] = payload.get("args", [])
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _open_app_sync, app_name, args)


# ---------------------------------------------------------------------------
# Nachrichten-Handler
# ---------------------------------------------------------------------------
async def _handle_message(raw: str, ws: websockets.WebSocketClientProtocol) -> None:
    """
    Verarbeitet eine eingehende Nachricht vom Server.

    Erwartet JSON: {request_id, action, payload}
    Antwortet mit: {request_id, result}

    Args:
        raw: JSON-String vom Server
        ws:  Aktive WebSocket-Verbindung für die Antwort
    """
    try:
        msg: dict[str, Any] = json.loads(raw)
        request_id: str = msg["request_id"]
        action: str = msg["action"]
        payload: dict[str, Any] = msg.get("payload", {})
    except (json.JSONDecodeError, KeyError) as exc:
        log.error("Ungültige Nachricht: %s – %s", raw[:200], exc)
        return

    log.info("Aktion '%s' empfangen (request_id=%s)", action, request_id)

    if action == "run_code":
        result = await _run_code(payload)
    elif action == "open_app":
        result = await _open_app(payload)
    else:
        result = {"error": f"Unbekannte Aktion: {action!r}"}
        log.warning("Unbekannte Aktion: %s", action)

    try:
        await ws.send(json.dumps({"request_id": request_id, "result": result}))
        log.debug("Antwort gesendet für request_id=%s", request_id)
    except websockets.exceptions.WebSocketException as exc:
        log.warning("Antwort konnte nicht gesendet werden: %s", exc)


# ---------------------------------------------------------------------------
# Reconnect-Loop
# ---------------------------------------------------------------------------
async def run() -> None:
    """
    Haupt-Loop: verbindet zum Relay-Server und verarbeitet Nachrichten.
    Bei Verbindungsabbruch: Reconnect mit exponentiellem Backoff (max. 5 min).
    """
    # RELAY_SERVER_URL kann ws://.../ws/agent enthalten oder ohne Pfad sein
    base_url = RELAY_SERVER_URL.rstrip("/")
    if "/ws/agent" not in base_url:
        base_url = base_url + "/ws/agent"
    ws_url = f"{base_url}?agent_id={AGENT_ID}&token={AGENT_TOKEN}"

    backoff: float = 2.0

    log.info("Neo Relay Agent (client) gestartet")
    log.info("  Agent-ID  : %s", AGENT_ID)
    log.info("  Server    : %s", RELAY_SERVER_URL)
    log.info("  Plattform : %s %s", platform.system(), platform.release())
    log.info("  Whitelist : %s", APP_WHITELIST or "keine Einschränkung")

    while True:
        log.info("Verbinde mit Relay-Server …")
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
                open_timeout=10,
            ) as ws:
                log.info("Verbunden mit Relay-Server! (Backoff zurückgesetzt)")
                backoff = 2.0

                # Max. 10 parallele Request-Tasks
                sem = asyncio.Semaphore(10)

                async def _process(raw: str) -> None:
                    async with sem:
                        await _handle_message(raw, ws)

                async for message in ws:
                    asyncio.create_task(_process(str(message)))

        except websockets.exceptions.ConnectionClosed as exc:
            code = getattr(exc.rcvd, "code", None) if hasattr(exc, "rcvd") else None
            if code == 4001:
                log.error(
                    "Server hat Verbindung abgewiesen (Code 4001 – ungültiger Token). "
                    "Bitte AGENT_TOKEN in .env prüfen! Warte 60s …"
                )
                await asyncio.sleep(60.0)
                continue
            log.warning("Verbindung getrennt (Code %s). Reconnect in %.0fs …", code, backoff)

        except websockets.exceptions.WebSocketException as exc:
            log.warning("WebSocket-Fehler: %s. Reconnect in %.0fs …", exc, backoff)

        except OSError as exc:
            log.warning("Netzwerk-Fehler (Server erreichbar?): %s. Reconnect in %.0fs …", exc, backoff)

        except asyncio.CancelledError:
            log.info("Agent-Loop abgebrochen.")
            return

        except Exception as exc:
            log.exception("Unerwarteter Fehler: %s. Reconnect in %.0fs …", exc, backoff)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF)
