"""
TAO Monitor — Scoring Runner
==============================
The main entry point for the 30-minute cron cycle.

Connects: taostats_fetch.py → subnet_scoring_engine.py → Telegram

Usage:
    # Test run (prints to stdout)
    python run_scoring.py --api-key "tao-xxxxx:yyyyyy"

    # With Telegram output
    python run_scoring.py --api-key "tao-xxxxx:yyyyyy" --telegram-token "BOT_TOKEN" --telegram-chat "CHAT_ID"

    # Using environment variables (recommended for Railway/cron)
    export TAOSTATS_API_KEY="tao-xxxxx:yyyyyy"
    export TELEGRAM_BOT_TOKEN="your_bot_token"
    export TELEGRAM_CHAT_ID="your_chat_id"
    python run_scoring.py

    # JSON output for dashboard API
    python run_scoring.py --json

Environment variables:
    TAOSTATS_API_KEY    - Required
    TELEGRAM_BOT_TOKEN  - Optional (for Telegram alerts)
    TELEGRAM_CHAT_ID    - Optional (for Telegram alerts)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

from taostats_fetch import TaostatsClient, fetch_all_subnet_metrics
from subnet_scoring_engine import (
    run_scoring_cycle,
    format_telegram_alert,
    to_json,
)
from price_cache import PriceCache, update_cache_from_metrics

logger = logging.getLogger("tao_scoring_runner")

PRICE_DB = Path.home() / "tao_monitor" / "price_history.db"

# Simon's current staked subnets
CURRENT_HOLDINGS = [0, 4, 51, 62, 64, 68, 75]

# How many top subnets to show in alerts
TOP_N = 10


# ─────────────────────────────────────────────────────────────────────────────
# Telegram sender
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(
    message: str,
    bot_token: str,
    chat_id: str,
    parse_mode: str = "HTML",
) -> bool:
    """Send a message via the Telegram Bot API.

    Returns True on success, False on failure.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            logger.info("Telegram message sent successfully")
            return True
        else:
            logger.error(f"Telegram API error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


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
) -> dict:
    """Run one complete scoring cycle.

    1. Fetch all subnet metrics from Taostats
    2. Run scoring engine (pre-filters + Markov + composite scoring)
    3. Format and send Telegram alert (if configured)
    4. Return the full scoring result as dict

    Returns the scoring result dict for further processing.
    """
    if holdings is None:
        holdings = CURRENT_HOLDINGS

    start_time = time.time()

    # Step 1: Fetch data
    logger.info("=" * 50)
    logger.info("TAO Monitor — Scoring Cycle Starting")
    logger.info("=" * 50)

    client = TaostatsClient(api_key=api_key)

    try:
        all_metrics = fetch_all_subnet_metrics(
            client,
            fetch_concentration=fetch_concentration,
            concentration_netuids=holdings,
        )
    except Exception as e:
        logger.error(f"Data fetch failed: {e}")
        error_msg = f"🔴 TAO MONITOR — Fetch Error\n\n{e}"
        if telegram_token and telegram_chat:
            send_telegram(error_msg, telegram_token, telegram_chat)
        return {"error": str(e)}

    logger.info(f"Fetched {len(all_metrics)} subnet metrics")

    # Step 1b: Update price cache and enrich metrics with history
    cache = PriceCache(PRICE_DB)
    update_cache_from_metrics(cache, all_metrics)
    cache.enrich_metrics(all_metrics)

    # Step 2: Run scoring
    result = run_scoring_cycle(all_metrics, top_n=top_n)

    elapsed = time.time() - start_time
    logger.info(
        f"Scoring complete: {result.passed_filters} passed, "
        f"{result.failed_filters} filtered out ({elapsed:.1f}s)"
    )

    # Step 3: Output
    if output_json:
        print(to_json(result))
    else:
        # Print Telegram-formatted message to stdout
        msg = format_telegram_alert(result, current_holdings=holdings)
        print(msg)

        # Send to Telegram if configured
        if telegram_token and telegram_chat:
            send_telegram(msg, telegram_token, telegram_chat)

    # Step 4: Check for critical alerts on holdings
    critical_alerts = []
    for f in result.filtered_out:
        if f["subnet_id"] in holdings:
            critical_alerts.append(
                f"🔴 CRITICAL: SN{f['subnet_id']} ({f['name']}) "
                f"FAILED pre-filter: {f['reason']}"
            )

    for s in result.ranked:
        if s.subnet_id in holdings and "MARKOV_BEAR_REGIME" in s.alert_flags:
            critical_alerts.append(
                f"⚠️ SN{s.subnet_id} ({s.name}) in BEAR regime "
                f"(signal: {s.markov_signal:+.3f})"
            )

    if critical_alerts:
        logger.warning("CRITICAL ALERTS ON HOLDINGS:")
        for alert in critical_alerts:
            logger.warning(f"  {alert}")

    return {
        "timestamp": result.timestamp,
        "passed": result.passed_filters,
        "failed": result.failed_filters,
        "top_subnet": result.ranked[0].name if result.ranked else "none",
        "critical_alerts": len(critical_alerts),
        "elapsed_seconds": round(elapsed, 1),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="run_scoring",
        description="TAO Monitor — run one scoring cycle",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("TAOSTATS_API_KEY"),
        help="Taostats API key (or set TAOSTATS_API_KEY env var)",
    )
    parser.add_argument(
        "--telegram-token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN"),
        help="Telegram bot token (or set TELEGRAM_BOT_TOKEN env var)",
    )
    parser.add_argument(
        "--telegram-chat",
        default=os.environ.get("TELEGRAM_CHAT_ID"),
        help="Telegram chat ID (or set TELEGRAM_CHAT_ID env var)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output full scoring result as JSON",
    )
    parser.add_argument(
        "--no-concentration", action="store_true",
        help="Skip metagraph fetch (faster, uses default Gini=0.5)",
    )
    parser.add_argument(
        "--top-n", type=int, default=TOP_N,
        help=f"Number of top subnets to show (default: {TOP_N})",
    )
    parser.add_argument(
        "--holdings", type=str, default=None,
        help="Comma-separated subnet IDs currently held (default: Simon's portfolio)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.api_key:
        print("ERROR: Taostats API key required.", file=sys.stderr)
        print("  Set TAOSTATS_API_KEY env var or pass --api-key", file=sys.stderr)
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
    )

    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
