# Neo Relay – OpenClaw Remote-Agent + Voice-System

Zwei-Komponenten-Architektur:

```
OpenClaw / Neo
    │  HTTP (Bearer Token)
    ▼
relay_server.py          ← Heimserver / VPS (Linux)
    │  WebSocket
    ├──────────────────► client/ (PC 1: Simon-Desktop)
    │                       ├── Agent-Loop   (Befehle empfangen)
    │                       └── Voice-Loop   (Mikro → Whisper → Neo → TTS)
    └──────────────────► client/ (PC 2, 3 …)
```

**Relay-Server** läuft dauerhaft auf dem Server.  
**Client** läuft auf jedem Windows/Linux/macOS-PC:  
- *Agent-Loop*: empfängt Befehle von Neo (Code ausführen, Apps starten)  
- *Voice-Loop*: Wake-Word → Aufnahme → Transkription → Neo-API → TTS-Ausgabe

Agents verbinden sich aktiv zum Server – **kein Port-Forwarding nötig**.

---

## Was läuft wo

| Komponente | Datei | Läuft auf |
|---|---|---|
| Relay-Server | `relay_server.py` | Heimserver / VPS |
| Client Entry-Point | `client/main.py` | Ziel-PC |
| Agent-Loop (WS) | `client/agent.py` | Ziel-PC |
| Voice-Loop | `client/voice.py` | Ziel-PC |

---

## Auto-Setup (Client)

Statt manuell alles zu installieren, geht's automatisch:

### Windows / Linux / macOS

```bash
cd client
python setup.py          # Vollständige Installation
# oder
python setup.py --check   # Nur prüfen was fehlt
```

Der Installer:
- Prüft Python-Version (>= 3.10, != 3.12)
- Installiert alle Dependencies via pip
- Prüft FFmpeg (oder zeigt wie man's installiert)
- Richtet Piper TTS ein (optional)
- Lädt Wake-Word + Whisper Modelle vor
- Erstellt .env aus .env.example
- Erstellt Start-Script (start.bat / start.sh)

### Was noch fehlt (manuell):

1. **.env bearbeiten** – Tokens eintragen
2. **FFmpeg** (Windows) – von https://ffmpeg.org/download.html
3. **Piper Voice** (optional) – deutsches ONNX-Modell

---

## Server-Setup (Linux)

### Voraussetzungen

```bash
python3 --version  # 3.10+
pip install fastapi uvicorn websockets
```

### Starten

```bash
export OPENCLAW_SERVER_TOKEN="langes-zufaelliges-passwort"
export OPENCLAW_AGENT_TOKEN="anderes-langes-passwort"
python relay_server.py
# API-Doku: http://server:8765/docs
```

### systemd-Unit (Autostart)

Datei: `/etc/systemd/system/neo-relay-server.service`

```ini
[Unit]
Description=Neo Relay Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=neo-relay
WorkingDirectory=/opt/neo-relay
ExecStart=/usr/bin/python3 /opt/neo-relay/relay_server.py
Restart=always
RestartSec=5
EnvironmentFile=/etc/neo-relay/server.env
NoNewPrivileges=true
ProtectSystem=strict

[Install]
WantedBy=multi-user.target
```

`/etc/neo-relay/server.env` (Rechte: `chmod 600`):

```
OPENCLAW_SERVER_TOKEN=langes-zufaelliges-passwort
OPENCLAW_AGENT_TOKEN=anderes-langes-passwort
RELAY_PORT=8765
RELAY_HOST=0.0.0.0
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now neo-relay-server
sudo journalctl -u neo-relay-server -f
```

---

## Client-Installation Windows (Schritt für Schritt)

### 1. Python 3.11 installieren

1. [python.org/downloads](https://www.python.org/downloads/) → Python 3.11.x herunterladen
2. Installer starten  
3. **Wichtig:** Haken bei **"Add Python to PATH"** setzen
4. "Install Now" klicken
5. Test in CMD: `python --version`

### 2. VC++ Redistributable (falls Compiler-Fehler)

Manche Pakete (webrtcvad) benötigen Visual C++ Runtime:  
[aka.ms/vs/17/release/vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe)

### 3. Client-Dateien einrichten

```cmd
REM Verzeichnis anlegen
mkdir C:\neo-relay
REM Dateien aus dem client/-Ordner nach C:\neo-relay kopieren
REM (main.py, agent.py, voice.py, requirements.txt, .env.example, start.bat)
```

### 4. Abhängigkeiten installieren

```cmd
cd C:\neo-relay
pip install -r requirements.txt
```

Bei torch-Installationsproblemen:

```cmd
REM CPU-only-Version (kleiner, reicht für Voice)
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
```

Bei webrtcvad-Kompilierungsproblemen:

```cmd
pip install webrtcvad-wheels
```

### 5. Piper TTS-Stimme herunterladen

Piper ist ein lokaler TTS-Synthesizer. Modelle: [rhasspy.github.io/piper-samples](https://rhasspy.github.io/piper-samples/)

```cmd
REM Piper-Binary: https://github.com/rhasspy/piper/releases
REM Entpacken nach C:\piper\

REM Deutsches Stimm-Modell herunterladen (Beispiel: thorsten-medium)
REM Direkt-Link:
curl -L -o C:\piper\de_DE-thorsten-medium.onnx ^
  https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx
curl -L -o C:\piper\de_DE-thorsten-medium.onnx.json ^
  https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/medium/de_DE-thorsten-medium.onnx.json

REM Piper zum PATH hinzufügen (einmalig)
setx PATH "%PATH%;C:\piper"
```

Weitere empfohlene Stimmen:
- `de_DE-kerstin-low` (weiblich, klein)
- `de_DE-ramona-low` (weiblich)
- `de_DE-eva_k-x_low` (weiblich, sehr klein)

### 6. Wake-Word-Modell einrichten

```cmd
REM hey_jarvis ist vorinstalliert (Fallback, immer verfügbar)

REM Für Custom "hey_neo":
REM Option A: openwakeword-Modell herunterladen
python -c "from openwakeword.utils import download_models; download_models()"

REM Option B: eigenes .onnx-Modell (z. B. von Picovoice Falcon trainiert)
REM WAKE_WORD_MODEL=C:\pfad\zu\hey_neo.onnx
```

### 7. Mikrofon einrichten

```cmd
REM Verfügbare Mikrofone auflisten
python -c "import sounddevice; print(sounddevice.query_devices())"

REM In .env: MIC_DEVICE=0  (oder den Index deines Mikrofons)
```

### 8. .env konfigurieren

```cmd
copy .env.example .env
notepad .env
```

Mindest-Konfiguration:

```env
RELAY_SERVER_URL=ws://dein-server:8765/ws/agent
AGENT_ID=simon-desktop
AGENT_TOKEN=anderes-langes-passwort
OPENCLAW_URL=http://localhost:18789
OPENCLAW_TOKEN=dein-neo-token
PIPER_VOICE=C:\piper\de_DE-thorsten-medium.onnx
```

### 9. Starten

Doppelklick auf `start.bat` – oder in CMD:

```cmd
cd C:\neo-relay
python main.py
```

---

## Client als Windows-Dienst

### Option A: NSSM (empfohlen)

[nssm.cc](https://nssm.cc) herunterladen, entpacken.

```cmd
REM Als Administrator ausführen
nssm install NeoRelayClient "C:\Python311\python.exe"
nssm set NeoRelayClient AppParameters "C:\neo-relay\main.py"
nssm set NeoRelayClient AppDirectory "C:\neo-relay"
nssm set NeoRelayClient AppStdout "C:\neo-relay\logs\stdout.log"
nssm set NeoRelayClient AppStderr "C:\neo-relay\logs\stderr.log"
nssm set NeoRelayClient AppRotateFiles 1
nssm set NeoRelayClient Start SERVICE_AUTO_START
nssm start NeoRelayClient

REM Status prüfen
nssm status NeoRelayClient
```

### Option B: Task Scheduler

```powershell
# Als Administrator in PowerShell
$action = New-ScheduledTaskAction `
    -Execute "python.exe" `
    -Argument "C:\neo-relay\main.py" `
    -WorkingDirectory "C:\neo-relay"

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Seconds 30) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName "NeoRelayClient" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force

Start-ScheduledTask -TaskName "NeoRelayClient"
```

---

## curl-Beispiele gegen den Server

```bash
SERVER="http://dein-server:8765"
TOKEN="langes-zufaelliges-passwort"
AGENT="simon-desktop"

# Server-Status (kein Auth)
curl "$SERVER/health"

# Verbundene Agents
curl -H "Authorization: Bearer $TOKEN" "$SERVER/agents"

# Python-Code ausführen
curl -s -X POST "$SERVER/agents/$AGENT/run_code" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"language":"python","code":"import platform; print(platform.node())","timeout":10}' | python -m json.tool

# Bash/Shell-Befehl
curl -s -X POST "$SERVER/agents/$AGENT/run_code" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"language":"bash","code":"df -h","timeout":5}' | python -m json.tool

# PowerShell (Windows-Agent)
curl -s -X POST "$SERVER/agents/$AGENT/run_code" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"language":"powershell","code":"Get-ComputerInfo | Select CsName,OsArchitecture","timeout":15}' | python -m json.tool

# App starten
curl -s -X POST "$SERVER/agents/$AGENT/open_app" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"app":"notepad.exe","args":[]}' | python -m json.tool

# In bestimmtem Verzeichnis ausführen
curl -s -X POST "$SERVER/agents/$AGENT/run_code" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"language":"python","code":"import os; print(os.getcwd())","timeout":5,"cwd":"C:\\Users\\Simon"}' | python -m json.tool
```

Beispielantwort `run_code`:

```json
{
  "stdout": "simon-desktop\n",
  "stderr": "",
  "exit_code": 0,
  "error": null
}
```

---

## Tailscale-Setup (kein Port-Forwarding)

Wenn der Heimserver keine feste IP hat oder hinter CGNAT sitzt:

### 1. Tailscale auf allen Maschinen

```bash
# Linux (Server + Linux-Clients)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4   # z. B. 100.64.0.10
```

Windows: Installer von [tailscale.com/download](https://tailscale.com/download)

### 2. Server konfigurieren

```bash
# Nur auf Tailscale-Interface binden (sicherer)
export RELAY_HOST="100.64.0.10"
python relay_server.py
```

### 3. Client `.env` anpassen

```env
# Tailscale-IP oder MagicDNS-Hostname
RELAY_SERVER_URL=ws://100.64.0.10:8765/ws/agent
# oder:
RELAY_SERVER_URL=ws://heimserver.tail1234.ts.net:8765/ws/agent
```

### Tailscale Funnel (öffentlicher HTTPS-Endpunkt)

Falls OpenClaw/Neo nicht im Tailscale-Netz ist:

```bash
sudo tailscale funnel 8765
# Ergibt: https://heimserver.tail1234.ts.net
# Clients dann mit wss:// verbinden
```

---

## Troubleshooting

### Agent verbindet sich nicht

```
⚠  AGENT_TOKEN ist noch der Default-Wert
```
→ `.env` öffnen, `AGENT_TOKEN` auf denselben Wert wie `OPENCLAW_AGENT_TOKEN` auf dem Server setzen.

### Verbindung scheitert (Code 4001)

→ Token stimmt nicht überein. Server-Log prüfen: `journalctl -u neo-relay-server -f`

### Mikrofon nicht gefunden

```cmd
python -c "import sounddevice; print(sounddevice.query_devices())"
```
→ Index des Mikrofons in `.env` setzen: `MIC_DEVICE=1`

### Wake-Word wird nicht erkannt

- Lautstärke zu leise? → Mikrofon-Verstärkung in Windows-Einstellungen erhöhen
- Falsches Modell? → `WAKE_WORD_MODEL=hey_jarvis` als Test verwenden
- Modell nicht installiert? → `python -c "from openwakeword.utils import download_models; download_models()"`

### Piper TTS stumm

- `piper` nicht im PATH? → `where piper` in CMD prüfen
- Falscher Stimm-Pfad? → absoluten Pfad in `.env` angeben
- Linux: `aplay` fehlt? → `sudo apt install alsa-utils`
- Windows: `sox` fehlt? → [sox.sourceforge.net](http://sox.sourceforge.net) oder `choco install sox`

### webrtcvad Installationsfehler (Windows)

```cmd
pip install webrtcvad-wheels
```

### Whisper lädt kein Modell herunter

→ Internetverbindung prüfen. Modelle werden nach `~/.cache/huggingface/hub/` gespeichert.  
→ Manueller Download: `python -c "from faster_whisper import WhisperModel; WhisperModel('base')"`

### torch-Installation schlägt fehl

```cmd
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

---

## Umgebungsvariablen Referenz

### Relay-Server

| Variable | Beschreibung | Standard |
|---|---|---|
| `OPENCLAW_SERVER_TOKEN` | Bearer-Token für HTTP-Endpoints | `dev-server-token-change-me` |
| `OPENCLAW_AGENT_TOKEN` | Token beim WS-Handshake | `dev-agent-token-change-me` |
| `RELAY_HOST` | Bind-Adresse | `0.0.0.0` |
| `RELAY_PORT` | Port | `8765` |

### Client

| Variable | Beschreibung | Standard |
|---|---|---|
| `RELAY_SERVER_URL` | WebSocket-URL des Servers | `ws://localhost:8765/ws/agent` |
| `AGENT_ID` | Eindeutige Agent-ID | Hostname |
| `AGENT_TOKEN` | Muss mit Server-Token übereinstimmen | `change-me` |
| `OPENCLAW_URL` | Neo/OpenClaw Basis-URL | `http://localhost:18789` |
| `OPENCLAW_TOKEN` | Bearer-Token für Neo | *(leer)* |
| `WAKE_WORD_MODEL` | openwakeword-Modell oder .onnx-Pfad | `hey_neo` |
| `WHISPER_MODEL` | faster-whisper Modell-Größe | `base` |
| `WHISPER_LANGUAGE` | Transkriptions-Sprache | `de` |
| `PIPER_VOICE` | Pfad zur Piper .onnx-Datei | *(leer)* |
| `MIC_DEVICE` | sounddevice Geräte-Index oder -Name | *(Standard)* |
| `APP_WHITELIST` | Kommaseparierte erlaubte Apps | *(alle)* |
| `ENABLE_AGENT` | Agent-Loop aktivieren | `true` |
| `ENABLE_VOICE` | Voice-Loop aktivieren | `true` |

---

## Sicherheitshinweise

- **Niemals** Default-Tokens in Produktion lassen – der Server warnt beim Start.
- `OPENCLAW_SERVER_TOKEN` und `AGENT_TOKEN` sollten **unterschiedlich** sein: ein kompromittierter Agent bekommt keinen vollen Server-Zugriff.
- `APP_WHITELIST` auf jedem Client auf das Minimum beschränken.
- Server idealerweise hinter Reverse-Proxy (nginx/Caddy) mit TLS betreiben.
- `run_code` führt **beliebigen Code** aus – nur vertrauenswürdigen Clients Server-Token geben.
- `.env`-Datei **nicht** in Git committen (ist in `.gitignore` einzutragen).
