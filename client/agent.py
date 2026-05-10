"""
client/agent.py – Loop A: WebSocket Agent

Verbindet sich mit dem Relay-Server, empfängt Befehle und führt sie aus.
Reconnect mit exponentiellem Backoff (max. 5 Minuten).

Unterstützte Aktionen:
    run_code   – Code in python/bash/shell/powershell ausführen
    open_app   – Anwendung plattformspezifisch starten

Konfiguration via Umgebungsvariablen (aus .env):
    RELAY_SERVER_URL       – Vollständige WebSocket-URL des Servers
    AGENT_ID               – Eindeutige ID dieses Clients (Standard: Hostname)
    OPENCLAW_AGENT_TOKEN   – Muss mit OPENCLAW_AGENT_TOKEN des Servers übereinstimmen
    APP_WHITELIST          – Kommaseparierte App-Namen (leer = alle erlaubt)
"""

import asyncio
import logging
import os
import platform
import sys
from typing import Optional

import websockets
import websockets.exceptions

# agent_core liegt im übergeordneten Verzeichnis; Pfad einmalig ergänzen
import os as _os
import sys as _sys
_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _root not in _sys.path:
    _sys.path.insert(0, _root)

from agent_core import handle_message  # noqa: E402

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
RELAY_SERVER_URL: str = os.getenv("RELAY_SERVER_URL", "ws://localhost:8765/ws/agent")
AGENT_ID: str = os.getenv("AGENT_ID", platform.node())
AGENT_TOKEN: str = os.getenv("OPENCLAW_AGENT_TOKEN", "dev-agent-token-change-me")
_WHITELIST_RAW: str = os.getenv("APP_WHITELIST", "")

APP_WHITELIST: Optional[list[str]] = (
    [a.strip() for a in _WHITELIST_RAW.split(",") if a.strip()]
    if _WHITELIST_RAW.strip()
    else None
)

_DEFAULT_TOKEN = "dev-agent-token-change-me"
_MAX_BACKOFF: float = 300.0  # max. 5 Minuten

log = logging.getLogger("agent")

if AGENT_TOKEN == _DEFAULT_TOKEN:
    log.warning("⚠  OPENCLAW_AGENT_TOKEN ist noch der Default-Wert – bitte in .env ändern!")


# ---------------------------------------------------------------------------
# Reconnect-Loop
# ---------------------------------------------------------------------------
async def run() -> None:
    """
    Haupt-Loop: verbindet zum Relay-Server und verarbeitet Nachrichten.
    Bei Verbindungsabbruch: Reconnect mit exponentiellem Backoff (max. 5 min).
    """
    ws_url = f"{RELAY_SERVER_URL.rstrip('/')}?agent_id={AGENT_ID}&token={AGENT_TOKEN}"

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

                sem = asyncio.Semaphore(10)

                async def _process(raw: str) -> None:
                    async with sem:
                        await handle_message(raw, ws, APP_WHITELIST)

                async for message in ws:
                    asyncio.create_task(_process(str(message)))

        except websockets.exceptions.ConnectionClosed as exc:
            code = getattr(exc.rcvd, "code", None) if hasattr(exc, "rcvd") else None
            if code == 4001:
                log.error(
                    "Server hat Verbindung abgewiesen (Code 4001 – ungültiger Token). "
                    "Bitte OPENCLAW_AGENT_TOKEN in .env prüfen! Warte 60s …"
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
