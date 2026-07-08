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

from taostats_fetch import fetch_wallet_holdings

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
COMMAND_COOLDOWN = 30  # seconds — ignore repeated *same* command within this window

_last_command_ts: dict[str, float] = {}
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


def _dashboard_url() -> str | None:
    """Derive the dashboard score endpoint from DASHBOARD_URL, or fall back to
    DASHBOARD_INGEST_URL by swapping /api/ingest-score -> /api/score (the same
    URL scheme run_scoring.py uses to POST). Returns None if neither is set."""
    url = os.environ.get("DASHBOARD_URL", "").strip()
    if url:
        return url.rstrip("/") + "/api/score"
    ingest = os.environ.get("DASHBOARD_INGEST_URL", "").strip()
    if ingest and "ingest-score" in ingest:
        return ingest.replace("ingest-score", "score")
    return None


def _fetch_latest_score() -> dict | None:
    """GET the last-ingested cron payload from serve.py. Uses BasicAuth
    (DASHBOARD_USER/PASS). Returns None on any failure — caller surfaces it."""
    url = _dashboard_url()
    if not url:
        return None
    user = os.environ.get("DASHBOARD_USER", "tao")
    pwd = os.environ.get("DASHBOARD_PASS", "bittensor")
    try:
        r = requests.get(url, auth=(user, pwd), timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "awaiting_first_scan":
            return None
        return data
    except Exception as e:
        logger.warning(f"Dashboard fetch failed: {e}")
        return None


def _format_status_from_payload(data: dict) -> str:
    """Render the on-demand /status message from the cron payload cached on
    serve.py. Uses the same layout as the 6h actionable digest so figures are
    consistent across on-demand and scheduled views."""
    # Local imports so a missing module in a legacy container still starts the
    # bot for /macro and /holdings (which don't need these).
    from types import SimpleNamespace
    from subnet_allocation import format_actionable_digest
    from spot_price import get_tao_prices

    macro = data.get("macro") or {}
    alloc = data.get("allocation") or {}

    # Reconstruct just enough of the AllocationPlan surface that the formatter
    # touches. We don't need the full dataclass — SimpleNamespace with the
    # attributes the formatter reads is sufficient (regime/signal/positions/
    # cut/deployed_fraction/sn0_target_weight).
    positions = []
    for p in alloc.get("positions") or []:
        positions.append(SimpleNamespace(
            subnet_id=int(p.get("subnet_id")),
            name=p.get("name") or "",
            action=p.get("action") or "hold",
            current_weight=p.get("current_weight"),
            target_weight=p.get("target_weight") or 0.0,
            markov_regime=p.get("markov_regime") or "",
            reason=p.get("reason") or "",
            pending_exit=bool(p.get("pending_exit")),
            pending_entry=bool(p.get("pending_entry")),
        ))
    plan = SimpleNamespace(
        macro_regime=macro.get("regime") or "Unknown",
        macro_signal=float(macro.get("signal") or 0.0),
        deployed_fraction=float(alloc.get("deployed_fraction") or 0.0),
        sn0_target_weight=float(alloc.get("sn0_target_weight") or 0.0),
        positions=positions,
        cut=alloc.get("cut") or [],
    )

    # Fundamentals for the ⚠️ Fund line — read the same file the cron reads.
    fundamentals = {}
    try:
        fp = SCRIPT_DIR / "fundamentals.json"
        if fp.exists():
            fundamentals = (json.loads(fp.read_text()) or {}).get("subnets", {}) or {}
    except Exception:
        pass

    prices = get_tao_prices() or {}
    # ts: cron timestamp is ISO; render HH:MM in the user's local zone if we can.
    ts_iso = data.get("timestamp") or ""
    ts_hhmm = "—"
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        ts_hhmm = dt.astimezone(ZoneInfo("Europe/London")).strftime("%H:%M")
    except Exception:
        ts_hhmm = ts_iso[11:16] if len(ts_iso) >= 16 else "—"

    account_total_tao = data.get("account_total_tao")
    free_tao = data.get("free_tao")
    # Root-stake TAO — added to the payload by run_scoring so /status can
    # render the same root/alpha/free split as the 6h cron digest. Older
    # payloads (pre-fix) won't have it → the tail gracefully degrades.
    root_tao = data.get("root_tao")

    return format_actionable_digest(
        plan,
        free_tao=free_tao,
        account_tao=account_total_tao,
        ts=ts_hhmm,
        fundamentals=fundamentals,
        root_tao=root_tao,
        tao_usd=prices.get("usd"),
        tao_gbp=prices.get("gbp"),
    )


def handle_status() -> None:
    """Render the latest cron snapshot from serve.py — same data path as the
    dashboard. Replaces the old subprocess-run-scoring approach, which failed
    because the listener container has no persistent snapshot history."""
    data = _fetch_latest_score()
    if not data:
        send(
            "⚠️ /status unavailable — dashboard hasn't ingested a scoring run yet, "
            "or DASHBOARD_URL/DASHBOARD_INGEST_URL is unset. Next cron will populate it."
        )
        return
    try:
        send(_format_status_from_payload(data))
    except Exception as e:
        logger.exception("Status format failed")
        send(f"🔴 Status format failed: {e}")


def handle_macro() -> None:
    """Show current TAO macro regime — reads from tao_macro.json written by fetch_tao_macro cron.
    Falls back to running fetch_tao_macro.py directly if file missing."""
    send("⏳ Fetching TAO macro regime...")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "fetch_tao_macro.py")],
            capture_output=True, text=True, timeout=60,
            env={**os.environ}, cwd=str(SCRIPT_DIR),
        )
        # fetch_tao_macro.py writes tao_macro.json then exits
        macro_path = SCRIPT_DIR / "tao_macro.json"
        if not macro_path.exists():
            send(f"⚠️ Macro fetch failed:\n<pre>{result.stdout[-300:]}</pre>")
            return
        data = json.loads(macro_path.read_text())
        if data.get("unavailable_reason"):
            send(f"⚠️ TAO macro unavailable: {data['unavailable_reason']}")
            return
        regime = data.get("current_regime", "Unknown")
        signal = float(data.get("signal", 0))
        probs  = data.get("next_state_probabilities", {})
        bull_p = float(probs.get("bull", 0.33))
        bear_p = float(probs.get("bear", 0.33))
        emoji  = "🟢" if regime == "Bull" else ("🔴" if regime == "Bear" else "🟡")
        send(
            f"🌍 TAO Macro Regime\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} <b>{regime}</b>\n"
            f"Signal: {signal:+.3f}\n"
            f"Bull: {bull_p:.0%}  Bear: {bear_p:.0%}"
        )
    except Exception as e:
        send(f"🔴 Error fetching macro: {e}")


def handle_holdings() -> None:
    """Show current holdings status — fetches real positions from chain."""
    send("⏳ Checking holdings...")
    try:
        # Fetch real holdings from chain
        holdings = fetch_wallet_holdings(API_KEY)
        if not holdings:
            send("⚠️ Could not fetch wallet holdings from chain")
            return

        sys.path.insert(0, str(SCRIPT_DIR))
        from taostats_fetch import TaostatsClient, fetch_all_subnet_metrics
        from subnet_scoring_engine import run_scoring_cycle

        client = TaostatsClient(api_key=API_KEY)
        all_metrics = fetch_all_subnet_metrics(client, fetch_concentration=False)
        scoring_result = run_scoring_cycle(all_metrics, top_n=5)

        lines = [f"📋 <b>Holdings Status</b> ({len(holdings)} positions)\n━━━━━━━━━━━━━━━━━━━━"]
        scored = {s.subnet_id: s for s in scoring_result.ranked_by_health}
        failed = {f["subnet_id"]: f["reason"] for f in scoring_result.filtered_out}

        for sn_id in holdings:
            if sn_id in failed:
                lines.append(f"🔴 SN{sn_id} — {failed[sn_id]}")
            elif sn_id in scored:
                s = scored[sn_id]
                chg = f" 24h:{s.pct_change_24h:+.0%}" if s.pct_change_24h is not None else ""
                lines.append(f"✅ SN{sn_id} ({s.name}) H:{s.health_score:.0f}{chg}")
            else:
                lines.append(f"✅ SN{sn_id} — passing")
        send("\n".join(lines))
    except Exception as e:
        send(f"🔴 Error checking holdings: {e}")


def handle_help() -> None:
    send(
        "🤖 <b>Tao Seeker Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "/status   — run scoring now and send full update\n"
        "/macro    — show current TAO macro regime\n"
        "/holdings — show holdings from chain with health scores\n"
        "/help     — this message\n\n"
        "All on-demand — no automated messages."
    )


HANDLERS = {
    "/status":   handle_status,
    "/macro":    handle_macro,
    "/holdings": handle_holdings,
    "/help":     handle_help,
}


def process_update(update: dict) -> None:
    message = update.get("message", {})
    text = message.get("text", "").strip().lower().split("@")[0]  # strip @botname suffix
    from_id = str(message.get("chat", {}).get("id", ""))

    # Only respond to the configured chat
    if from_id != str(CHAT_ID):
        logger.info(f"Ignoring message from chat {from_id}")
        return

    if text not in HANDLERS:
        return

    # Cooldown to prevent double-triggers — per command, so /macro then /holdings
    # back-to-back doesn't silently drop the second one.
    now = time.time()
    last = _last_command_ts.get(text, 0)
    if now - last < COMMAND_COOLDOWN:
        logger.info(f"Command '{text}' ignored — cooldown active")
        return

    _last_command_ts[text] = now
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
