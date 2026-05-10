"""
main.py – Neo Relay Client: Entry-Point

Startet Agent-Loop (WebSocket) und Voice-Loop (Mikrofon + TTS) parallel.
Beide Loops laufen als asyncio-Tasks; Strg+C löst sauberes Shutdown aus.

Konfiguration via .env (python-dotenv):
    Siehe .env.example für alle Variablen.
"""

import asyncio
import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv

# .env aus dem Client-Verzeichnis laden (vor allen anderen Imports)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

# ---------------------------------------------------------------------------
# Logging konfigurieren
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("main")


def _enabled(env_var: str, default: bool = True) -> bool:
    """Liest einen bool-artigen Env-Wert (true/false/1/0)."""
    val = os.getenv(env_var, "true" if default else "false").strip().lower()
    return val in ("1", "true", "yes", "on")


async def _run_loop(name: str, coro_factory) -> None:
    """
    Hüllt einen Loop-Coroutine so ein, dass unerwartete Exceptions
    geloggt werden, aber der Event-Loop weiterlaufen kann.
    """
    try:
        await coro_factory()
    except asyncio.CancelledError:
        log.info("%s-Loop wurde beendet.", name)
    except Exception as exc:
        log.exception("%s-Loop abgestürzt: %s", name, exc)
        raise


async def main() -> None:
    """Startet Agent- und Voice-Loop parallel, wartet auf beide."""
    enable_agent: bool = _enabled("ENABLE_AGENT", default=True)
    enable_voice: bool = _enabled("ENABLE_VOICE", default=True)

    tasks: list[asyncio.Task] = []

    if enable_agent:
        from client import agent as agent_module  # noqa: PLC0415
        tasks.append(
            asyncio.create_task(
                _run_loop("Agent", agent_module.run),
                name="agent-loop",
            )
        )
        log.info("Agent-Loop aktiviert.")
    else:
        log.info("Agent-Loop deaktiviert (ENABLE_AGENT=false).")

    if enable_voice:
        from client import voice as voice_module  # noqa: PLC0415
        tasks.append(
            asyncio.create_task(
                _run_loop("Voice", voice_module.run),
                name="voice-loop",
            )
        )
        log.info("Voice-Loop aktiviert.")
    else:
        log.info("Voice-Loop deaktiviert (ENABLE_VOICE=false).")

    if not tasks:
        log.warning("Beide Loops deaktiviert – nichts zu tun. Beende.")
        return

    try:
        # Warte, bis einer der Tasks fertig ist (oder ein Fehler auftritt)
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_EXCEPTION
        )

        # Falls ein Task mit Exception endete, loggen und Rest abbrechen
        for task in done:
            if task.exception():
                log.error(
                    "Task '%s' beendet mit Fehler: %s",
                    task.get_name(),
                    task.exception(),
                )

    except asyncio.CancelledError:
        log.info("Shutdown eingeleitet …")

    finally:
        # Alle noch laufenden Tasks sauber beenden
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Alle Loops beendet. Auf Wiedersehen!")


def _run() -> None:
    """Einstiegspunkt: nimmt Strg+C entgegen und beendet sauber."""
    log.info("Neo Relay Client wird gestartet …")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # asyncio.run() fängt CancelledError intern ab; dieser Block
        # greift nur, wenn der Loop schon beendet ist.
        log.info("Durch Strg+C beendet.")
    except Exception as exc:
        log.exception("Unerwarteter Fehler im Haupt-Loop: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    _run()
