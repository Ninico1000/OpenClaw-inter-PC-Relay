"""
agent.py – Neo Relay Agent
Verbindet sich mit dem Relay-Server und führt Befehle aus.

Start:
    python agent.py

Umgebungsvariablen:
    RELAY_URL              – WebSocket-URL des Servers (Standard: ws://localhost:8765)
    AGENT_ID               – Eindeutige ID dieses Agents (Standard: Hostname)
    OPENCLAW_AGENT_TOKEN   – Muss mit dem Server-Token übereinstimmen
    APP_WHITELIST          – Kommaseparierte App-Namen, die open_app erlaubt sind
                             (leer = keine Einschränkung)
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
RELAY_URL: str = os.getenv("RELAY_URL", "ws://localhost:8765")
AGENT_ID: str = os.getenv("AGENT_ID", platform.node())
AGENT_TOKEN: str = os.getenv("OPENCLAW_AGENT_TOKEN", "dev-agent-token-change-me")
_WHITELIST_RAW: str = os.getenv("APP_WHITELIST", "")

# None = kein Filter, Liste = nur diese Apps erlaubt
APP_WHITELIST: Optional[list[str]] = (
    [a.strip() for a in _WHITELIST_RAW.split(",") if a.strip()]
    if _WHITELIST_RAW.strip()
    else None
)

_DEFAULT_TOKEN = "dev-agent-token-change-me"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("agent")

if AGENT_TOKEN == _DEFAULT_TOKEN:
    log.warning("⚠  OPENCLAW_AGENT_TOKEN ist noch der Default-Wert – bitte ändern!")

# Plattform einmalig ermitteln
_SYSTEM: str = platform.system().lower()  # "windows" | "darwin" | "linux"


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

    # Interpreter-Kommando je nach Sprache und Plattform bestimmen
    if language == "python":
        cmd = [sys.executable, "-c", code]

    elif language == "bash":
        if _SYSTEM == "windows":
            # Erfordert Git Bash oder WSL im PATH
            cmd = ["bash", "-c", code]
        else:
            cmd = ["/bin/bash", "-c", code]

    elif language == "shell":
        if _SYSTEM == "windows":
            cmd = ["cmd", "/c", code]
        else:
            cmd = ["/bin/sh", "-c", code]

    elif language == "powershell":
        # Funktioniert auf Windows nativ, auf Linux/macOS mit pwsh
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
        return {
            "stdout": "",
            "stderr": str(exc),
            "exit_code": -1,
            "error": "exception",
        }


def _open_app_sync(app_name: str, args: list[str]) -> dict[str, Any]:
    """
    Startet eine Anwendung plattformspezifisch im Hintergrund (non-blocking).
    Wird im Thread-Pool ausgeführt, da subprocess.Popen synchron ist.
    """
    if APP_WHITELIST is not None and app_name not in APP_WHITELIST:
        return {
            "pid": None,
            "error": f"App '{app_name}' steht nicht auf der Whitelist",
        }

    try:
        if _SYSTEM == "windows":
            # DETACHED_PROCESS: Prozess läuft unabhängig vom Agent-Prozess weiter
            proc = subprocess.Popen(
                [app_name] + args,
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )

        elif _SYSTEM == "darwin":
            # Versuche macOS-nativen 'open -a AppName', falle auf direkten Aufruf zurück
            try:
                proc = subprocess.Popen(
                    ["open", "-a", app_name] + args,
                    start_new_session=True,
                )
            except FileNotFoundError:
                proc = subprocess.Popen(
                    [app_name] + args,
                    start_new_session=True,
                )

        else:  # Linux und andere POSIX-Systeme
            proc = subprocess.Popen(
                [app_name] + args,
                start_new_session=True,
                close_fds=True,
            )

        return {"pid": proc.pid, "error": None}

    except FileNotFoundError:
        return {"pid": None, "error": f"App '{app_name}' nicht gefunden"}
    except Exception as exc:
        return {"pid": None, "error": str(exc)}


async def open_app(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrapper: führt _open_app_sync im Thread-Pool aus, damit der Event-Loop frei bleibt."""
    app_name: str = payload["app"]
    args: list[str] = payload.get("args", [])
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _open_app_sync, app_name, args)


# ---------------------------------------------------------------------------
# Nachrichten-Dispatcher
# ---------------------------------------------------------------------------
async def handle_message(
    raw: str, ws: websockets.WebSocketClientProtocol
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

    log.info("Aktion '%s' (request_id=%s)", action, request_id)

    if action == "run_code":
        result = await run_code(payload)
    elif action == "open_app":
        result = await open_app(payload)
    else:
        result = {"error": f"Unbekannte Aktion: {action!r}"}

    try:
        await ws.send(json.dumps({"request_id": request_id, "result": result}))
    except websockets.exceptions.WebSocketException as exc:
        log.warning("Konnte Antwort nicht senden (Verbindung verloren?): %s", exc)


# ---------------------------------------------------------------------------
# Reconnect-Loop mit Exponential Backoff
# ---------------------------------------------------------------------------
async def run() -> None:
    """
    Hauptschleife: verbindet sich mit dem Relay-Server und verarbeitet Nachrichten.
    Bei Verbindungsabbruch wird mit Exponential Backoff neu verbunden.
    """
    ws_url = f"{RELAY_URL}/ws/agent?agent_id={AGENT_ID}&token={AGENT_TOKEN}"

    backoff: float = 2.0       # Startverzögerung in Sekunden
    max_backoff: float = 60.0  # Maximale Wartezeit

    log.info("Neo Relay Agent gestartet")
    log.info("  Agent-ID  : %s", AGENT_ID)
    log.info("  Relay-URL : %s", RELAY_URL)
    log.info("  Plattform : %s %s", platform.system(), platform.release())
    log.info("  Whitelist : %s", APP_WHITELIST or "keine (alle Apps erlaubt)")

    while True:
        log.info("Verbinde mit %s ...", RELAY_URL)
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=30,   # Keepalive alle 30s
                ping_timeout=10,    # Verbindung tot, wenn kein Pong in 10s
                open_timeout=10,    # Verbindungsaufbau-Timeout
            ) as ws:
                log.info("Verbunden! (Backoff zurückgesetzt)")
                backoff = 2.0  # Nach erfolgreicher Verbindung zurücksetzen

                # Semaphore: max. 10 parallel laufende Tasks pro Verbindung
                sem = asyncio.Semaphore(10)

                async def process(raw: str) -> None:
                    async with sem:
                        await handle_message(raw, ws)

                async for message in ws:
                    # Jeden eingehenden Request als eigenen Task starten
                    asyncio.create_task(process(message))

        except websockets.exceptions.ConnectionClosed as exc:
            # Prüfe auf Auth-Fehler (Code 4001) – dann länger warten
            code = getattr(exc.rcvd, "code", None) if hasattr(exc, "rcvd") else None
            if code == 4001:
                log.error(
                    "Server hat die Verbindung wegen ungültigem Token abgewiesen "
                    "(Code 4001). Bitte OPENCLAW_AGENT_TOKEN prüfen! Warte 60s ..."
                )
                await asyncio.sleep(60.0)
                continue  # Kein doppeltes sleep am Ende
            log.warning(
                "Verbindung getrennt (Code %s). Reconnect in %.0fs ...", code, backoff
            )

        except websockets.exceptions.WebSocketException as exc:
            log.warning("WebSocket-Fehler: %s. Reconnect in %.0fs ...", exc, backoff)

        except OSError as exc:
            log.warning(
                "Netzwerk-Fehler (Server erreichbar?): %s. Reconnect in %.0fs ...",
                exc,
                backoff,
            )

        except Exception as exc:
            log.exception(
                "Unerwarteter Fehler: %s. Reconnect in %.0fs ...", exc, backoff
            )

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Agent durch Benutzer beendet.")
