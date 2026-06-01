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
    """Show current TAO macro regime — computed live via yfinance."""
    send("⏳ Fetching TAO macro regime...")
    try:
        result = subprocess.run(
            [sys.executable, "-c",
             """
import sys, json
sys.path.insert(0, '.')
from markov_regime import analyze, fetch_ticker
try:
    close = fetch_ticker('TAO22974-USD', years=1)
    if len(close) < 30:
        close = fetch_ticker('TAO-USD', years=1)
    r = analyze(close, source='TAO', window=20, threshold=0.05, min_train=60, hmm=False)
    print(json.dumps({
        'regime': r['current_regime'],
        'signal': r['signal'],
        'bull': r['next_state_probabilities']['bull'],
        'bear': r['next_state_probabilities']['bear'],
    }))
except Exception as e:
    print(json.dumps({'error': str(e)}))
"""],
            capture_output=True, text=True, timeout=60,
            cwd=str(SCRIPT_DIR),
        )
        data = json.loads(result.stdout.strip())
        if "error" in data:
            send(f"⚠️ Macro fetch failed: {data['error']}")
            return
        regime = data["regime"]
        signal = data["signal"]
        bull_p = data["bull"]
        bear_p = data["bear"]
        emoji = "🟢" if regime == "Bull" else ("🔴" if regime == "Bear" else "🟡")
        msg = (
            f"🌍 TAO Macro Regime\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} <b>{regime}</b>\n"
            f"Signal: {signal:+.3f}\n"
            f"Bull: {bull_p:.0%}  Bear: {bear_p:.0%}"
        )
        send(msg)
    except Exception as e:
        send(f"🔴 Error computing macro: {e}")


def handle_holdings() -> None:
    """Show current holdings status — runs a fresh scoring cycle."""
    send("⏳ Checking holdings...")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "run_scoring.py"),
             "--no-concentration", "--json"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ},
        )
        if result.returncode != 0:
            send(f"🔴 Scoring failed:\n<pre>{result.stderr[-300:]}</pre>")
            return
        data = json.loads(result.stdout.strip())
        filtered = {str(f["subnet_id"]): f["reason"]
                    for f in data.get("filtered_out", [])
                    if f["subnet_id"] in [0, 4, 51, 62, 64, 68, 75]}
        passing  = [s for s in data.get("ranked", [])
                    if s["subnet_id"] in [0, 4, 51, 62, 64, 68, 75]]

        lines = ["📋 <b>Holdings Status</b>\n━━━━━━━━━━━━━━━━━━━━"]
        for s in passing:
            es = s.get("entry_score", s.get("composite_score", 0))
            hs = s.get("health_score", 0)
            lines.append(f"✅ SN{s['subnet_id']} ({s['name']}) — E:{es:.0f} H:{hs:.0f}")
        for sn_id, reason in filtered.items():
            lines.append(f"🔴 SN{sn_id} — {reason}")
        send("\n".join(lines))
    except Exception as e:
        send(f"🔴 Error checking holdings: {e}")


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
