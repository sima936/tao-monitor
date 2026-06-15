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
import csv
import json
import logging
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, timezone

import requests

from taostats_fetch import TaostatsClient, fetch_all_subnet_metrics, fetch_cost_basis
from tp_cl_stops import evaluate_stops, append_outcome_log, format_stop_alert, TRAIL_PCT, STOP_PCT
from subnet_scoring_engine import (
    run_scoring_cycle,
    format_telegram_alert,
    to_json,
    TaoMacroState,
    MacroRegime,
)
from subnet_allocation import (
    compute_target_allocation,
    AllocationPolicy,
    format_allocation_plan,
)
from geckoterminal_fetch import fetch_history_for_netuids

logger = logging.getLogger("tao_scoring_runner")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

# Last-resort fallback only. The cron now resolves holdings on-chain via a
# single get_wallet_stakes() call in main() (balances reused for P&L + alloc);
# this list is used only if that call fails.
# Updated Jun 9 to the real on-chain set (was stale: [0,4,51,62,64,68,75]).
CURRENT_HOLDINGS = [0, 4, 9, 44, 46, 55, 68, 107, 123]
TOP_N = 5  # reduced from 10 — keeps alerts shorter

# Alert frequency control
DIGEST_INTERVAL_HOURS = int(os.environ.get("DIGEST_HOURS", 4))
STATE_FILE = Path(os.environ.get("STATE_FILE", str(Path(__file__).parent / "scoring_state.json")))
# Per-cycle calibration log (consumed offline by score_calibration.py). Gitignored.
# Point SCORE_LOG_PATH at a Railway Volume so it accumulates across cron runs — a
# local path resets every ephemeral run/redeploy (see DEPLOY note in handoff).
SCORE_LOG_PATH = Path(os.environ.get("SCORE_LOG_PATH", str(Path(__file__).parent / "score_log.csv")))
# Persistent last-good Gini cache (volume-backed). Concentration drifts slowly,
# so when a live taostats fetch rate-limits a holding we reuse its last REAL
# value rather than a fake 0.5 (which also wrongly slips concentrated names past
# the genie pre-filter). Point GINI_CACHE_PATH at the same Volume as
# SCORE_LOG_PATH so it survives ephemeral cron containers.
GINI_CACHE_PATH = Path(os.environ.get("GINI_CACHE_PATH", str(Path(__file__).parent / "gini_cache.json")))
GINI_CACHE_MAX_AGE_H = float(os.environ.get("GINI_CACHE_MAX_AGE_H", 48))


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


def append_score_log(result, scored, path: Path = SCORE_LOG_PATH) -> None:
    """Append one snapshot row per real-data subnet for offline calibration.

    Schema is the contract score_calibration.py reads: ts, subnet_id, name,
    price, composite (= health_score, the exit/hold metric the allocator sizes
    off), plus every engine component as f_<param> — auto-derived from
    ParameterScores, so a future p11 flows through to the analyzer untouched.

    Snapshot only — NO lookahead. Forward returns are joined per-subnet in the
    analyzer. Only `scored` (real-data survivors) are logged; the ~100
    placeholder-history subnets have untrustworthy scores and are excluded for
    the same reason they're kept out of the book.
    """
    if not scored:
        return
    try:
        factor_keys = None
        rows = []
        for s in scored:
            params = asdict(s.params) if getattr(s, "params", None) else {}
            if factor_keys is None:
                factor_keys = sorted(params.keys())
            row = {
                "ts": result.timestamp,
                "subnet_id": s.subnet_id,
                "name": s.name,
                "price": s.token_price,
                "composite": round(float(s.health_score), 2),
                "entry_score": round(float(s.entry_score), 2),
                "markov_regime": s.markov_regime,
            }
            for k in factor_keys:
                row[f"f_{k}"] = round(float(params[k]), 2)
            rows.append(row)

        fieldnames = (
            ["ts", "subnet_id", "name", "price", "composite",
             "entry_score", "markov_regime"]
            + [f"f_{k}" for k in (factor_keys or [])]
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = (not path.exists()) or path.stat().st_size == 0
        with path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames)
            if new_file:
                w.writeheader()
            w.writerows(rows)
        logger.info(f"Score log: appended {len(rows)} rows → {path}")
    except Exception as e:
        logger.warning(f"Score log append failed (non-fatal): {e}")


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


def push_cost_basis_to_dashboard(cost_basis_json: str) -> None:
    """POST computed cost-basis JSON to serve.py's in-memory store.

    Derives the cost-basis ingest URL from DASHBOARD_INGEST_URL by swapping the
    path segment (ingest-score → ingest-cost-basis), reusing the same token.
    No-op unless DASHBOARD_INGEST_URL and SCORE_INGEST_TOKEN are both set.
    """
    score_url = os.environ.get('DASHBOARD_INGEST_URL', '').strip()
    token = os.environ.get('SCORE_INGEST_TOKEN', '').strip()
    if not score_url or not token:
        logger.info("Cost-basis ingest skipped (DASHBOARD_INGEST_URL / SCORE_INGEST_TOKEN unset)")
        return
    url = score_url.replace('ingest-score', 'ingest-cost-basis')
    try:
        resp = requests.post(
            url,
            data=cost_basis_json.encode('utf-8'),
            headers={'X-Ingest-Token': token, 'Content-Type': 'application/json'},
            timeout=15,
        )
        logger.info(f"Cost-basis ingest: HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Cost-basis ingest failed: {e}")


def parse_stake_balances(stakes: list[dict]) -> dict[int, float]:
    """netuid → balance in TAO from get_wallet_stakes() entries.

    balance_as_tao is an integer rao string → /1e9 (matches gordie.html's parse).
    One source of truth so holdings-resolution, the P&L gate, and the allocator
    all consume a SINGLE get_wallet_stakes fetch (LIVE_STATE #5 de-dup).
    """
    out: dict[int, float] = {}
    for entry in stakes or []:
        nid = entry.get("netuid", entry.get("subnet_id"))
        if nid is None:
            continue
        try:
            bal = float(entry.get("balance_as_tao"))
        except (TypeError, ValueError):
            continue
        out[int(nid)] = bal / 1e9
    return out


def compute_holdings_pnl(client, cost_basis: dict, holdings: list[int],
                         bal_by_netuid: dict[int, float] | None = None) -> dict | None:
    """Map each held netuid → realised+unrealised P&L fraction vs net-invested.

        pnl[netuid] = (current balance_as_tao − net_invested) / net_invested

    net_invested comes from the cost-basis dict (fetch_cost_basis). Current
    balances are reused from `bal_by_netuid` when supplied (no extra call —
    threaded from the cycle's single get_wallet_stakes); otherwise fetched here.
    House-money / zero-basis positions (net_invested <= 0, e.g. SN0 Root) are
    skipped — P&L% is undefined there. Returns None on a fetch failure so the
    Telegram formatter cleanly falls back to its EMA gate.
    """
    positions = (cost_basis or {}).get("positions", {})
    if not positions:
        return None
    if bal_by_netuid is None:
        try:
            bal_by_netuid = parse_stake_balances(client.get_wallet_stakes())
        except Exception as e:
            logger.warning(f"P&L gate: stake-balance fetch failed (non-fatal): {e}")
            return None

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    pnl: dict[int, float] = {}
    for h in holdings:
        pos = positions.get(str(h))
        if not pos:
            continue
        net_inv = _f(pos.get("tao_invested"))
        if net_inv is None or net_inv <= 0:   # house money / no basis → undefined
            continue
        bal = bal_by_netuid.get(h)
        if bal is None:
            continue
        pnl[h] = (bal - net_inv) / net_inv

    if pnl:
        logger.info("P&L gate: " + ", ".join(f"SN{k} {v*100:+.0f}%" for k, v in pnl.items()))
    return pnl or None


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
    """Load the persistent last-good Gini cache (real values from prior runs).

    Written by save_gini_cache() to GINI_CACHE_PATH. Concentration drifts
    slowly, so a value up to GINI_CACHE_MAX_AGE_H old is a far better stand-in
    than the 0.5 placeholder when a live fetch rate-limits — and it keeps the
    genie pre-filter honest (0.5 would wrongly pass a concentrated name).
    Returns {} if the cache is missing or older than the cap.
    """
    try:
        if not GINI_CACHE_PATH.exists():
            return {}
        age_hours = (time.time() - GINI_CACHE_PATH.stat().st_mtime) / 3600
        if age_hours > GINI_CACHE_MAX_AGE_H:
            logger.warning(
                f"Gini cache {age_hours:.1f}h old (> {GINI_CACHE_MAX_AGE_H:.0f}h cap) — ignoring"
            )
            return {}
        data = json.loads(GINI_CACHE_PATH.read_text())
        return {int(k): float(v) for k, v in data.items()}
    except Exception as e:
        logger.warning(f"Could not load gini cache: {e}")
        return {}


def save_gini_cache(scores: dict[int, float]) -> None:
    """Persist fresh real Gini values into the last-good store, merging with any
    existing file so names not refreshed this run keep their previous real value.
    Point GINI_CACHE_PATH at a Volume or this resets each ephemeral run."""
    if not scores:
        return
    try:
        existing: dict[int, float] = {}
        if GINI_CACHE_PATH.exists():
            try:
                existing = {int(k): float(v)
                            for k, v in json.loads(GINI_CACHE_PATH.read_text()).items()}
            except Exception:
                existing = {}
        merged = {**existing, **{int(k): float(v) for k, v in scores.items()}}
        GINI_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        GINI_CACHE_PATH.write_text(json.dumps(merged, indent=2))
        logger.info(f"Gini cache: saved {len(scores)} fresh → {GINI_CACHE_PATH} ({len(merged)} total)")
    except Exception as e:
        logger.warning(f"Could not save gini cache: {e}")


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

    Computes real Gini for the held subnets in-process via GiniFetcher
    (SDK → RPC → Taostats fallback). Bounded to holdings, so ~12.5s/subnet on
    the Taostats fallback (≈100s for 8 subnets) — fine on the 12h cron, but
    NEVER call this from /status (60s subprocess timeout). Values that come back
    are real only (0.5 placeholders dropped); whatever the live fetch can't reach
    is filled from the persistent last-good cache (load_gini_cache) by the caller.

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


def _drop_forming_bar(
    prices: list[float], stamps: list[str]
) -> tuple[list[float], list[str]]:
    """Drop the most recent bar iff it is today's still-forming UTC-day bar.

    GeckoTerminal (and taostats) include the current UTC day as a partial,
    continuously-updating candle. Using it as the endpoint for the 7d return,
    the EMA, and the Markov regime label makes the regime whipsaw intraday and
    especially across the 00:00 UTC roll (e.g. Zipcode read +23% at 23:06 and
    +2% an hour later at 00:11 with no real move — the window's right edge slid
    onto a near-empty new bar). Keeping only CLOSED days makes every run within
    a UTC day read an identical series; the reading steps once per day at the
    close, and cron timing stops affecting the regime.

    Conditional on date, not unconditional: if a run fires before today's bar
    exists yet, the last bar is already a closed prior day — keep it. Never
    empties the series (guarded to len > 1) and leaves unparseable stamps alone.
    """
    if not prices or not stamps or len(prices) != len(stamps) or len(prices) <= 1:
        return prices, stamps
    try:
        last_date = (
            datetime.fromisoformat(str(stamps[-1]).replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .date()
        )
    except Exception:
        return prices, stamps  # unparseable timestamp → leave untouched
    if last_date == datetime.now(timezone.utc).date():
        return prices[:-1], stamps[:-1]
    return prices, stamps


# ─────────────────────────────────────────────────────────────────────────────
# Candidate set for real-data enrichment (free-tier API budget)
# ─────────────────────────────────────────────────────────────────────────────

WATCHLIST = [3]            # SN3 Teutonic — always enriched even if not held
CAND_MIN_POOL = 50.0       # TAO — skip illiquid pools when picking candidates
CAND_MAX_PRICE = 0.10      # TAO — skip very expensive tokens when picking candidates


def _recent_7d_change(m) -> float:
    """Real 7d move from the (anchored) price series; -inf if unknown.

    The synthetic series is reconstructed from the real 1d/7d anchors, so its
    own 7d change == the real one — a legitimate ranking signal with no extra
    API call.
    """
    ph = getattr(m, "price_history", None) or []
    if len(ph) >= 8 and ph[-8]:
        return (ph[-1] - ph[-8]) / ph[-8]
    if len(ph) >= 2 and ph[0]:
        return (ph[-1] - ph[0]) / ph[0]
    return float("-inf")


def select_candidates(all_metrics, holdings, watchlist, budget: int) -> list[int]:
    """Bounded set of subnets worth spending real-data budget on.

    Always includes current holdings + watchlist (need real exit/health
    signal), then fills the remaining budget with the strongest 7d movers
    among adequately-liquid, sanely-priced pools. Uses only fields already in
    hand — no extra API calls. SN0 is skipped by the fetchers downstream.
    """
    forced: list[int] = []
    seen: set[int] = set()
    for nid in list(holdings) + list(watchlist):
        if nid == 0:           # SN0 Root: dust, always price-filtered, and skipped
            continue           # by the fetchers — don't spend an enrichment slot on it
        if nid not in seen:
            forced.append(nid)
            seen.add(nid)

    pool = [
        m for m in all_metrics
        if m.subnet_id not in seen
        and m.subnet_id != 0
        and m.pool_depth >= CAND_MIN_POOL
        and m.token_price <= CAND_MAX_PRICE
        and "deprecated" not in (m.name or "").lower()
    ]
    pool.sort(key=_recent_7d_change, reverse=True)

    remaining = max(0, budget - len(forced))
    return forced + [m.subnet_id for m in pool[:remaining]]


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
    candidate_budget: int = 0,
    cost_basis: bool = False,
    prefetched_balances: dict[int, float] | None = None,
) -> dict:
    if holdings is None:
        holdings = CURRENT_HOLDINGS

    start_time = time.time()
    logger.info("TAO Monitor — Scoring Cycle Starting")

    # Load previous state for change detection
    prev_state = load_state()

    # Gini: prefer the SDK-written disk cache (Infinity8 co-located runs); on
    # Railway that cache is unreachable, so we optionally fetch Gini in-process
    # for the enrichment target set below. Opt-in (cron only) — never on /status.
    gini_cache = load_gini_cache()

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
        # We return here BEFORE any save_state / dashboard push, so the last good
        # state and report stand untouched until the next cycle. The common cause
        # is a transient upstream blip (read/connect timeout, 5xx, rate-limit) —
        # so send a calm note, not a raw traceback that looks like a crash.
        msg = str(e).lower()
        transient = (
            isinstance(e, (requests.exceptions.Timeout,
                           requests.exceptions.ConnectionError))
            or "timed out" in msg or "timeout" in msg
            or "connection" in msg or "502" in msg or "503" in msg or "504" in msg
        )
        if transient:
            soft = (
                "🟡 TAO MONITOR — data source slow\n\n"
                "Skipped this cycle (upstream timeout). Holding last state — "
                "no changes made. Will retry next run."
            )
        else:
            soft = (
                "🟡 TAO MONITOR — cycle skipped\n\n"
                f"Data fetch error: {type(e).__name__}. Holding last state — "
                "no changes made. Will retry next run."
            )
        if telegram_token and telegram_chat:
            send_telegram(soft, telegram_token, telegram_chat)
        return {"error": str(e), "skipped": True}

    logger.info(f"Fetched {len(all_metrics)} subnet metrics")

    # Apply the disk Gini cache first (Infinity8 path), where available.
    all_metrics = apply_gini_overrides(all_metrics, gini_cache)

    # Bounded enrichment target set (free-tier API budget). candidate_budget == 0
    # keeps the original holdings-only behaviour; > 0 broadens to holdings +
    # watchlist + the strongest 7d movers among liquid, sanely-priced pools.
    if candidate_budget > 0:
        targets = select_candidates(all_metrics, holdings, WATCHLIST, candidate_budget)
        logger.info(f"Enrichment targets ({len(targets)}): {targets}")
    else:
        targets = list(holdings)

    # Real Gini for the target set, in-process. The persistent last-good cache
    # was already applied above, so any holding the live fetch can't reach
    # (taostats rate-limit) keeps its previous REAL value, never a fake 0.5.
    # Fresh values override on top and are written back to keep the cache current.
    # Opt-in, cron only — ~12.5s/subnet.
    if holdings_gini:
        fresh = fetch_holdings_gini(targets, api_key)   # real values only (0.5 dropped)
        if fresh:
            all_metrics = apply_gini_overrides(all_metrics, fresh)
            save_gini_cache(fresh)
        reused = [n for n in targets if n != 0 and n not in fresh and n in gini_cache]
        if reused:
            logger.info(
                f"Gini: {len(fresh)} fresh, {len(reused)} reused from cache "
                f"(≤{GINI_CACHE_MAX_AGE_H:.0f}h old): {reused}"
            )

    # Replace synthetic 9-bar history with REAL daily bars for the target set.
    # PRIMARY: GeckoTerminal daily OHLCV (free, no key, ~90 bars, TAO-denominated
    #   — values match taostats; fixes the synthetic-anchor regime instability
    #   that whipsawed holdings, e.g. Minos flipping Bull/Sideways on a flat price).
    # FALLBACK: taostats pool/history only for subnets GT didn't return (new pools).
    # Same {netuid: (closes, timestamps)} contract → drops into apply_history_overrides.
    if holdings_history:
        gt_hist = fetch_history_for_netuids(targets)
        missing = [n for n in targets if n != 0 and n not in gt_hist]
        ts_hist = fetch_holdings_history(client, missing) if missing else {}
        history = {**ts_hist, **gt_hist}  # GT wins on any overlap
        # Drop today's still-forming UTC-day bar so regime/EMA/7d use CLOSED
        # days only — kills the intraday / midnight-roll whipsaw.
        _trimmed = 0
        _clean = {}
        for _n, (_p, _t) in history.items():
            _p2, _t2 = _drop_forming_bar(_p, _t)
            if len(_p2) != len(_p):
                _trimmed += 1
            _clean[_n] = (_p2, _t2)
        history = _clean
        all_metrics = apply_history_overrides(all_metrics, history)
        logger.info(
            f"Closed-bar trim: dropped today's forming bar on {_trimmed}/{len(history)} series"
        )
        logger.info(
            f"Real history: {len(gt_hist)} from GeckoTerminal, "
            f"{len(ts_hist)} from taostats fallback ({len(missing)} not on GT)"
        )

    # Convert macro dict → TaoMacroState for scoring engine
    macro_state = macro_dict_to_state(macro)

    # Score — pass pre-computed macro so scoring engine doesn't recompute with empty data
    result = run_scoring_cycle(all_metrics, top_n=top_n, macro=macro_state)

    # Auto cost-basis (opt-in, cron only — pages stake-event history, ~12.5s/page).
    # Computed here on the always-slow cron and pushed to the dashboard, because
    # serve.py is single-threaded and must never block on a multi-page fetch.
    # Also yields the real P&L gate for the Telegram trim section.
    pnl_by_netuid = None
    cb = None
    bal_by_netuid = prefetched_balances        # threaded from main's on-chain holdings resolve
    if cost_basis:
        try:
            cb = fetch_cost_basis(api_key)
            push_cost_basis_to_dashboard(json.dumps(cb))
            if bal_by_netuid is None:           # only fetch if main didn't already (#5 de-dup)
                bal_by_netuid = parse_stake_balances(client.get_wallet_stakes())
            pnl_by_netuid = compute_holdings_pnl(client, cb, holdings, bal_by_netuid=bal_by_netuid)
        except Exception as e:
            logger.warning(f"Cost-basis computation failed (non-fatal): {e}")

    # ── Allocation layer ("be Siam"): score → size. Targets always compute;
    # Drift + per-name actions only when balances are known (cron / threaded). ─
    account_tao = sum(bal_by_netuid.values()) if bal_by_netuid else None
    current_weight_by_id = (
        {sid: b / account_tao for sid, b in bal_by_netuid.items()}
        if (bal_by_netuid and account_tao) else None
    )
    # Size only REAL-DATA subnets: the enrichment `targets` (holdings + watchlist
    # + movers that received real history/Gini) ∪ holdings. Excludes the ~100
    # placeholder-history subnets whose health scores aren't trustworthy — those
    # live on the Opportunities tab (data-maturity gated), not in the book.
    real_data_ids = set(targets) | set(holdings)
    eligible_scored = [s for s in result.ranked_by_health if s.subnet_id in real_data_ids]
    append_score_log(result, eligible_scored)   # per-cycle calibration snapshot (gitignored)

    # ── TP/CL stops — evaluate BEFORE allocation so a fired stop forces a
    #    same-cycle full exit, overriding the conviction floor + 18h gate.
    #    Advisory: 🚨 ping + outcome-log row, no auto-unstake. Cron path only. ──
    force_exit: dict[int, str] = {}
    if cost_basis:
        price_by_id  = {s.subnet_id: float(s.token_price) for s in eligible_scored if s.token_price}
        name_by_id   = {s.subnet_id: s.name for s in eligible_scored}
        regime_by_id = {s.subnet_id: s.markov_regime for s in eligible_scored}
        health_by_id = {s.subnet_id: float(s.health_score) for s in eligible_scored}
        cost_by_id   = {int(k): float(v.get("tao_invested", 0) or 0)
                        for k, v in ((cb or {}).get("positions", {}) or {}).items()}
        stop_events, peak_out, fired_out = evaluate_stops(
            holdings, price_by_id, (bal_by_netuid or {}), cost_by_id,
            pnl_by_netuid, regime_by_id, health_by_id, name_by_id,
            prev_state.get("peak_price") or {}, prev_state.get("stop_fired") or {},
            trail_pct=TRAIL_PCT, stop_pct=STOP_PCT, now_ts=time.time(),
        )
        prev_state["peak_price"] = {str(k): v for k, v in peak_out.items()}
        prev_state["stop_fired"] = {str(k): v for k, v in fired_out.items()}
        force_exit = {e["netuid"]: e["event_type"].lower() for e in stop_events}
        if stop_events:
            append_outcome_log(stop_events)
            logger.warning("STOPS FIRED: " + " | ".join(
                f"SN{e['netuid']} {e['event_type']}" for e in stop_events))
            if telegram_token and telegram_chat:
                send_telegram(format_stop_alert(stop_events), telegram_token, telegram_chat)

    cut_since_in = {int(k): v for k, v in (prev_state.get("cut_since") or {}).items()}
    plan = compute_target_allocation(
        eligible_scored,                        # real-data survivors, sized off health_score
        result.macro,
        account_tao=account_tao,
        current_weight_by_id=current_weight_by_id,
        cut_since=cut_since_in,                  # OPEN #6 — persistent confirmation streak
        now_ts=time.time(),
        force_exit=force_exit,                   # STEP 2 — stops override floor + gate
    )
    # Persist the updated confirmation streak for the next cron. JSON stringifies
    # int keys, so they're coerced back to int on load (cut_since_in above).
    # Only written through to disk on the Telegram path (save_state at end of
    # run); the read-only --json/dashboard path returns before save and so never
    # advances the streak clock — correct, it's display-only.
    prev_state["cut_since"] = {str(k): v for k, v in plan.cut_since.items()}
    logger.info(
        f"Allocation: deploy {plan.deployed_fraction:.0%} · "
        f"{len(plan.positions)} green · {len(plan.cut)} cut · SN0 {plan.sn0_target_weight:.0%}"
    )

    # Build the dashboard/JSON payload (v4 score + allocation block) and push it.
    try:
        _payload = json.loads(to_json(result))
        _payload["allocation"] = plan.to_dict()
        payload_json = json.dumps(_payload)
    except Exception as e:
        logger.warning(f"Allocation embed failed (non-fatal): {e}")
        payload_json = to_json(result)
    push_score_to_dashboard(payload_json)

    elapsed = time.time() - start_time
    logger.info(
        f"Scoring complete: {result.passed_filters} passed, "
        f"{result.failed_filters} filtered out ({elapsed:.1f}s)"
    )

    # JSON output path — no Telegram logic
    if output_json:
        print(payload_json)
        return {"timestamp": result.timestamp, "passed": result.passed_filters}

    # Build message
    macro_header = format_macro_header(macro)
    msg = format_telegram_alert(result, current_holdings=holdings, macro_header=macro_header,
                                pnl_by_netuid=pnl_by_netuid)
    if cost_basis:   # cron digest only — keep the 60s /status path lean (no alloc block)
        msg += "\n\n" + format_allocation_plan(plan, account_tao=account_tao)
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
    parser.add_argument("--candidates", type=int, default=0, metavar="N",
                        help="Enrich N subnets (holdings + watchlist + top 7d movers) "
                             "with real history/Gini instead of holdings only. "
                             "0 = holdings only (default). Free-tier safe: ~15-25; "
                             "each adds ~12.5s. Combine with --holdings-history/-gini.")
    parser.add_argument("--cost-basis", action="store_true",
                        help="Compute per-subnet cost basis from on-chain stake "
                             "events and push to the dashboard (cron only — pages "
                             "delegation history, ~12.5s/page).")
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

    prefetched_balances = None
    wallet_read_failed = False
    if args.holdings:
        holdings = [int(x.strip()) for x in args.holdings.split(",")]
    else:
        # No explicit holdings (bare cron) — resolve on-chain. The wallet read is
        # the SOURCE OF TRUTH for which subnets we hold. If it fails OR returns
        # empty we must NOT compute the cycle on the stale CURRENT_HOLDINGS list:
        # that resurrects exited names (e.g. NIOME) into the report AND into the
        # stop/allocation logic. Treat a failed/empty read like the transient
        # data-fetch path — hold last state, skip the cycle, send a calm note.
        try:
            from taostats_fetch import TaostatsClient
            _c = TaostatsClient(api_key=args.api_key, rate_limit_delay=0.5)
            prefetched_balances = parse_stake_balances(_c.get_wallet_stakes())
        except Exception as e:
            logger.warning(f"On-chain holdings fetch failed ({e})")
            prefetched_balances = {}
        if prefetched_balances:
            holdings = sorted(prefetched_balances)
            logger.info(f"Wallet holdings from chain: {holdings} ({len(holdings)} subnets)")
        else:
            wallet_read_failed = True
            holdings = CURRENT_HOLDINGS  # unused — we skip below; kept to avoid unbound var

    if wallet_read_failed:
        logger.warning(
            "Wallet read empty/failed — holding last state, skipping cycle "
            "(no phantom-book compute on stale CURRENT_HOLDINGS)."
        )
        if args.telegram_token and args.telegram_chat:
            send_telegram(
                "\U0001f7e1 TAO MONITOR — wallet read unavailable\n\n"
                "Couldn't read your on-chain positions this cycle (taostats blip). "
                "Holding last state — no scoring, no stops, no changes. Will retry next run.",
                args.telegram_token, args.telegram_chat,
            )
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

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
        candidate_budget=args.candidates,
        cost_basis=args.cost_basis,
        prefetched_balances=prefetched_balances,
    )

    if "error" in result:
        logger.warning(f"Run finished with error: {result.get('error')}")

    # Force a clean, immediate process exit. The on-chain holdings fetchers
    # (--holdings-gini / --holdings-history) open a substrate websocket whose
    # non-daemon thread blocks normal interpreter termination, leaving the cron
    # container hung as "Running" long after the report is sent. A bare sys.exit()
    # won't kill that thread. All side effects (Telegram, /data state via
    # save_state, dashboard push) are already flushed by the time run() returns,
    # so an immediate os._exit is safe and resolves the hang regardless of which
    # thread is lingering. Exit 0 on both paths preserves the prior cron status.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
        


if __name__ == "__main__":
    main()
