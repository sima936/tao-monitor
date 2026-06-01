"""
TAO Monitor — Scoring Runner
==============================
The main entry point for the 30-minute cron cycle.

Connects: taostats_fetch.py → subnet_scoring_engine.py → Telegram

Alert modes (reduces Telegram noise):
  - IMMEDIATE: new 🔴 critical alert on a holding → always sends
  - DIGEST:    full update every DIGEST_INTERVAL_HOURS (default 4h)
  - SILENT:    no change, no digest due → logs only, no Telegram

State is persisted to STATE_FILE so changes are detected across runs.

Usage:
    python run_scoring.py --api-key "tao-xxxxx:yyyyyy"

    # With Telegram
    export TAOSTATS_API_KEY="..."
    export TELEGRAM_BOT_TOKEN="..."
    export TELEGRAM_CHAT_ID="..."
    python run_scoring.py

    # Force send regardless of change detection
    python run_scoring.py --force-send

    # JSON output for dashboard API
    python run_scoring.py --json

Environment variables:
    TAOSTATS_API_KEY    - Required
    TELEGRAM_BOT_TOKEN  - Optional
    TELEGRAM_CHAT_ID    - Optional
    DIGEST_HOURS        - Hours between digest sends (default: 4)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import requests

from taostats_fetch import TaostatsClient, fetch_all_subnet_metrics
from subnet_scoring_engine import (
    run_scoring_cycle,
    format_telegram_alert,
    to_json,
    TaoMacroState,
    MacroRegime,
)

logger = logging.getLogger("tao_scoring_runner")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

CURRENT_HOLDINGS = [0, 4, 51, 62, 64, 68, 75]
TOP_N = 5  # reduced from 10 — keeps alerts shorter

# Alert frequency control
DIGEST_INTERVAL_HOURS = int(os.environ.get("DIGEST_HOURS", 4))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/home/simar/tao-monitor/scoring_state.json"))


# ─────────────────────────────────────────────────────────────────────────────
# State persistence — change detection
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load previous cycle state. Returns empty dict if no state yet."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning(f"Could not save state: {e}")


def extract_state_snapshot(result, holdings: list[int]) -> dict:
    """Extract the parts of scoring result that matter for change detection."""
    holding_set = set(holdings)

    # Which holdings are currently failing filters
    failing = {
        f["subnet_id"]: f["reason"]
        for f in result.filtered_out
        if f["subnet_id"] in holding_set
    }

    # Alert flags on holdings that passed
    holding_alerts = {}
    for s in result.ranked_by_entry:
        if s.subnet_id in holding_set and s.alert_flags:
            holding_alerts[s.subnet_id] = sorted(s.alert_flags)

    # Top 5 subnet IDs (order matters)
    top5_ids = [s.subnet_id for s in result.ranked_by_entry[:5]]

    return {
        "failing_holdings": failing,
        "holding_alerts": holding_alerts,
        "top5_ids": top5_ids,
        "passed_count": result.passed_filters,
    }


def should_send_telegram(
    current_snapshot: dict,
    prev_state: dict,
    force: bool = False,
) -> tuple[bool, str]:
    """
    Decide whether to send a Telegram message.

    Returns (should_send: bool, reason: str)

    Rules:
    1. Force flag → always send
    2. New 🔴 critical alert on a holding → send immediately
    3. Holding recovered from filter failure → send
    4. Digest interval elapsed → send full digest
    5. Otherwise → skip
    """
    if force:
        return True, "forced"

    now_ts = time.time()
    last_digest_ts = prev_state.get("last_digest_ts", 0)
    prev_snapshot = prev_state.get("snapshot", {})

    # Rule 2: new critical alert on a holding
    prev_failing = set(prev_snapshot.get("failing_holdings", {}).keys())
    curr_failing = set(current_snapshot["failing_holdings"].keys())
    new_failures = curr_failing - prev_failing
    if new_failures:
        return True, f"new_failures:{new_failures}"

    # Rule 2b: new alert flags on holdings
    prev_halerts = prev_snapshot.get("holding_alerts", {})
    curr_halerts = current_snapshot["holding_alerts"]
    for sn_id, flags in curr_halerts.items():
        if flags != prev_halerts.get(sn_id, []):
            return True, f"new_alert_flags:SN{sn_id}"

    # Rule 3: holding recovered
    recovered = prev_failing - curr_failing
    if recovered:
        return True, f"recovered:{recovered}"

    # Rule 4: digest interval
    hours_since = (now_ts - last_digest_ts) / 3600
    if hours_since >= DIGEST_INTERVAL_HOURS:
        return True, f"digest_{DIGEST_INTERVAL_HOURS}h"

    return False, "no_change"


# ─────────────────────────────────────────────────────────────────────────────
# Telegram sender
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            logger.info("Telegram sent")
            return True
        logger.error(f"Telegram {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"Telegram failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Gini override from gini_fetch.py (Bittensor SDK — Infinity8 only)
# ─────────────────────────────────────────────────────────────────────────────

def load_gini_cache() -> dict[int, float]:
    """
    Load Gini scores written by gini_fetch.py.

    gini_fetch.py writes /home/simar/tao-monitor/gini_cache.json:
        { "4": 0.92, "51": 0.95, "62": 0.97, ... }

    Returns empty dict if cache missing or stale (>2h old).
    """
    cache_path = Path("/home/simar/tao-monitor/gini_cache.json")
    try:
        if not cache_path.exists():
            return {}
        age_hours = (time.time() - cache_path.stat().st_mtime) / 3600
        if age_hours > 2:
            logger.warning(f"Gini cache is {age_hours:.1f}h old — using placeholders")
            return {}
        data = json.loads(cache_path.read_text())
        # Keys may be strings
        return {int(k): float(v) for k, v in data.items()}
    except Exception as e:
        logger.warning(f"Could not load gini cache: {e}")
        return {}


def apply_gini_overrides(
    all_metrics: list,
    gini_cache: dict[int, float],
) -> list:
    """Overwrite genie_score on SubnetMetrics objects where we have real data."""
    if not gini_cache:
        return all_metrics
    overridden = 0
    for m in all_metrics:
        if m.subnet_id in gini_cache:
            m.genie_score = gini_cache[m.subnet_id]
            overridden += 1
    logger.info(f"Applied {overridden} real Gini scores from cache")
    return all_metrics


# ─────────────────────────────────────────────────────────────────────────────
# TAO macro regime (from tao_price_history.json written by a separate fetcher)
# ─────────────────────────────────────────────────────────────────────────────

def load_tao_macro_signal() -> dict | None:
    """
    Load TAO macro Markov signal.

    Expected file: /home/simar/tao-monitor/tao_macro.json
    Written by: a separate cron job running markov_regime.py --ticker TAO-USD --json
    
    Falls back gracefully if missing.
    """
    macro_path = Path("/home/simar/tao-monitor/tao_macro.json")
    try:
        if not macro_path.exists():
            return None
        age_hours = (time.time() - macro_path.stat().st_mtime) / 3600
        if age_hours > 6:
            logger.warning(f"TAO macro data is {age_hours:.1f}h old")
            return None
        return json.loads(macro_path.read_text())
    except Exception as e:
        logger.warning(f"Could not load TAO macro: {e}")
        return None


def macro_dict_to_state(macro: dict | None) -> TaoMacroState | None:
    """Convert tao_macro.json dict to TaoMacroState for run_scoring_cycle.

    Returns None if macro is None — scoring engine will then use UNKNOWN state.
    """
    if macro is None:
        return None
    reg = macro.get("current_regime", "Unknown")
    signal = float(macro.get("signal", 0.0))
    probs = macro.get("next_state_probabilities", {})
    bull_p = float(probs.get("bull", 0.33))
    bear_p = float(probs.get("bear", 0.33))

    if reg == "Bull":
        regime = MacroRegime.BULL
        mode = "🟢 BULL — Rotate actively. Buy pullbacks. Take profits into strength."
    elif reg == "Bear":
        regime = MacroRegime.BEAR
        mode = "🔴 BEAR — Capital preservation. Move to SN0. No new entries."
    elif reg in ("Sideways", "Unknown") and macro.get("unavailable_reason"):
        return None  # fetch_tao_macro wrote an unavailable state
    else:
        regime = MacroRegime.SIDEWAYS
        mode = "🟡 SIDEWAYS — Hold conviction. Avoid new entries. Trim weak."

    return TaoMacroState(regime=regime, signal=signal, bull_prob=bull_p,
                         bear_prob=bear_p, strategy_mode=mode, available=True)


def format_macro_header(macro: dict | None) -> str:
    """Format the macro regime line for Telegram."""
    if macro is None:
        return "🌍 MACRO: ⚠️ TAO regime unknown"

    signal = macro.get("signal", 0)
    regime = macro.get("current_regime", "Unknown")
    bull_p = macro.get("next_state_probabilities", {}).get("bull", 0.33)
    bear_p = macro.get("next_state_probabilities", {}).get("bear", 0.33)

    if regime == "Bull":
        emoji = "🟢"
        action = "Favourable — entries OK"
    elif regime == "Bear":
        emoji = "🔴"
        action = "Caution — reduce exposure"
    else:
        emoji = "🟡"
        action = "Neutral — selective entries only"

    return (
        f"🌍 MACRO: {emoji} TAO {regime} regime\n"
        f"Signal: {signal:+.3f} | Bull: {bull_p:.0%} Bear: {bear_p:.0%}\n"
        f"→ {action}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run(
    api_key: str,
    telegram_token: str | None = None,
    telegram_chat: str | None = None,
    output_json: bool = False,
    fetch_concentration: bool = True,
    holdings: list[int] | None = None,
    top_n: int = TOP_N,
    force_send: bool = False,
) -> dict:
    if holdings is None:
        holdings = CURRENT_HOLDINGS

    start_time = time.time()
    logger.info("TAO Monitor — Scoring Cycle Starting")

    # Load previous state for change detection
    prev_state = load_state()

    # Load Gini cache from SDK fetcher
    gini_cache = load_gini_cache()

    # Load TAO macro signal
    macro = load_tao_macro_signal()

    # Fetch subnet data
    client = TaostatsClient(api_key=api_key)
    try:
        all_metrics = fetch_all_subnet_metrics(
            client,
            fetch_concentration=fetch_concentration,
        )
    except Exception as e:
        logger.error(f"Data fetch failed: {e}")
        error_msg = f"🔴 TAO MONITOR — Fetch Error\n\n{e}"
        if telegram_token and telegram_chat:
            send_telegram(error_msg, telegram_token, telegram_chat)
        return {"error": str(e)}

    logger.info(f"Fetched {len(all_metrics)} subnet metrics")

    # Apply real Gini scores where available
    all_metrics = apply_gini_overrides(all_metrics, gini_cache)

    # Convert macro dict → TaoMacroState for scoring engine
    macro_state = macro_dict_to_state(macro)

    # Score — pass pre-computed macro so scoring engine doesn't recompute with empty data
    result = run_scoring_cycle(all_metrics, top_n=top_n, macro=macro_state)
    elapsed = time.time() - start_time
    logger.info(
        f"Scoring complete: {result.passed_filters} passed, "
        f"{result.failed_filters} filtered out ({elapsed:.1f}s)"
    )

    # JSON output path — no Telegram logic
    if output_json:
        print(to_json(result))
        return {"timestamp": result.timestamp, "passed": result.passed_filters}

    # Build message
    macro_header = format_macro_header(macro)
    msg = format_telegram_alert(result, current_holdings=holdings, macro_header=macro_header)
    print(msg)

    # Change detection — decide whether to send
    current_snapshot = extract_state_snapshot(result, holdings)
    should_send, reason = should_send_telegram(current_snapshot, prev_state, force=force_send)

    if should_send and telegram_token and telegram_chat:
        logger.info(f"Sending Telegram ({reason})")
        send_telegram(msg, telegram_token, telegram_chat)
        # Update last digest timestamp if this was a digest send
        prev_state["last_digest_ts"] = time.time()
    elif not should_send:
        logger.info(f"Skipping Telegram — {reason}")

    # Save updated state
    prev_state["snapshot"] = current_snapshot
    prev_state["last_run_ts"] = time.time()
    save_state(prev_state)

    # Critical alert logging
    critical_alerts = []
    for f in result.filtered_out:
        if f["subnet_id"] in holdings:
            critical_alerts.append(f"SN{f['subnet_id']} ({f['name']}): {f['reason']}")
    for s in result.ranked_by_entry:
        if s.subnet_id in holdings and "MARKOV_BEAR_REGIME" in s.alert_flags:
            critical_alerts.append(f"SN{s.subnet_id} ({s.name}): BEAR regime")

    if critical_alerts:
        logger.warning("CRITICAL ALERTS: " + " | ".join(critical_alerts))

    return {
        "timestamp": result.timestamp,
        "passed": result.passed_filters,
        "failed": result.failed_filters,
        "telegram_sent": should_send,
        "telegram_reason": reason,
        "critical_alerts": len(critical_alerts),
        "elapsed_seconds": round(elapsed, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="run_scoring")
    parser.add_argument("--api-key", default=os.environ.get("TAOSTATS_API_KEY"))
    parser.add_argument("--telegram-token", default=os.environ.get("TELEGRAM_BOT_TOKEN"))
    parser.add_argument("--telegram-chat", default=os.environ.get("TELEGRAM_CHAT_ID"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-concentration", action="store_true")
    parser.add_argument("--top-n", type=int, default=TOP_N)
    parser.add_argument("--holdings", type=str, default=None)
    parser.add_argument("--force-send", action="store_true",
                        help="Send Telegram regardless of change detection")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.api_key:
        print("ERROR: TAOSTATS_API_KEY required.", file=sys.stderr)
        sys.exit(1)

    holdings = CURRENT_HOLDINGS
    if args.holdings:
        holdings = [int(x.strip()) for x in args.holdings.split(",")]

    result = run(
        api_key=args.api_key,
        telegram_token=args.telegram_token,
        telegram_chat=args.telegram_chat,
        output_json=args.json,
        fetch_concentration=not args.no_concentration,
        holdings=holdings,
        top_n=args.top_n,
        force_send=args.force_send,
    )

    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
