"""
tao_bot_listener.py — On-demand Telegram command listener
===========================================================
Polls the Telegram Bot API for commands and triggers scoring runs on demand.
Runs as a persistent background process on Infinity8 (separate from cron).

Commands:
    /status   — run full scoring cycle and send results immediately
    /macro    — show current TAO macro regime from tao_macro.json
    /holdings — show current holdings pass/fail status
    /help     — list available commands

Start (background):
    nohup python3 tao_bot_listener.py >> /home/simar/tao-monitor/bot_listener.log 2>&1 &

Stop:
    pkill -f tao_bot_listener.py

Check if running:
    pgrep -f tao_bot_listener.py

Environment variables (loaded from .env):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    TAOSTATS_API_KEY
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bot_listener] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot_listener")

BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY    = os.environ.get("TAOSTATS_API_KEY", "")
SCRIPT_DIR = Path(__file__).parent
MACRO_FILE = SCRIPT_DIR / "tao_macro.json"
STATE_FILE = SCRIPT_DIR / "scoring_state.json"

POLL_INTERVAL = 2   # seconds between Telegram getUpdates polls
COMMAND_COOLDOWN = 30  # seconds — ignore repeated commands within this window

_last_command_ts: float = 0
_last_update_id: int = 0


def send(text: str) -> None:
    """Send a message to the configured Telegram chat."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("No bot token/chat ID — cannot send")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Send failed: {e}")


def get_updates(offset: int) -> list[dict]:
    """Poll Telegram for new messages."""
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 20, "allowed_updates": ["message"]},
            timeout=25,
        )
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as e:
        logger.error(f"getUpdates failed: {e}")
    return []


def handle_status() -> None:
    """Run a full scoring cycle and send results."""
    send("⏳ Running scoring cycle...")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "run_scoring.py"),
             "--no-concentration", "--force-send"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ},
        )
        if result.returncode != 0:
            send(f"🔴 Scoring run failed:\n<pre>{result.stderr[-500:]}</pre>")
        else:
            logger.info("Status command: scoring run completed")
            # The scoring run already sent the Telegram message via --force-send
    except subprocess.TimeoutExpired:
        send("🔴 Scoring run timed out (>60s)")
    except Exception as e:
        send(f"🔴 Error running scoring: {e}")


def handle_macro() -> None:
    """Show current TAO macro regime."""
    try:
        if not MACRO_FILE.exists():
            send("⚠️ tao_macro.json not found — macro cron may not have run yet")
            return
        age_min = (time.time() - MACRO_FILE.stat().st_mtime) / 60
        data = json.loads(MACRO_FILE.read_text())
        regime  = data.get("current_regime", "Unknown")
        signal  = data.get("signal", 0)
        probs   = data.get("next_state_probabilities", {})
        bull_p  = probs.get("bull", 0)
        bear_p  = probs.get("bear", 0)

        emoji = "🟢" if regime == "Bull" else ("🔴" if regime == "Bear" else "🟡")
        msg = (
            f"🌍 TAO Macro Regime\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} <b>{regime}</b>\n"
            f"Signal: {signal:+.3f}\n"
            f"Bull: {bull_p:.0%}  Bear: {bear_p:.0%}\n"
            f"Data age: {age_min:.0f} min"
        )
        send(msg)
    except Exception as e:
        send(f"🔴 Error reading macro: {e}")


def handle_holdings() -> None:
    """Show last known holdings status from state file."""
    try:
        if not STATE_FILE.exists():
            send("⚠️ No scoring state yet — wait for first cron cycle")
            return
        age_min = (time.time() - STATE_FILE.stat().st_mtime) / 60
        state = json.loads(STATE_FILE.read_text())
        snapshot = state.get("snapshot", {})
        failing  = snapshot.get("failing_holdings", {})
        passing  = snapshot.get("passing_holdings", [])

        lines = [f"📋 Holdings Status (as of {age_min:.0f} min ago)\n━━━━━━━━━━━━━━━━━━━━"]
        for sn_id in passing:
            lines.append(f"✅ SN{sn_id}")
        for sn_id, reason in failing.items():
            lines.append(f"🔴 SN{sn_id} — {reason}")
        send("\n".join(lines))
    except Exception as e:
        send(f"🔴 Error reading state: {e}")


def handle_help() -> None:
    send(
        "🤖 <b>TAO Monitor Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "/status   — run scoring now and send full update\n"
        "/macro    — show current TAO macro regime\n"
        "/holdings — show holdings pass/fail from last cycle\n"
        "/help     — this message\n\n"
        "Automatic alerts send on:\n"
        "• New critical failure on a holding\n"
        "• Holding recovers from failure\n"
        "• Every 4 hours (digest)"
    )


HANDLERS = {
    "/status":   handle_status,
    "/macro":    handle_macro,
    "/holdings": handle_holdings,
    "/help":     handle_help,
}


def process_update(update: dict) -> None:
    global _last_command_ts

    message = update.get("message", {})
    text = message.get("text", "").strip().lower().split("@")[0]  # strip @botname suffix
    from_id = str(message.get("chat", {}).get("id", ""))

    # Only respond to the configured chat
    if from_id != str(CHAT_ID):
        logger.info(f"Ignoring message from chat {from_id}")
        return

    if text not in HANDLERS:
        return

    # Cooldown to prevent double-triggers
    now = time.time()
    if now - _last_command_ts < COMMAND_COOLDOWN:
        logger.info(f"Command '{text}' ignored — cooldown active")
        return

    _last_command_ts = now
    logger.info(f"Handling command: {text}")
    HANDLERS[text]()


def main() -> None:
    global _last_update_id

    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    logger.info("TAO bot listener started — polling for commands")
    send("🤖 TAO Monitor bot listener started. Send /help for commands.")

    while True:
        try:
            updates = get_updates(_last_update_id + 1)
            for update in updates:
                _last_update_id = max(_last_update_id, update.get("update_id", 0))
                process_update(update)
        except Exception as e:
            logger.error(f"Poll loop error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
