"""Smoke test — end-to-end conversation hitting Gemini.

The booking backend is the in-memory mock, so only a Gemini API key is
required (loaded from `backend/.env`). Runs three short scripted
conversations (info / booking / manage) and prints each turn's reply and
tool trace to stdout.
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

# Make `app.*` importable when running this file directly.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(ROOT / "backend" / ".env")

# Sanity check before importing the agent (config.py is fail-fast).
if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
    print("⚠️  Missing env: GEMINI_API_KEY.\n"
          "   Copy backend/.env.example to backend/.env and fill it.")
    sys.exit(1)

from app.agents import router as agent_router  # noqa: E402


_TURNS_BOOKING = [
    "ciao",
    "voglio prenotare una visita",
    "privato",
    "non ho preferenze",
    "una visita cardiologica",
    "va bene il primo slot",
    "sì confermo",
]

_TURNS_MANAGE = [
    "vorrei disdire un appuntamento",
    "il primo",
    "sì cancella",
]

_TURNS_INFO = [
    "che orari fate?",
    "e per il parcheggio?",
]


async def _run_session(turns: list[str], label: str) -> None:
    session = f"smoke-{label}-{uuid.uuid4().hex[:6]}"
    print(f"\n══════ {label.upper()} ({session}) ══════")
    for turn in turns:
        print(f"\n  USER : {turn}")
        result = await agent_router.dispatch(session, turn)
        debug = result.get("debug", {})
        print(f"  FLOW : {debug.get('flow')} / {debug.get('phase_name')}")
        for reply in result.get("replies", []):
            print(f"  BOT  : {reply}")
        tools = [t["tool"] for t in debug.get("tool_trace", [])]
        if tools:
            print(f"  TOOLS: {tools}")


async def _main() -> None:
    await _run_session(_TURNS_INFO,    "info-inline")
    await _run_session(_TURNS_BOOKING, "booking")
    await _run_session(_TURNS_MANAGE,  "manage")


if __name__ == "__main__":
    asyncio.run(_main())
