# OpenClaw Inter-PC Relay

Verteiltes System zur Befehlsweiterleitung und Sprachsteuerung über mehrere PCs hinweg.

```
OpenClaw/Neo  ──HTTP──►  Relay-Server  ──WebSocket──►  Agent(s)
                                                          └─ Code ausführen
                                                          └─ Apps starten
Voice-Client  ──────────────────────────────────────────►  (Mikrofon → TTS)
```

Agents verbinden sich aktiv zum Server – kein Port-Forwarding auf Ziel-PCs nötig.

---

## Schnellstart

### Relay-Server

```bash
pip install fastapi uvicorn pydantic
OPENCLAW_SERVER_TOKEN=mein-server-token \
OPENCLAW_AGENT_TOKEN=mein-agent-token \
python relay_server.py
```

### Standalone-Agent (auf dem Ziel-PC)

```bash
pip install websockets
RELAY_SERVER_URL=ws://server-ip:8765/ws/agent \
OPENCLAW_AGENT_TOKEN=mein-agent-token \
python agent.py
```

### Voice-Client (mit Sprachassistent + Agent)

```bash
cd client
cp .env.example .env     # .env anpassen
python setup.py          # Abhängigkeiten + Modelle installieren
python main.py
```

---

## Umgebungsvariablen

### Relay-Server (`relay_server.py`)

| Variable | Standard | Beschreibung |
|---|---|---|
| `OPENCLAW_SERVER_TOKEN` | `dev-server-token-change-me` | Bearer-Token für HTTP-Endpoints |
| `OPENCLAW_AGENT_TOKEN` | `dev-agent-token-change-me` | Token beim WS-Handshake |
| `RELAY_HOST` | `0.0.0.0` | Bind-Adresse |
| `RELAY_PORT` | `8765` | Port |

### Agent (`agent.py` / `client/agent.py`)

| Variable | Standard | Beschreibung |
|---|---|---|
| `RELAY_SERVER_URL` | `ws://localhost:8765/ws/agent` | WebSocket-URL des Servers |
| `AGENT_ID` | Hostname | Eindeutige Agent-ID |
| `OPENCLAW_AGENT_TOKEN` | `dev-agent-token-change-me` | Muss mit Server-Token übereinstimmen |
| `APP_WHITELIST` | *(leer)* | Kommaseparierte erlaubte App-Namen |

### Voice-Client (`.env`)

| Variable | Standard | Beschreibung |
|---|---|---|
| `OPENCLAW_URL` | `http://localhost:18789` | Neo/OpenClaw API |
| `OPENCLAW_TOKEN` | – | Bearer-Token für Neo |
| `WAKE_WORD_MODEL` | `hey_neo` | openwakeword-Modell oder Pfad zur .onnx |
| `WHISPER_MODEL` | `base` | faster-whisper Modell |
| `WHISPER_LANGUAGE` | `de` | Transkriptions-Sprache |
| `PIPER_VOICE` | – | Pfad zur Piper .onnx-Stimme |
| `MIC_DEVICE` | *(Standard)* | sounddevice-Index oder -Name |
| `ENABLE_AGENT` | `true` | Agent-Loop aktivieren |
| `ENABLE_VOICE` | `true` | Voice-Loop aktivieren |

---

## API-Endpunkte

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/health` | Liveness-Check (kein Auth) |
| `GET` | `/agents` | Verbundene Agents auflisten |
| `POST` | `/agents/{id}/run_code` | Code auf Agent ausführen |
| `POST` | `/agents/{id}/open_app` | App auf Agent starten |
| `WS` | `/ws/agent` | Agent-Verbindungs-Endpoint |

Interaktive Docs: `http://server:8765/docs`

---

## Sicherheitshinweise

- **Tokens ändern**: Standard-Tokens niemals in Produktion verwenden.
- **`.env` nicht einchecken**: Liegt in `.gitignore`; enthält Secrets.
- **TLS**: Für Produktionsumgebungen `wss://` und HTTPS verwenden.
- **APP_WHITELIST**: Auf Agents setzen, um unbeabsichtigte App-Starts zu verhindern.
- **Netzwerk**: Server nach Möglichkeit nicht öffentlich exponieren.
