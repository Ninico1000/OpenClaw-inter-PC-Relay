"""
relay_server.py – Neo Relay Server
Vermittelt Befehle von OpenClaw/Neo (HTTP) an verbundene Agents (WebSocket).

Start:
    python relay_server.py
    # oder
    uvicorn relay_server:app --host 0.0.0.0 --port 8765

Umgebungsvariablen:
    OPENCLAW_SERVER_TOKEN  – Bearer-Token für HTTP-Endpoints (Neo → Server)
    OPENCLAW_AGENT_TOKEN   – Token beim WS-Handshake (Agent → Server)
    RELAY_HOST             – Bind-Adresse (Standard: 0.0.0.0)
    RELAY_PORT             – Port (Standard: 8765)
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
SERVER_TOKEN: str = os.getenv("OPENCLAW_SERVER_TOKEN", "dev-server-token-change-me")
AGENT_TOKEN: str = os.getenv("OPENCLAW_AGENT_TOKEN", "dev-agent-token-change-me")
HOST: str = os.getenv("RELAY_HOST", "0.0.0.0")
PORT: int = int(os.getenv("RELAY_PORT", "8765"))

_DEFAULT_SERVER_TOKEN = "dev-server-token-change-me"
_DEFAULT_AGENT_TOKEN = "dev-agent-token-change-me"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("relay")

# Sicherheits-Warnung bei Default-Tokens
if SERVER_TOKEN == _DEFAULT_SERVER_TOKEN:
    log.warning("⚠  OPENCLAW_SERVER_TOKEN ist noch der Default-Wert – bitte ändern!")
if AGENT_TOKEN == _DEFAULT_AGENT_TOKEN:
    log.warning("⚠  OPENCLAW_AGENT_TOKEN ist noch der Default-Wert – bitte ändern!")


# ---------------------------------------------------------------------------
# Interner Zustand
# ---------------------------------------------------------------------------
class AgentState:
    """Hält den Zustand einer aktiven Agent-Verbindung."""

    def __init__(self, ws: WebSocket, agent_id: str) -> None:
        self.ws = ws
        self.agent_id = agent_id
        self.connected_at: str = datetime.now(timezone.utc).isoformat()
        # Offene Request-Futures, keyed by request_id
        self.pending: dict[str, asyncio.Future[Any]] = {}
        # Lock verhindert gleichzeitige Sends auf denselben WS
        self._send_lock = asyncio.Lock()

    async def send(self, message: dict[str, Any]) -> None:
        """Serialisiert und sendet eine Nachricht an den Agent (thread-safe)."""
        async with self._send_lock:
            await self.ws.send_text(json.dumps(message))


# Globale Registry: agent_id → AgentState
_agents: dict[str, AgentState] = {}


# ---------------------------------------------------------------------------
# FastAPI-App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Neo Relay Server",
    description=(
        "Verbindet OpenClaw/Neo (HTTP-Clients) mit Agents (WebSocket-Clients). "
        "Agents verbinden sich aktiv zum Server – kein Port-Forwarding auf den Ziel-PCs nötig."
    ),
    version="1.0.0",
)

_security = HTTPBearer(auto_error=True)


def require_server_token(
    creds: HTTPAuthorizationCredentials = Depends(_security),
) -> str:
    """Dependency: prüft den Bearer-Token für HTTP-Endpoints."""
    if creds.credentials != SERVER_TOKEN:
        raise HTTPException(status_code=401, detail="Ungültiger Server-Token")
    return creds.credentials


# ---------------------------------------------------------------------------
# Request-/Response-Modelle
# ---------------------------------------------------------------------------
class RunCodeRequest(BaseModel):
    """Anfrage zum Ausführen von Code auf einem Agent."""

    language: str  # python | bash | shell | powershell
    code: str
    timeout: int = 30  # Sekunden
    cwd: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "language": "python",
                "code": "import platform; print(platform.node())",
                "timeout": 10,
                "cwd": None,
            }
        }
    }


class OpenAppRequest(BaseModel):
    """Anfrage zum Starten einer Anwendung auf einem Agent."""

    app: str
    args: list[str] = []

    model_config = {
        "json_schema_extra": {
            "example": {"app": "notepad.exe", "args": []}
        }
    }


# ---------------------------------------------------------------------------
# Interne Dispatch-Hilfsfunktion
# ---------------------------------------------------------------------------
async def _dispatch(
    agent_id: str,
    action: str,
    payload: dict[str, Any],
    timeout: float,
) -> Any:
    """
    Sendet einen Request an den Agent und wartet auf die Antwort.
    Wirft HTTP-Exceptions bei unbekanntem Agent oder Timeout.
    """
    state = _agents.get(agent_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_id}' ist nicht verbunden",
        )

    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    state.pending[request_id] = future

    try:
        await state.send(
            {"request_id": request_id, "action": action, "payload": payload}
        )
        return await asyncio.wait_for(future, timeout=timeout)

    except asyncio.TimeoutError:
        state.pending.pop(request_id, None)
        raise HTTPException(
            status_code=504,
            detail=f"Agent '{agent_id}' hat nicht innerhalb von {timeout:.0f}s geantwortet",
        )
    except ConnectionError as exc:
        state.pending.pop(request_id, None)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# HTTP-Endpoints
# ---------------------------------------------------------------------------
@app.get("/health", summary="Liveness-Check (kein Auth nötig)")
async def health() -> dict[str, Any]:
    """Gibt den Server-Status und die Anzahl verbundener Agents zurück."""
    return {"status": "ok", "agents_connected": len(_agents)}


@app.get(
    "/agents",
    summary="Liste aller verbundenen Agents",
    dependencies=[Depends(require_server_token)],
)
async def list_agents() -> dict[str, Any]:
    """Gibt agent_id und Verbindungszeitpunkt aller aktiven Agents zurück."""
    return {
        "agents": [
            {"agent_id": aid, "connected_at": s.connected_at}
            for aid, s in _agents.items()
        ]
    }


@app.post(
    "/agents/{agent_id}/run_code",
    summary="Code auf einem Agent ausführen",
    dependencies=[Depends(require_server_token)],
)
async def run_code(agent_id: str, req: RunCodeRequest) -> Any:
    """
    Führt Code in der angegebenen Sprache auf dem Ziel-Agent aus.
    Gibt stdout, stderr und exit_code zurück.
    """
    return await _dispatch(
        agent_id,
        "run_code",
        req.model_dump(),
        timeout=float(req.timeout) + 10,  # etwas Puffer über den Code-Timeout
    )


@app.post(
    "/agents/{agent_id}/open_app",
    summary="Anwendung auf einem Agent starten",
    dependencies=[Depends(require_server_token)],
)
async def open_app(agent_id: str, req: OpenAppRequest) -> Any:
    """
    Startet eine Anwendung im Hintergrund auf dem Ziel-Agent.
    Gibt die PID des gestarteten Prozesses zurück.
    """
    return await _dispatch(agent_id, "open_app", req.model_dump(), timeout=15.0)


# ---------------------------------------------------------------------------
# WebSocket-Endpoint für Agents
# ---------------------------------------------------------------------------
@app.websocket("/ws/agent")
async def agent_ws(
    ws: WebSocket,
    agent_id: str = Query(..., description="Eindeutige Agent-ID (z. B. Hostname)"),
    token: str = Query(..., description="OPENCLAW_AGENT_TOKEN"),
) -> None:
    """
    WebSocket-Endpoint, zu dem sich Agents verbinden.
    Token wird nach dem Accept geprüft – bei Fehler sofortiger Close mit Code 4001.
    """
    await ws.accept()

    # Token-Prüfung nach Accept (WS-Standard erlaubt keine HTTP-4xx vor dem Upgrade)
    if token != AGENT_TOKEN:
        log.warning("Abgewiesener Agent '%s': ungültiger Token", agent_id)
        await ws.close(code=4001, reason="Ungültiger Agent-Token")
        return

    # Falls gleiche agent_id bereits verbunden: alte Session aufräumen
    if existing := _agents.pop(agent_id, None):
        log.info("Agent '%s' hat sich neu verbunden – alte Session wird verworfen", agent_id)
        for fut in existing.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Agent hat sich neu verbunden"))

    state = AgentState(ws, agent_id)
    _agents[agent_id] = state
    log.info("Agent verbunden: %s", agent_id)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Agent '%s' hat ungültiges JSON gesendet", agent_id)
                continue

            request_id = msg.get("request_id")
            result = msg.get("result")

            if request_id and request_id in state.pending:
                fut = state.pending.pop(request_id)
                if not fut.done():
                    fut.set_result(result)
            else:
                log.debug(
                    "Unbekannte request_id von Agent '%s': %s", agent_id, request_id
                )

    except WebSocketDisconnect:
        log.info("Agent getrennt: %s", agent_id)
    except Exception as exc:
        log.error("Fehler bei Agent '%s': %s", agent_id, exc)
    finally:
        _agents.pop(agent_id, None)
        # Alle wartenden Futures mit Fehler beenden
        for fut in state.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(f"Agent '{agent_id}' getrennt"))


# ---------------------------------------------------------------------------
# Direktstart
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
