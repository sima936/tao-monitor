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

# Last-resort fallback only. The cron now resolves holdings on-chain via
# fetch_wallet_holdings(); this list is used only if that call fails.
# Updated Jun 9 to the real on-chain set (was stale: [0,4,51,62,64,68,75]).
CURRENT_HOLDINGS = [0, 4, 9, 44, 46, 55, 68, 107, 123]
TOP_N = 5  # reduced from 10 — keeps alerts shorter

# Alert frequency control
DIGEST_INTERVAL_HOURS = int(os.environ.get("DIGEST_HOURS", 4))
STATE_FILE = Path(os.environ.get("STATE_FILE", str(Path(__file__).parent / "scoring_state.json")))


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

def push_score_to_dashboard(result_json: str) -> None:
    """POST the v4 scoring JSON to serve.py's in-memory store (Option 1 bridge).

    No-op unless DASHBOARD_INGEST_URL and SCORE_INGEST_TOKEN are both set, so it
    stays inert on the /status fast path and in local runs.
    """
    url = os.environ.get('DASHBOARD_INGEST_URL', '').strip()
    token = os.environ.get('SCORE_INGEST_TOKEN', '').strip()
    if not url or not token:
        logger.info("Dashboard ingest skipped (DASHBOARD_INGEST_URL / SCORE_INGEST_TOKEN unset)")
        return
    try:
        resp = requests.post(
            url,
            data=result_json.encode('utf-8'),
            headers={'X-Ingest-Token': token, 'Content-Type': 'application/json'},
            timeout=15,
        )
        logger.info(f"Dashboard ingest: HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Dashboard ingest failed: {e}")


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


def fetch_holdings_gini(holdings: list[int], api_key: str) -> dict[int, float]:
    """In-process Gini fetch for holdings only — Railway-friendly.

    The Infinity8 gini_cache.json path (load_gini_cache) is dead on Railway:
    the cron runs in an ephemeral container that can't see /home/simar.
    This computes real Gini for the held subnets in-process via GiniFetcher
    (SDK → RPC → Taostats fallback). Bounded to holdings, so ~12.5s/subnet on
    the Taostats fallback (≈100s for 8 subnets) — fine on the 12h cron, but
    NEVER call this from /status (60s subprocess timeout).

    SN0 (Root/Kraken) is skipped: it always fails the price filter and its
    metagraph is not a meaningful concentration signal.
    """
    targets = [h for h in holdings if h != 0]
    if not targets:
        return {}
    try:
        from gini_fetch import GiniFetcher
    except Exception as e:
        logger.warning(f"GiniFetcher import failed — keeping placeholders: {e}")
        return {}

    fetcher = GiniFetcher(taostats_api_key=api_key)
    logger.info(
        f"Fetching holdings Gini for {targets} via "
        f"{fetcher.active_source or 'auto'} (skipping SN0)..."
    )
    try:
        scores = fetcher.get_gini_batch(targets)
    except Exception as e:
        logger.warning(f"Holdings Gini batch failed — keeping placeholders: {e}")
        return {}

    # Drop placeholder (0.5) results so we don't overwrite with fake data and
    # so the real-vs-placeholder count downstream stays honest.
    real = {k: v for k, v in scores.items() if v != 0.5}
    dropped = len(scores) - len(real)
    if dropped:
        logger.warning(
            f"{dropped} holdings returned placeholder Gini "
            f"(source unavailable / endpoint shape changed) — left as placeholder"
        )
    return real


# ─────────────────────────────────────────────────────────────────────────────
# Real price history for holdings (replaces the 9-bar synthetic series)
# ─────────────────────────────────────────────────────────────────────────────

POOL_HISTORY_PATH = "/api/dtao/pool/history/v1"


def fetch_holdings_history(
    client, holdings: list[int], limit: int = 200
) -> dict[int, tuple[list[float], list[str]]]:
    """Fetch REAL daily price history for holdings via pool/history.

    pool/latest no longer returns seven_day_prices, so every subnet currently
    runs Markov/trend/momentum on a 9-bar SYNTHETIC series reconstructed from
    just the 24h/7d % anchors. This pulls real daily closes (frequency=by_day,
    oldest-first) so the held subnets get genuine regime/trend signal.

    Bounded to holdings (skip SN0/Root), ~12.5s/subnet — cron only, never on
    the 60s /status path. Returns {netuid: (prices, timestamps)} oldest-first;
    subnets with <9 real bars are omitted (synthetic is left in place).
    """
    out: dict[int, tuple[list[float], list[str]]] = {}
    for netuid in [h for h in holdings if h != 0]:
        try:
            resp = client.get(
                POOL_HISTORY_PATH,
                params={
                    "netuid": netuid,
                    "frequency": "by_day",
                    "limit": limit,
                    "order": "timestamp_asc",
                },
            )
            rows = resp.get("data", []) if isinstance(resp, dict) else []
        except Exception as e:
            logger.warning(f"History fetch failed for SN{netuid}: {e}")
            continue

        prices: list[float] = []
        ts: list[str] = []
        for row in rows:
            try:
                p = float(row.get("price"))
            except (TypeError, ValueError):
                continue
            if p > 0:
                prices.append(p)
                ts.append(row.get("timestamp", ""))

        if len(prices) >= 9:
            out[netuid] = (prices, ts)
            logger.info(f"SN{netuid}: {len(prices)} real daily bars")
        else:
            logger.info(f"SN{netuid}: only {len(prices)} real bars — keeping synthetic")
    return out


def apply_history_overrides(
    all_metrics: list, history: dict[int, tuple[list[float], list[str]]]
) -> list:
    """Swap synthetic price_history for real bars where we fetched them."""
    if not history:
        return all_metrics
    by_id = {m.subnet_id: m for m in all_metrics}
    applied = 0
    for netuid, (prices, ts) in history.items():
        m = by_id.get(netuid)
        if m is not None:
            m.price_history = prices
            m.timestamps = ts
            applied += 1
    logger.info(f"Applied real price history to {applied} holdings")
    return all_metrics


# ─────────────────────────────────────────────────────────────────────────────
# TAO macro regime (from tao_price_history.json written by a separate fetcher)
# ─────────────────────────────────────────────────────────────────────────────

def compute_tao_macro_inline(years: int = 1) -> dict | None:
    """Compute the TAO macro regime in-process — no external file dependency.

    Reproduces fetch_tao_macro.py's output as a dict, so the existing
    macro_dict_to_state() and format_macro_header() consumers are unchanged.
    Uses the engine's own TAO_WINDOW / TAO_THRESHOLD as the single source of
    truth for macro tuning. Returns None on ANY failure, so run() then falls
    back to the file, then to Unknown — never worse than current behaviour.
    """
    try:
        from markov_regime import analyze, fetch_ticker  # lazy
        from subnet_scoring_engine import TAO_WINDOW, TAO_THRESHOLD
    except Exception as e:
        logger.warning(f"Inline macro import failed: {e}")
        return None

    close = None
    for ticker in ("TAO22974-USD", "TAO-USD"):
        try:
            c = fetch_ticker(ticker, years=years)
            if c is not None and len(c) > 30:
                close = c
                break
        except Exception as e:
            logger.warning(f"Inline macro fetch {ticker} failed: {e}")
    if close is None or len(close) < 30:
        logger.warning("Inline macro: no TAO price data — falling back")
        return None

    try:
        r = analyze(close, source="TAO-inline",
                    window=TAO_WINDOW, threshold=TAO_THRESHOLD,
                    min_train=60, hmm=False)
        logger.info(f"Inline macro: {r['current_regime']} (signal {r['signal']:+.3f})")
        return r
    except Exception as e:
        logger.warning(f"Inline macro analyze failed: {e}")
        return None


def load_tao_macro_signal() -> dict | None:
    """
    Load TAO macro Markov signal.

    Expected file: /home/simar/tao-monitor/tao_macro.json
    Written by: a separate cron job running markov_regime.py --ticker TAO-USD --json
    
    Falls back gracefully if missing.
    """
    macro_path = Path(__file__).parent / "tao_macro.json"
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
    holdings_gini: bool = False,
    holdings_history: bool = False,
) -> dict:
    if holdings is None:
        holdings = CURRENT_HOLDINGS

    start_time = time.time()
    logger.info("TAO Monitor — Scoring Cycle Starting")

    # Load previous state for change detection
    prev_state = load_state()

    # Gini: prefer the SDK-written disk cache (Infinity8 co-located runs);
    # on Railway that cache is unreachable, so optionally fetch holdings Gini
    # in-process. holdings_gini is opt-in (cron only) — never on /status.
    gini_cache = load_gini_cache()
    if not gini_cache and holdings_gini:
        gini_cache = fetch_holdings_gini(holdings, api_key)

    # Load TAO macro signal — inline compute first, file fallback, then Unknown
    macro = compute_tao_macro_inline() or load_tao_macro_signal()

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

    # Replace synthetic 9-bar history with real daily bars for holdings (opt-in,
    # cron only — adds ~12.5s/holding; never on the 60s /status path).
    if holdings_history:
        history = fetch_holdings_history(client, holdings)
        all_metrics = apply_history_overrides(all_metrics, history)

    # Convert macro dict → TaoMacroState for scoring engine
    macro_state = macro_dict_to_state(macro)

    # Score — pass pre-computed macro so scoring engine doesn't recompute with empty data
    result = run_scoring_cycle(all_metrics, top_n=top_n, macro=macro_state)

    # Push the full v4 result to the dashboard's in-memory store (Option 1 bridge)
    push_score_to_dashboard(to_json(result))
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
    parser.add_argument("--holdings-gini", action="store_true",
                        help="Fetch real Gini for holdings in-process (cron only — "
                             "adds ~100s; do NOT use on the 60s /status path)")
    parser.add_argument("--holdings-history", action="store_true",
                        help="Fetch real daily price history for holdings (cron only — "
                             "adds ~100s; replaces synthetic bars; not on /status)")
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

    if args.holdings:
        holdings = [int(x.strip()) for x in args.holdings.split(",")]
    else:
        # No explicit holdings (bare cron) — resolve on-chain so the report
        # never drifts from /status. Fall back to the constant only on failure.
        try:
            from taostats_fetch import fetch_wallet_holdings
            holdings = fetch_wallet_holdings(args.api_key) or CURRENT_HOLDINGS
        except Exception as e:
            logger.warning(f"On-chain holdings fetch failed ({e}) — using fallback")
            holdings = CURRENT_HOLDINGS

    result = run(
        api_key=args.api_key,
        telegram_token=args.telegram_token,
        telegram_chat=args.telegram_chat,
        output_json=args.json,
        fetch_concentration=not args.no_concentration,
        holdings=holdings,
        top_n=args.top_n,
        force_send=args.force_send,
        holdings_gini=args.holdings_gini,
        holdings_history=args.holdings_history,
    )

    if "error" in result:
        sys.exit(0)
        


if __name__ == "__main__":
    main()
