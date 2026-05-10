"""
client/voice.py – Loop B: Voice-Assistent

Ablauf:
  1. Mikrofon-Stream öffnen (sounddevice)
  2. Auf Wake-Word warten (openwakeword oder Fallback: hey_jarvis)
  3. Bestätigungston abspielen
  4. Sprache aufnehmen bis ~1,5 s Stille (webrtcvad oder Energie-Schwelle)
  5. Transkription mit faster-whisper
  6. POST an OpenClaw/Neo
  7. Antwort per Piper TTS vorlesen
  8. Zurück zu Schritt 2

Konfiguration via .env:
    OPENCLAW_URL       – Basis-URL des Neo-Servers
    OPENCLAW_TOKEN     – Bearer-Token für Neo
    WAKE_WORD_MODEL    – openwakeword-Modell (default: hey_neo)
                         Für Custom-Modelle: Pfad zur .onnx-Datei
                         oder Name eines vorinstallierten Modells.
                         Fallback auf "hey_jarvis" wenn kein Modell geladen werden kann.
    WHISPER_MODEL      – faster-whisper Modell (default: base)
    WHISPER_LANGUAGE   – Sprache für Transkription (default: de)
    PIPER_VOICE        – Pfad zur Piper .onnx-Datei (z. B. de_DE-thorsten-medium.onnx)
    MIC_DEVICE         – sounddevice-Gerät-Index oder -Name (leer = Standard)
"""

import asyncio
import io
import logging
import os
import platform
import queue
import subprocess
import tempfile
import threading
import wave
from typing import Any, Optional

import numpy as np
import requests
import sounddevice as sd

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
OPENCLAW_URL: str = os.getenv("OPENCLAW_URL", "http://localhost:18789")
OPENCLAW_TOKEN: str = os.getenv("OPENCLAW_TOKEN", "")
WAKE_WORD_MODEL: str = os.getenv("WAKE_WORD_MODEL", "hey_neo")
WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base")
WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "de")
PIPER_VOICE: str = os.getenv("PIPER_VOICE", "")
MIC_DEVICE_RAW: str = os.getenv("MIC_DEVICE", "").strip()
MIC_DEVICE: Optional[Any] = int(MIC_DEVICE_RAW) if MIC_DEVICE_RAW.isdigit() else (MIC_DEVICE_RAW or None)

# Audio-Parameter
_SAMPLE_RATE: int = 16000
_CHANNELS: int = 1
_CHUNK_FRAMES: int = 1280          # 80 ms bei 16 kHz – openwakeword-Fenster
_SILENCE_THRESHOLD: float = 0.01  # RMS-Schwelle für Stille
_SILENCE_DURATION_S: float = 1.5  # Sekunden Stille → Aufnahme stopp
_MAX_RECORD_S: float = 30.0       # Maximale Aufnahmedauer

log = logging.getLogger("voice")


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def _numpy_to_wav_bytes(audio: np.ndarray, sample_rate: int = _SAMPLE_RATE) -> bytes:
    """Konvertiert ein float32-NumPy-Array in WAV-Bytes (int16, mono)."""
    pcm = (audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def _play_tone(freq: float = 880.0, duration: float = 0.15, volume: float = 0.4) -> None:
    """Gibt einen kurzen Sinuston zur Bestätigung aus (blockierend, kurz)."""
    try:
        t = np.linspace(0, duration, int(_SAMPLE_RATE * duration), endpoint=False)
        tone = (np.sin(2 * np.pi * freq * t) * volume).astype(np.float32)
        sd.play(tone, samplerate=_SAMPLE_RATE, blocking=True)
    except Exception as exc:
        log.debug("Bestätigungston konnte nicht gespielt werden: %s", exc)


def _speak(text: str, piper_voice: str) -> None:
    """
    Spricht Text per Piper TTS aus (subprocess).

    Piper liest Text von stdin und gibt PCM-Audio aus. Das Audio wird direkt
    über aplay (Linux) oder SoX (Windows/macOS) wiedergegeben.

    Args:
        text:        Auszusprechender Text
        piper_voice: Pfad zur Piper .onnx-Sprachmodell-Datei
    """
    if not piper_voice or not os.path.isfile(piper_voice):
        log.warning("Piper-Stimme nicht gefunden: %r – TTS übersprungen.", piper_voice)
        return

    try:
        # Piper gibt raw PCM (16-Bit, 22050 Hz, mono) auf stdout aus
        piper_proc = subprocess.Popen(
            ["piper", "--model", piper_voice, "--output-raw"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

        system = platform.system().lower()

        if system == "linux":
            # aplay liest raw PCM direkt
            play_proc = subprocess.Popen(
                ["aplay", "--rate=22050", "--format=S16_LE", "--channels=1", "-"],
                stdin=piper_proc.stdout,
                stderr=subprocess.DEVNULL,
            )
        elif system == "windows":
            # SoX auf Windows: sox -t raw -r 22050 -e signed -b 16 -c 1 - -d
            play_proc = subprocess.Popen(
                ["sox", "-t", "raw", "-r", "22050", "-e", "signed", "-b", "16", "-c", "1", "-", "-d"],
                stdin=piper_proc.stdout,
                stderr=subprocess.DEVNULL,
            )
        else:
            # macOS: sox analog
            play_proc = subprocess.Popen(
                ["sox", "-t", "raw", "-r", "22050", "-e", "signed", "-b", "16", "-c", "1", "-", "-d"],
                stdin=piper_proc.stdout,
                stderr=subprocess.DEVNULL,
            )

        piper_proc.stdin.write(text.encode("utf-8"))
        piper_proc.stdin.close()
        piper_proc.wait(timeout=30)
        play_proc.wait(timeout=30)

    except FileNotFoundError:
        log.error("Piper oder Audioplayer (aplay/sox) nicht gefunden – TTS übersprungen.")
    except subprocess.TimeoutExpired:
        log.warning("TTS-Wiedergabe-Timeout.")
    except Exception as exc:
        log.exception("TTS-Fehler: %s", exc)


def _speak_error(msg: str, piper_voice: str) -> None:
    """Spricht eine Fehlermeldung aus (oder loggt nur, wenn kein TTS)."""
    log.warning("FEHLER-TTS: %s", msg)
    _speak(msg, piper_voice)


# ---------------------------------------------------------------------------
# Wake-Word-Detektion
# ---------------------------------------------------------------------------
class WakeWordDetector:
    """
    Umhüllt openwakeword für Audio-Frame-weise Erkennung.

    Für Custom-Modelle: WAKE_WORD_MODEL auf den Pfad zur .onnx-Datei setzen.
    Alle verfügbaren vorinstallierten Modelle: python -c "import openwakeword; print(openwakeword.MODELS)"

    Falls das gewünschte Modell nicht geladen werden kann, wird
    automatisch auf "hey_jarvis" (vorinstalliert) zurückgefallen.
    """

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._oww_model = None
        self._active_key: Optional[str] = None
        self._threshold: float = 0.5
        self._load(model_name)

    def _load(self, model_name: str) -> None:
        """Lädt das openwakeword-Modell; fällt auf hey_jarvis zurück."""
        try:
            from openwakeword.model import Model  # type: ignore

            if os.path.isfile(model_name):
                # Pfad zu einer Custom-.onnx-Datei
                self._oww_model = Model(wakeword_models=[model_name], inference_framework="onnx")
                # Key ist der Dateiname ohne Extension
                self._active_key = os.path.splitext(os.path.basename(model_name))[0]
                log.info("Custom Wake-Word-Modell geladen: %s", model_name)
            else:
                # Vorinstalliertes Modell anhand des Namens
                self._oww_model = Model(wakeword_models=[model_name], inference_framework="onnx")
                self._active_key = model_name
                log.info("Wake-Word-Modell geladen: %s", model_name)

        except Exception as exc:
            log.warning(
                "Konnte Wake-Word-Modell '%s' nicht laden (%s). "
                "Versuche Fallback 'hey_jarvis' …",
                model_name,
                exc,
            )
            try:
                from openwakeword.model import Model  # type: ignore

                self._oww_model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
                self._active_key = "hey_jarvis"
                log.info("Fallback Wake-Word 'hey_jarvis' geladen.")
            except Exception as exc2:
                log.error(
                    "Auch 'hey_jarvis' konnte nicht geladen werden: %s. "
                    "Wake-Word-Erkennung deaktiviert.",
                    exc2,
                )

    def detect(self, audio_chunk: np.ndarray) -> bool:
        """
        Prüft einen Audio-Chunk (float32, 16 kHz) auf das Wake-Word.

        Args:
            audio_chunk: NumPy-Array mit _CHUNK_FRAMES Samples

        Returns:
            True wenn Wake-Word erkannt, sonst False
        """
        if self._oww_model is None:
            return False
        try:
            pcm_int16 = (audio_chunk * 32767).astype(np.int16)
            prediction = self._oww_model.predict(pcm_int16)
            if self._active_key and prediction.get(self._active_key, 0) >= self._threshold:
                return True
            # Auch andere Keys prüfen (bei Modellen mit mehreren Wörtern)
            return any(v >= self._threshold for v in prediction.values())
        except Exception as exc:
            log.debug("Wake-Word-Fehler: %s", exc)
            return False

    @property
    def active(self) -> bool:
        """True wenn ein Modell geladen ist."""
        return self._oww_model is not None


# ---------------------------------------------------------------------------
# VAD-basiertes Aufnahme-Ende
# ---------------------------------------------------------------------------
class SilenceDetector:
    """
    Erkennt Stille in Audio-Chunks.
    Nutzt webrtcvad falls verfügbar, sonst einfache Energie-Schwelle.
    """

    def __init__(self) -> None:
        self._vad = None
        self._use_vad = False

        try:
            import webrtcvad  # type: ignore

            self._vad = webrtcvad.Vad(2)  # Aggressivitätsstufe 2
            self._use_vad = True
            log.debug("webrtcvad wird für Stille-Erkennung verwendet.")
        except ImportError:
            log.debug("webrtcvad nicht verfügbar – nutze Energie-Schwelle.")

    def is_speech(self, audio_chunk: np.ndarray) -> bool:
        """
        Gibt True zurück wenn der Chunk Sprache enthält.

        Args:
            audio_chunk: float32 Array, 16 kHz, ~30 ms (480 Samples)
        """
        if self._use_vad and self._vad is not None:
            try:
                pcm = (audio_chunk * 32767).astype(np.int16)
                # webrtcvad erwartet genau 480 Samples bei 16 kHz / 30 ms
                chunk_30ms = pcm[:480] if len(pcm) >= 480 else np.pad(pcm, (0, 480 - len(pcm)))
                return self._vad.is_speech(chunk_30ms.tobytes(), sample_rate=_SAMPLE_RATE)
            except Exception:
                pass
        # Fallback: RMS-Energie
        rms = float(np.sqrt(np.mean(audio_chunk.astype(np.float32) ** 2)))
        return rms > _SILENCE_THRESHOLD


# ---------------------------------------------------------------------------
# Haupt-Sprachverarbeitung
# ---------------------------------------------------------------------------
class VoiceProcessor:
    """
    Koordiniert Wake-Word, Aufnahme, Transkription, API-Call und TTS.
    """

    def __init__(self) -> None:
        self._wake_detector = WakeWordDetector(WAKE_WORD_MODEL)
        self._silence_detector = SilenceDetector()
        self._whisper = None
        self._piper_voice = PIPER_VOICE

        # faster-whisper laden
        try:
            from faster_whisper import WhisperModel  # type: ignore

            self._whisper = WhisperModel(
                WHISPER_MODEL,
                device="cpu",
                compute_type="int8",  # RAM-schonend
            )
            log.info("Whisper-Modell '%s' geladen.", WHISPER_MODEL)
        except Exception as exc:
            log.error("Whisper konnte nicht geladen werden: %s", exc)

    def _transcribe(self, audio: np.ndarray) -> str:
        """
        Transkribiert Audio mit faster-whisper.

        Args:
            audio: float32-Array, 16 kHz

        Returns:
            Transkribierter Text (leer bei Fehler)
        """
        if self._whisper is None:
            return ""
        try:
            segments, _ = self._whisper.transcribe(
                audio,
                language=WHISPER_LANGUAGE,
                beam_size=5,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            log.info("Transkription: %r", text)
            return text
        except Exception as exc:
            log.error("Transkriptions-Fehler: %s", exc)
            return ""

    def _call_openclaw(self, message: str) -> Optional[str]:
        """
        Sendet die transkribierte Nachricht an OpenClaw/Neo.

        Args:
            message: Transkribierter Text

        Returns:
            Antwort-Text oder None bei Fehler
        """
        url = f"{OPENCLAW_URL.rstrip('/')}/chat"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if OPENCLAW_TOKEN:
            headers["Authorization"] = f"Bearer {OPENCLAW_TOKEN}"

        try:
            resp = requests.post(
                url,
                json={"message": message},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            # Antwort kann unter verschiedenen Keys liegen
            reply = (
                data.get("reply")
                or data.get("response")
                or data.get("message")
                or data.get("text")
                or str(data)
            )
            log.info("OpenClaw Antwort: %r", reply)
            return str(reply)
        except requests.ConnectionError:
            log.error("OpenClaw nicht erreichbar: %s", url)
            return None
        except requests.HTTPError as exc:
            log.error("OpenClaw HTTP-Fehler: %s", exc)
            return None
        except Exception as exc:
            log.exception("OpenClaw-Fehler: %s", exc)
            return None

    def record_until_silence(self, audio_queue: "queue.Queue[np.ndarray]") -> np.ndarray:
        """
        Liest Audio aus dem Queue bis 1,5 s Stille erkannt werden.

        Args:
            audio_queue: Queue mit float32-Chunks vom Mikrofon-Callback

        Returns:
            Gesamte Aufnahme als float32-Array
        """
        recorded: list[np.ndarray] = []
        silent_chunks: int = 0
        max_silent = int(_SILENCE_DURATION_S * _SAMPLE_RATE / _CHUNK_FRAMES)
        max_chunks = int(_MAX_RECORD_S * _SAMPLE_RATE / _CHUNK_FRAMES)
        chunk_count: int = 0

        log.info("Aufnahme läuft …")

        while chunk_count < max_chunks:
            try:
                chunk = audio_queue.get(timeout=2.0)
            except queue.Empty:
                break

            recorded.append(chunk)
            chunk_count += 1

            if self._silence_detector.is_speech(chunk):
                silent_chunks = 0
            else:
                silent_chunks += 1
                if silent_chunks >= max_silent:
                    log.debug("Stille erkannt nach %d Chunks.", chunk_count)
                    break

        if not recorded:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(recorded)

    async def handle_voice_request(self, audio: np.ndarray) -> None:
        """
        Verarbeitet eine vollständige Sprach-Aufnahme von Transkription bis TTS.

        Args:
            audio: float32-Array der vollständigen Aufnahme
        """
        loop = asyncio.get_running_loop()

        # Transkription im Thread-Pool (CPU-intensiv)
        text = await loop.run_in_executor(None, self._transcribe, audio)

        if not text:
            log.warning("Leere Transkription – ignoriert.")
            await loop.run_in_executor(
                None,
                _speak,
                "Entschuldigung, ich habe dich nicht verstanden.",
                self._piper_voice,
            )
            return

        # Status: verstanden
        _play_tone(freq=1200.0, duration=0.1)

        # API-Call
        reply = await loop.run_in_executor(None, self._call_openclaw, text)

        if reply is None:
            await loop.run_in_executor(
                None,
                _speak,
                "Der Server ist nicht erreichbar. Bitte überprüfe deine Verbindung.",
                self._piper_voice,
            )
            return

        # Antwort vorlesen
        await loop.run_in_executor(None, _speak, reply, self._piper_voice)


# ---------------------------------------------------------------------------
# Haupt-Loop
# ---------------------------------------------------------------------------
async def run() -> None:
    """
    Haupt-Loop: öffnet Mikrofon, wartet auf Wake-Word, verarbeitet Sprache.
    Läuft dauerhaft bis CancelledError.
    """
    log.info("Voice-Loop gestartet.")
    log.info("  Wake-Word  : %s", WAKE_WORD_MODEL)
    log.info("  Whisper    : %s (%s)", WHISPER_MODEL, WHISPER_LANGUAGE)
    log.info("  Piper-Stimme: %s", PIPER_VOICE or "(nicht konfiguriert)")
    log.info("  OpenClaw   : %s", OPENCLAW_URL)
    log.info("  Mikrofon   : %s", MIC_DEVICE or "Standard")

    processor = VoiceProcessor()

    # Status-Sound: "ich höre zu"
    _play_tone(freq=660.0, duration=0.2)

    audio_q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=500)
    recording_event = threading.Event()

    def _mic_callback(
        indata: np.ndarray,
        frames: int,
        time_info: Any,
        status: Any,
    ) -> None:
        """Wird vom sounddevice-Thread aufgerufen; legt Chunks in die Queue."""
        if status:
            log.debug("Mikrofon-Status: %s", status)
        chunk = indata[:, 0].copy()  # Mono
        try:
            audio_q.put_nowait(chunk)
        except queue.Full:
            log.warning("Audio-Queue voll – Frame verworfen (Mikrofon zu schnell?)")

    loop = asyncio.get_running_loop()

    try:
        with sd.InputStream(
            samplerate=_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype="float32",
            blocksize=_CHUNK_FRAMES,
            device=MIC_DEVICE,
            callback=_mic_callback,
        ):
            log.info("Mikrofon offen – lausche auf Wake-Word '%s' …", WAKE_WORD_MODEL)

            while True:
                # Warte auf Wake-Word (nicht blockierend für asyncio)
                wake_detected = False
                while not wake_detected:
                    try:
                        chunk = audio_q.get_nowait()
                        wake_detected = processor._wake_detector.detect(chunk)
                        # Kurz yield damit asyncio andere Tasks bearbeiten kann
                        await asyncio.sleep(0)

                    except queue.Empty:
                        # 5 ms warten wenn Queue leer – reduziert CPU-Last deutlich
                        await asyncio.sleep(0.005)
                    except asyncio.CancelledError:
                        log.info("Voice-Loop abgebrochen (Wake-Word-Phase).")
                        return

                log.info("Wake-Word erkannt!")

                # Bestätigungston
                await loop.run_in_executor(None, _play_tone, 880.0, 0.15, 0.5)

                # Queue leeren (Wake-Word-Audio nicht aufnehmen)
                while not audio_q.empty():
                    try:
                        audio_q.get_nowait()
                    except queue.Empty:
                        break

                # Aufnahme bis Stille
                try:
                    audio = await loop.run_in_executor(
                        None, processor.record_until_silence, audio_q
                    )
                except asyncio.CancelledError:
                    log.info("Voice-Loop abgebrochen (Aufnahme-Phase).")
                    return

                if len(audio) < _SAMPLE_RATE * 0.3:
                    log.info("Aufnahme zu kurz – ignoriert.")
                    continue

                # Verarbeitung (Transkription + API + TTS)
                try:
                    await processor.handle_voice_request(audio)
                except asyncio.CancelledError:
                    log.info("Voice-Loop abgebrochen (Verarbeitungs-Phase).")
                    return
                except Exception as exc:
                    log.exception("Fehler bei Sprachverarbeitung: %s", exc)
                    await loop.run_in_executor(
                        None,
                        _speak_error,
                        "Es ist ein Fehler aufgetreten.",
                        processor._piper_voice,
                    )

                # Status: bereit für nächstes Wake-Word
                _play_tone(freq=440.0, duration=0.1)
                log.info("Bereit – lausche wieder auf Wake-Word …")

    except sd.PortAudioError as exc:
        log.error("Mikrofon-Fehler: %s", exc)
        log.error(
            "Verfügbare Mikrofone: %s",
            [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0],
        )
        raise
    except asyncio.CancelledError:
        log.info("Voice-Loop sauber beendet.")
    except Exception as exc:
        log.exception("Unerwarteter Fehler im Voice-Loop: %s", exc)
        raise
