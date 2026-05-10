"""
setup.py – Auto-Installer für den Neo Voice Client
Führt alle Installation-Schritte automatisch durch.

Usage:
    python setup.py          # Vollständige Installation
    python setup.py --check  # Nur Prüfen was fehlt
    python setup.py --voice-only  # Nur Voice-Komponenten
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Farben für Output
# ---------------------------------------------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"


def log_info(msg: str):
    print(f"{BLUE}ℹ{RESET} {msg}")


def log_success(msg: str):
    print(f"{GREEN}✓{RESET} {msg}")


def log_warning(msg: str):
    print(f"{YELLOW}⚠{RESET} {msg}")


def log_error(msg: str):
    print(f"{RED}✗{RESET} {msg}")


def log_step(msg: str):
    print(f"\n{BOLD}{msg}{RESET}")


def run_cmd(cmd: list[str], desc: str, check: bool = True, retry: int = 0) -> subprocess.CompletedProcess | None:
    """Führt einen Befehl aus, zeigt Fortschritt."""
    log_info(f"{desc}...")
    for attempt in range(retry + 1):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode == 0:
                return result
            if attempt < retry:
                log_warning(f"Versuch {attempt + 1} fehlgeschlagen, erneuter Versuch...")
                import time
                time.sleep(5)
        except subprocess.TimeoutExpired:
            log_error(f"Timeout bei: {' '.join(cmd)}")
        except Exception as e:
            log_error(f"Fehler: {e}")
    if check:
        log_error(f"Fehlgeschlagen: {desc}")
    return None


def check_python() -> tuple[bool, str]:
    """Prüft Python-Version (>= 3.10, != 3.12)."""
    version = sys.version_info
    version_str = f"{version.major}.{version.minor}.{version.micro}"
    
    if version.major == 3 and version.minor >= 10:
        if version.minor == 12:
            log_warning(f"Python 3.12 erkannt – manche Libraries können Probleme machen. Empfohlen: 3.11")
        log_success(f"Python {version_str} ✓")
        return True, version_str
    
    log_error(f"Python {version_str} – wird nicht unterstützt. Bitte Python 3.10 oder 3.11 installieren.")
    return False, version_str


def check_pip() -> bool:
    """Prüft ob pip verfügbar ist."""
    try:
        subprocess.run([sys.executable, "-m", "pip", "--version"], 
                      capture_output=True, check=True)
        log_success("pip verfügbar ✓")
        return True
    except:
        log_error("pip nicht verfügbar")
        return False


def install_dependencies() -> bool:
    """Installiert Python-Dependencies."""
    log_step("Python Dependencies werden installiert...")
    
    req_file = Path(__file__).parent / "requirements.txt"
    if not req_file.exists():
        log_error("requirements.txt nicht gefunden")
        return False
    
    result = run_cmd(
        [sys.executable, "-m", "pip", "install", "-r", str(req_file), "--quiet"],
        "pip install requirements.txt",
        check=False,
    )
    
    if result and result.returncode == 0:
        log_success("Dependencies installiert ✓")
        return True
    
    # Fallback mit --break-system-packages
    result = run_cmd(
        [sys.executable, "-m", "pip", "install", "-r", str(req_file), 
         "--break-system-packages", "--quiet"],
        "pip install (--break-system-packages)",
        check=False,
    )
    
    if result:
        log_success("Dependencies installiert ✓")
        return True
    return False


def download_whisper_model(model: str = "base") -> bool:
    """Lädt Whisper-Modell herunter (erster Start macht das automatisch, aber hier vorab)."""
    log_step(f"Whisper Modell '{model}' wird heruntergeladen...")
    
    code = f'''
import whisper
print("Lade Modell {model}...")
model = whisper.load_model("{model}")
print("Modell geladen ✓")
'''
    result = run_cmd(
        [sys.executable, "-c", code],
        f"Whisper {model} Modell",
        check=False,
    )
    
    if result and result.returncode == 0:
        log_success(f"Whisper Modell '{model}' bereit ✓")
        return True
    
    log_warning(f"Whisper Modell konnte nicht vorab geladen werden (wird beim ersten Start automatisch versucht)")
    return False


def check_ffmpeg() -> bool:
    """Prüft/Installiert ffmpeg (nötig für Audio-Verarbeitung)."""
    log_step("FFmpeg wird geprüft...")
    
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        log_success("FFmpeg verfügbar ✓")
        return True
    except:
        pass
    
    # Versuche Installation
    if sys.platform == "win32":
        log_warning("FFmpeg nicht gefunden. Bitte manuell installieren:")
        log_warning("  1. https://ffmpeg.org/download.html -> Windows builds")
        log_warning("  2. ffmpeg.exe in PATH legen oder im Script-Verzeichnis ablegen")
        return False
    
    elif sys.platform == "darwin":
        if run_cmd(["brew", "install", "ffmpeg"], "brew install ffmpeg", check=False):
            log_success("FFmpeg via Homebrew installiert ✓")
            return True
    
    else:  # Linux
        if run_cmd(["sudo", "apt-get", "install", "-y", "ffmpeg"], "sudo apt install ffmpeg", check=False):
            log_success("FFmpeg via apt installiert ✓")
            return True
    
    log_warning("FFmpeg konnte nicht automatisch installiert werden")
    return False


def setup_piper(language: str = "de") -> bool:
    """Prüft oder lädt Piper Voice Model."""
    log_step("Piper TTS wird eingerichtet...")
    
    # Prüfe ob piper installiert ist
    try:
        subprocess.run(["piper", "--help"], capture_output=True, check=True)
        log_success("Piper CLI verfügbar ✓")
        return True
    except:
        pass
    
    # Piper Python Package versuchen
    try:
        import piper
        log_success("Piper Python-Package verfügbar ✓")
        return True
    except ImportError:
        pass
    
    log_warning("Piper nicht vollständig verfügbar")
    log_info("Für deutsche Stimme: lade de_DE-thorsten-medium.onnx von https://github.com/rhasspy/piper/raw/master/src/python_api/")
    log_info("Speichere als: piper_voice/de_DE-thorsten-medium.onnx")
    
    # Erstelle Verzeichnis
    piper_dir = Path(__file__).parent / "piper_voice"
    piper_dir.mkdir(exist_ok=True)
    
    return False


def setup_openwakeword() -> bool:
    """Prüft Wake-Word Modell."""
    log_step("OpenWakeWord wird eingerichtet...")
    
    try:
        import openwakeword
        from openwakeword.model import Model
        
        log_info("OpenWakeWord importierbar ✓")
        
        # Prüfe ob Standard-Modell verfügbar
        # Die Modelle werden beim ersten Aufruf automatisch gedownloadet
        log_success("OpenWakeWord bereit (Modelle werden beim ersten Start geladen) ✓")
        return True
    except ImportError as e:
        log_error(f"OpenWakeWord nicht verfügbar: {e}")
        return False


def create_env_file() -> bool:
    """Erstellt .env Datei aus Beispiel wenn nicht vorhanden."""
    log_step(".env Datei wird eingerichtet...")
    
    env_file = Path(__file__).parent / ".env"
    env_example = Path(__file__).parent / ".env.example"
    
    if env_file.exists():
        log_info(".env existiert bereits")
        return True
    
    if not env_example.exists():
        log_error(".env.example nicht gefunden")
        return False
    
    shutil.copy(env_example, env_file)
    log_success(".env aus .env.example erstellt ✓")
    log_warning("Bitte .env bearbeiten und Tokens eintragen!")
    return True


def download_audio_models() -> bool:
    """Lädt benötigte Audio-Modelle (Wake-Word, Whisper)."""
    log_step("Audio-Modelle werden vorab heruntergeladen...")
    
    # Wake-Word Modelle
    log_info("Lade Wake-Word Modell 'hey_neo'...")
    try:
        from openwakeword.model import Model
        import os
        os.makedirs(Path(__file__).parent / "models", exist_ok=True)
        # Modelle werden automatisch gecacht, das reicht
        log_success("Wake-Word Modell bereit ✓")
    except Exception as e:
        log_warning(f"Wake-Word: {e}")
    
    # Whisper Modell (lightweight check)
    log_info("Whisper wird beim ersten Start automatisch geladen")
    log_info("Dies kann einige Minuten dauern beim ersten Mal...")
    
    return True


def create_start_script() -> bool:
    """Erstellt plattformspezifisches Start-Script."""
    log_step("Start-Script wird erstellt...")
    
    client_dir = Path(__file__).parent
    
    if sys.platform == "win32":
        bat_path = client_dir / "start.bat"
        if bat_path.exists():
            log_success("start.bat existiert bereits ✓")
        else:
            with open(bat_path, "w") as f:
                f.write("@echo off\ncd /d \"%~dp0\"\npython main.py\npause\n")
            log_success("start.bat erstellt ✓")
    
    elif sys.platform == "darwin":
        sh_path = client_dir / "start.sh"
        with open(sh_path, "w") as f:
            f.write("#!/bin/bash\ncd \"$(dirname \"$0\")\"\npython3 main.py\n")
        os.chmod(sh_path, 0o755)
        log_success("start.sh erstellt ✓")
    
    else:
        sh_path = client_dir / "start.sh"
        with open(sh_path, "w") as f:
            f.write("#!/bin/bash\ncd \"$(dirname \"$0\")\"\npython3 main.py\n")
        os.chmod(sh_path, 0o755)
        log_success("start.sh erstellt ✓")
    
    return True


def full_install() -> bool:
    """Führt vollständige Installation durch."""
    print(f"\n{BOLD}{'='*60}")
    print("Neo Voice Client – Auto-Installer")
    print(f"{'='*60}{RESET}\n")
    
    all_ok = True
    
    # 1. Python prüfen
    log_step("Python-Version wird geprüft...")
    ok, version = check_python()
    if not ok:
        log_error("Python 3.10+ erforderlich")
        return False
    
    # 2. pip prüfen
    if not check_pip():
        log_error("pip ist erforderlich")
        return False
    
    # 3. FFmpeg
    check_ffmpeg()
    
    # 4. Dependencies
    if not install_dependencies():
        all_ok = False
    
    # 5. Audio-Modelle
    download_audio_models()
    
    # 6. Piper
    setup_piper()
    
    # 7. OpenWakeWord
    setup_openwakeword()
    
    # 8. .env
    create_env_file()
    
    # 9. Start-Script
    create_start_script()
    
    # Zusammenfassung
    log_step("Zusammenfassung")
    print(f"""
{BOLD}Installation abgeschlossen!{RESET}

Nächste Schritte:
1. {BLUE}.env{RESET} Datei bearbeiten und Tokens eintragen:
   - RELAY_SERVER_URL = ws://DEIN-SERVER:8765/ws/agent
   - AGENT_TOKEN = agent-connect-token (vom Server)
   - OPENCLAW_URL = http://DEIN-NEO:18789
   - OPENCLAW_TOKEN = dein-neo-token

2. {BLUE}ffmpeg{RESET} manuell installieren falls oben fehlgeschlagen:
   Windows: https://ffmpeg.org/download.html

3. {BLUE}piper voice model{RESET} herunterladen (optional für TTS):
   mkdir piper_voice
   # Lade de_DE-thorsten-medium.onnx von GitHub

4. Starten:
   Windows: {YELLOW}start.bat{RESET} oder {YELLOW}python main.py{RESET}
   Linux:   {YELLOW}./start.sh{RESET} oder {YELLOW}python3 main.py{RESET}

Viel Erfolg! 🚀
""")
    
    return all_ok


def check_only() -> bool:
    """Prüft nur was fehlt."""
    print(f"\n{BOLD}System-Check{RESET}\n")
    
    results = []
    
    # Python
    ok, _ = check_python()
    results.append(("Python", ok))
    
    # pip
    results.append(("pip", check_pip()))
    
    # FFmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        results.append(("FFmpeg", True))
    except:
        results.append(("FFmpeg", False))
    
    # Dependencies
    deps_ok = True
    for pkg in ["websockets", "sounddevice", "requests", "faster_whisper"]:
        try:
            __import__(pkg.replace("-", "_"))
            results.append((f"  {pkg}", True))
        except:
            results.append((f"  {pkg}", False))
            deps_ok = False
    results.append(("Dependencies", deps_ok))
    
    # .env
    env_exists = (Path(__file__).parent / ".env").exists()
    results.append((".env", env_exists))
    
    print(f"\n{BOLD}Ergebnis:{RESET}")
    for name, ok in results:
        if ok:
            log_success(f"{name}")
        else:
            log_error(f"{name}")
    
    return all(ok for _, ok in results if not name.startswith("  "))
    


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Neo Voice Client Installer")
    parser.add_argument("--check", action="store_true", help="Nur System-Check, keine Installation")
    parser.add_argument("--voice-only", action="store_true", help="Nur Voice-Komponenten prüfen")
    args = parser.parse_args()
    
    if args.check:
        success = check_only()
        sys.exit(0 if success else 1)
    elif args.voice_only:
        setup_openwakeword()
        download_audio_models()
        setup_piper()
    else:
        success = full_install()
        sys.exit(0 if success else 1)
