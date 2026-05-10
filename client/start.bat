@echo off
REM Neo Relay Client – Windows Doppelklick-Starter
REM Setzt das Arbeitsverzeichnis auf das Verzeichnis dieser Batch-Datei.

cd /d "%~dp0"

REM Prüfen ob Python vorhanden ist
python --version >nul 2>&1
if errorlevel 1 (
    echo FEHLER: Python nicht gefunden.
    echo Bitte Python 3.11+ von https://www.python.org/downloads/ installieren.
    echo Wichtig: Haken bei "Add Python to PATH" setzen!
    pause
    exit /b 1
)

REM Prüfen ob .env existiert
if not exist ".env" (
    echo HINWEIS: .env nicht gefunden.
    echo Bitte .env.example nach .env kopieren und anpassen:
    echo   copy .env.example .env
    echo   notepad .env
    pause
    exit /b 1
)

REM Abhängigkeiten prüfen (nur Warnung, kein Abbruch)
python -c "import websockets, dotenv, sounddevice" >nul 2>&1
if errorlevel 1 (
    echo Abhängigkeiten werden installiert ...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo FEHLER beim Installieren der Abhängigkeiten.
        pause
        exit /b 1
    )
)

echo Neo Relay Client wird gestartet ...
echo Drücke Strg+C zum Beenden.
echo.
python main.py

echo.
echo Client beendet.
pause
