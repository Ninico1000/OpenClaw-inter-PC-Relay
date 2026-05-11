"""
agent.py – Neo Relay Agent (Standalone)
Verbindet sich mit dem Relay-Server und führt Befehle aus.

Start:
    python agent.py

Umgebungsvariablen:
    RELAY_SERVER_URL       – Vollständige WebSocket-URL des Servers
                             (Standard: ws://localhost:8765/ws/agent)
    AGENT_ID               – Eindeutige ID dieses Agents (Standard: Hostname)
    OPENCLAW_AGENT_TOKEN   – Muss mit dem Server-Token übereinstimmen
    APP_WHITELIST          – Kommaseparierte App-Namen, die open_app erlaubt sind
                             (leer = keine Einschränkung)
"""

import asyncio
import logging
import os
import platform
from typing import Optional

import websockets
import websockets.exceptions

from agent_core import handle_message

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("agent")

if AGENT_TOKEN == _DEFAULT_TOKEN:
    log.warning("⚠  OPENCLAW_AGENT_TOKEN ist noch der Default-Wert – bitte ändern!")


# ---------------------------------------------------------------------------
# Reconnect-Loop mit Exponential Backoff
# ---------------------------------------------------------------------------
async def run() -> None:
    """
    Hauptschleife: verbindet sich mit dem Relay-Server und verarbeitet Nachrichten.
    Bei Verbindungsabbruch wird mit Exponential Backoff neu verbunden.
    """
    ws_url = f"{RELAY_SERVER_URL.rstrip('/')}?agent_id={AGENT_ID}&token={AGENT_TOKEN}"

    backoff: float = 2.0
    max_backoff: float = 60.0

    log.info("Neo Relay Agent gestartet")
    log.info("  Agent-ID  : %s", AGENT_ID)
    log.info("  Relay-URL : %s", RELAY_SERVER_URL)
    log.info("  Plattform : %s %s", platform.system(), platform.release())
    log.info("  Whitelist : %s", APP_WHITELIST or "keine (alle Apps erlaubt)")

    while True:
        log.info("Verbinde mit %s ...", RELAY_SERVER_URL)
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=30,
                ping_timeout=10,
                open_timeout=10,
            ) as ws:
                log.info("Verbunden! (Backoff zurückgesetzt)")
                backoff = 2.0

                sem = asyncio.Semaphore(10)

                async def process(raw: str) -> None:
                    async with sem:
                        await handle_message(raw, ws, APP_WHITELIST)

                async for message in ws:
                    asyncio.create_task(process(message))

        except websockets.exceptions.ConnectionClosed as exc:
            code = getattr(exc.rcvd, "code", None) if hasattr(exc, "rcvd") else None
            if code == 4001:
                log.error(
                    "Server hat die Verbindung wegen ungültigem Token abgewiesen "
                    "(Code 4001). Bitte OPENCLAW_AGENT_TOKEN prüfen! Warte 60s ..."
                )
                await asyncio.sleep(60.0)
                continue
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
