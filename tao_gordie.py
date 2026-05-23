#!/usr/bin/env python3
"""
TAO GORDIE — Subnet Portfolio Management Agent
Inspired by Siam Kidd's "The Gordon" (DSV Fund, March 2026).

Reference: "The Gordon — An Adventure into Agentic Portfolio Management"
- Strategy: Flow Momentum
- Siam runs 4 portfolios: Gordon, Ellie, Gordy Bigger, AD
- Rebalance: Every 30 min, 24/7
- Score Model: 100 pts per subnet
- Gas Reserve: τ0.20 always
- Typical holdings: 10-12 subnets per portfolio
- Typical scores in practice: 10-38 range

Our implementation (Simon's version):
- Phase 1: Monitoring & Alerts (manual rebalancing) ← CURRENT
- Phase 2: Scoring & Ranking Engine
- Phase 3: Automated Execution (future)

Data sources: Taostats API (dtao/pool endpoints), CoinGecko (TAO/fiat)
"""

import json
import logging
import math
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ─── Configuration ───────────────────────────────────────────────────────────

TAOSTATS_API_KEY = os.environ.get("TAOSTATS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
BASE_URL = "https://api.taostats.io/api"

# Directories
LOG_DIR = Path(os.environ.get("GORDIE_LOG_DIR", Path.home() / "tao_logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "gordie.log"
STATE_FILE = LOG_DIR / "gordie_state.json"
HISTORY_FILE = LOG_DIR / "gordie_history.json"

# Cycle interval
CYCLE_INTERVAL_SECONDS = int(os.environ.get("GORDIE_INTERVAL", 1800))  # 30 min

# Total subnets to scan (taostats shows ~140 active)
MAX_NETUID = 140

# ─── Current Holdings (Simon's portfolio) ────────────────────────────────────
# Updated manually or via env. Format: {netuid: {"name": str, "hotkey": str}}

CURRENT_HOLDINGS = {
    4:   {"name": "Targon",   "hotkey": ""},
    5:   {"name": "Hone",     "hotkey": ""},
    9:   {"name": "Iota",     "hotkey": ""},
    32:  {"name": "ItsAI",    "hotkey": ""},
    44:  {"name": "Score",    "hotkey": ""},
    55:  {"name": "NIOME",    "hotkey": ""},
    68:  {"name": "NOVA",     "hotkey": ""},
    75:  {"name": "Hippius",  "hotkey": ""},
    123: {"name": "MANTIS",   "hotkey": ""},
}

WATCHLIST = {
    3:  {"name": "Templar"},        # DSV conviction: decentralised pre-training
    2:  {"name": "D-Sperse"},       # DSV conviction: SSL for AI
    13: {"name": "Data Universe"},  # DSV conviction: decentralised data scraper
    18: {"name": "Zeus"},           # DSV conviction: weather forecasting
    33: {"name": "Ready AI"},       # DSV conviction: text data cleaning
    34: {"name": "Bitmind"},        # DSV conviction: deepfake detection
    46: {"name": "RESI"},           # DSV conviction: real estate valuations
    50: {"name": "Synth"},          # DSV conviction: price forecasting
    56: {"name": "Gradients"},      # DSV conviction: Auto ML
    85: {"name": "Vidaio"},         # DSV conviction: video compression
    93: {"name": "Bitcast"},        # DSV conviction: video generation / creator economy
    21: {"name": "AdTao"},          # PPC Rebel: Google Ads optimisation (watch pool depth)
}

# ─── DSV Fund Conviction Subnets (Siam's list — for reference/comparison) ────
# These are Siam's long-term holds. Some may fail Gordie's momentum filters
# but are held on fundamental conviction (two-bucket approach).
DSV_CONVICTION = {2, 3, 4, 13, 17, 18, 32, 33, 34, 44, 46, 50, 51, 56, 62, 64, 68, 75, 85, 93}

# ─── Permanent Blacklist (from Siam's Gordie) ────────────────────────────────
BLACKLIST = {17, 29, 43, 53, 89, 104, 112, 115}

# ─── Hard Pre-Filter Thresholds ──────────────────────────────────────────────

FILTERS = {
    "price_cap":          0.06,       # τ — above = overextended
    "price_floor":         0.0,        # τ — at or below = bad data
    "min_pool_depth":    2000.0,    # τ — below = too thin, slippage risk
    "max_pool_depth":      150000.0,   # τ — above = mature, limited upside
    "gini_cap":            0.85,       # Gini coefficient — above = whale risk
    "monthly_pump_cap":    500.0,      # % — above = likely manipulation
    "accel_sell_day":      -5.0,       # % — day AND week both below = accel sell
    "accel_sell_week":     -5.0,       # %
    "flat_month_floor":    3.0,        # % — month AND week both below = flat
    "flat_week_floor":     3.0,        # %
    "structural_decline": -25.0,      # % month — sustained downtrend
    "day_crash":           -20.0,      # % day — capitulation
}

# ─── Scoring Weights (Phase 2) ───────────────────────────────────────────────

SCORE_WEIGHTS = {
    "trend_strength":    0.30,  # How positive is the multi-timeframe momentum
    "gini_health":       0.15,  # Lower Gini = healthier distribution
    "pool_depth_sweet":  0.10,  # Goldilocks zone for pool depth
    "day_momentum":      0.20,  # 24h price change
    "week_momentum":     0.15,  # 7d price change
    "volume_signal":     0.10,  # Volume relative to market cap
}

TOP_N_DISPLAY = 10  # Show top N opportunities in Telegram

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("gordie")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def sf(v):
    """Safe float conversion."""
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def send_telegram(message: str, parse_mode: str = "HTML") -> bool:
    """Send a message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set — skipping send")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            log.error(f"Telegram send failed: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def load_state() -> dict:
    """Load persistent state from disk."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_run": None, "previous_scores": {}, "alert_history": []}


def save_state(state: dict):
    """Save persistent state to disk."""
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
    except Exception as e:
        log.error(f"Failed to save state: {e}")


def append_history(entry: dict):
    """Append a cycle result to the history log (keep last 48 entries = 24hrs)."""
    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except Exception:
            pass
    history.append(entry)
    history = history[-48:]  # Keep ~24h of 30-min cycles
    try:
        HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))
    except Exception as e:
        log.error(f"Failed to save history: {e}")


# ─── Data Fetching ───────────────────────────────────────────────────────────

def taostats_get(endpoint: str, params: dict = None) -> dict | None:
    """Make a GET request to the Taostats API."""
    url = f"{BASE_URL}/{endpoint}"
    headers = {"Authorization": TAOSTATS_API_KEY}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        elif r.status_code == 429:
            log.warning(f"Rate limited on {endpoint} — backing off")
            time.sleep(5)
            return None
        else:
            log.warning(f"API {endpoint} returned {r.status_code}")
            return None
    except requests.exceptions.Timeout:
        log.warning(f"Timeout on {endpoint}")
        return None
    except Exception as e:
        log.error(f"API error on {endpoint}: {e}")
        return None


def fetch_pool_data(netuid: int) -> dict | None:
    """Fetch pool/latest data for a single subnet.
    
    Returns dict with: price, total_tao, total_alpha, market_cap, liquidity,
    price_change_1_hour, price_change_1_day, price_change_1_week, price_change_1_month,
    tao_volume_24_hr, fear_and_greed_index, etc.
    """
    result = taostats_get("dtao/pool/latest/v1", {"netuid": netuid})
    if result and result.get("data"):
        data = result["data"]
        # API returns a list, take first entry
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        elif isinstance(data, dict):
            return data
    return None


def fetch_all_pools() -> dict:
    """Fetch pool data for all subnets.
    
    Tries batch endpoint first, falls back to per-subnet queries.
    Returns {netuid: pool_data_dict}.
    """
    all_pools = {}
    
    # Try fetching all at once (some API versions support no netuid = all)
    result = taostats_get("dtao/pool/latest/v1", {"limit": 256})
    if result and result.get("data"):
        data = result["data"]
        if isinstance(data, list) and len(data) > 1:
            for entry in data:
                netuid = entry.get("netuid")
                if netuid is not None:
                    all_pools[int(netuid)] = entry
            if len(all_pools) > 10:
                log.info(f"Batch fetch: got {len(all_pools)} subnets")
                return all_pools
    
    # Fallback: fetch per subnet (slower but reliable)
    log.info("Falling back to per-subnet fetch...")
    for netuid in range(0, MAX_NETUID + 1):
        if netuid in BLACKLIST:
            continue
        pool = fetch_pool_data(netuid)
        if pool:
            all_pools[netuid] = pool
        # Small delay to avoid rate limits
        if netuid % 20 == 0 and netuid > 0:
            time.sleep(1)
    
    log.info(f"Per-subnet fetch: got {len(all_pools)} subnets")
    return all_pools


def fetch_subnet_info() -> dict:
    """Fetch subnet metadata (immunity, registration date, etc.)."""
    result = taostats_get("subnet/latest/v1", {"limit": 256})
    if result and result.get("data"):
        info = {}
        for entry in result["data"]:
            netuid = entry.get("netuid")
            if netuid is not None:
                info[int(netuid)] = entry
        return info
    return {}


def fetch_holder_distribution(netuid: int) -> list | None:
    """Fetch holder/stake distribution for Gini calculation.
    
    Uses liquidity distributions or metagraph stake data.
    """
    # Try the dedicated holders/distribution endpoint
    result = taostats_get(f"dtao/liquidity/distribution/latest/v1", {"netuid": netuid})
    if result and result.get("data"):
        return result["data"]
    
    # Fallback: use metagraph stake data to compute concentration
    result = taostats_get("metagraph/latest/v1", {"netuid": netuid, "limit": 256})
    if result and result.get("data"):
        return result["data"]
    
    return None


def fetch_tao_price() -> tuple:
    """Fetch TAO/GBP and TAO/USD from CoinGecko."""
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "bittensor", "vs_currencies": "gbp,usd"}
    headers = {"User-Agent": "tao-gordie/1.0"}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    try:
        r = requests.get(url, params=params, headers=headers, timeout=15)
        data = r.json()
        return data["bittensor"]["gbp"], data["bittensor"]["usd"]
    except Exception as e:
        log.error(f"CoinGecko error: {e}")
        return None, None


# ─── Gini Coefficient Calculation ────────────────────────────────────────────

def calculate_gini(values: list) -> float:
    """Calculate Gini coefficient from a list of stake/balance values.
    
    Returns 0.0 (perfect equality) to 1.0 (perfect inequality).
    """
    if not values or len(values) < 2:
        return 0.0
    
    vals = sorted([v for v in values if v > 0])
    n = len(vals)
    if n < 2:
        return 0.0
    
    total = sum(vals)
    if total == 0:
        return 0.0
    
    # Gini formula: G = (2 * sum(i * x_i)) / (n * sum(x_i)) - (n + 1) / n
    cumsum = sum((i + 1) * v for i, v in enumerate(vals))
    gini = (2.0 * cumsum) / (n * total) - (n + 1) / n
    return max(0.0, min(1.0, gini))


def compute_gini_for_subnet(netuid: int) -> float | None:
    """Compute Gini coefficient for a subnet's stake distribution."""
    data = fetch_holder_distribution(netuid)
    if not data:
        return None
    
    stakes = []
    for entry in data:
        # Try different field names depending on endpoint
        stake = sf(entry.get("stake")) or sf(entry.get("total_stake")) or sf(entry.get("amount"))
        if stake and stake > 0:
            stakes.append(stake)
    
    if len(stakes) < 3:
        return None
    
    return calculate_gini(stakes)


# ─── Pre-Filter Engine ───────────────────────────────────────────────────────

def apply_prefilters(netuid: int, pool: dict, subnet_info: dict = None) -> tuple:
    """Apply all hard pre-filters to a subnet.
    
    Returns (passed: bool, rejection_reason: str | None, metrics: dict)
    """
    metrics = {
        "netuid": netuid,
        "name": pool.get("subnet_name") or pool.get("name") or f"SN{netuid}",
    }
    
    # Extract values with safe conversion
    price = sf(pool.get("price"))  # Already in TAO (e.g., 0.069)
    
    # Pool depth: API returns in rao (1 TAO = 1e9 rao)
    total_tao_raw = sf(pool.get("total_tao")) or sf(pool.get("tao_in_pool"))
    total_tao = total_tao_raw / 1e9 if total_tao_raw and total_tao_raw > 1e6 else total_tao_raw
    
    pct_day = sf(pool.get("price_change_1_day")) or sf(pool.get("price_change_24_hr"))
    pct_week = sf(pool.get("price_change_1_week")) or sf(pool.get("price_change_7_day"))
    pct_month = sf(pool.get("price_change_1_month")) or sf(pool.get("price_change_30_day"))
    
    # Market cap and volume also in rao
    market_cap_raw = sf(pool.get("market_cap"))
    market_cap = market_cap_raw / 1e9 if market_cap_raw and market_cap_raw > 1e6 else market_cap_raw
    
    volume_raw = sf(pool.get("tao_volume_24_hr")) or sf(pool.get("volume_24_hr"))
    volume_24h = volume_raw / 1e9 if volume_raw and volume_raw > 1e6 else volume_raw
    
    liquidity_raw = sf(pool.get("liquidity"))
    liquidity = liquidity_raw / 1e9 if liquidity_raw and liquidity_raw > 1e6 else liquidity_raw
    
    fear_greed = sf(pool.get("fear_and_greed_index"))
    
    # Gini — tao.app exposes this as a native column, so the API may return it
    gini = sf(pool.get("gini")) or sf(pool.get("gini_coefficient"))
    
    # Additional fields visible in tao.app column selector
    alpha_liq_raw = sf(pool.get("total_alpha")) or sf(pool.get("alpha_in_pool"))
    alpha_liq = alpha_liq_raw / 1e9 if alpha_liq_raw and alpha_liq_raw > 1e6 else alpha_liq_raw
    root_prop = sf(pool.get("root_prop")) or sf(pool.get("root_proportion"))
    
    # Buy/sell: tao_buy_volume_24_hr is TAO volume (rao), buys_24_hr is tx count
    buy_vol_raw = sf(pool.get("tao_buy_volume_24_hr"))
    sell_vol_raw = sf(pool.get("tao_sell_volume_24_hr"))
    buy_vol_1d = buy_vol_raw / 1e9 if buy_vol_raw and buy_vol_raw > 1e6 else buy_vol_raw
    sell_vol_1d = sell_vol_raw / 1e9 if sell_vol_raw and sell_vol_raw > 1e6 else sell_vol_raw
    buy_count = sf(pool.get("buys_24_hr"))
    sell_count = sf(pool.get("sells_24_hr"))
    buyers = sf(pool.get("buyers_24_hr"))
    sellers = sf(pool.get("sellers_24_hr"))
    
    users_owned = sf(pool.get("users_owned")) or sf(pool.get("holders"))
    pct_hour = sf(pool.get("price_change_1_hour"))
    
    # Store metrics for scoring
    metrics.update({
        "price": price,
        "pool_depth": total_tao,
        "pct_hour": pct_hour,
        "pct_day": pct_day,
        "pct_week": pct_week,
        "pct_month": pct_month,
        "market_cap": market_cap,
        "volume_24h": volume_24h,
        "liquidity": liquidity,
        "fear_greed": fear_greed,
        "gini": gini,
        "alpha_liq": alpha_liq,
        "root_prop": root_prop,
        "buy_vol_1d": buy_vol_1d,
        "sell_vol_1d": sell_vol_1d,
        "users_owned": users_owned,
    })
    
    # ── Blacklist check ──
    if netuid in BLACKLIST:
        return False, "BLACKLISTED", metrics
    
    # ── Skip root (SN0) — not an alpha subnet ──
    if netuid == 0:
        return False, "ROOT_SUBNET", metrics
    
    # ── Filter 1: Price floor ──
    if price is None or price <= FILTERS["price_floor"]:
        return False, f"PRICE_FLOOR (price={price})", metrics
    
    # ── Filter 2: Price cap ──
    if price > FILTERS["price_cap"]:
        return False, f"PRICE_CAP (price={price:.4f}τ > {FILTERS['price_cap']}τ)", metrics
    
    # ── Filter 3: Min pool depth ──
    if total_tao is not None and total_tao < FILTERS["min_pool_depth"]:
        return False, f"MIN_POOL ({total_tao:.0f}τ < {FILTERS['min_pool_depth']:.0f}τ)", metrics
    
    # ── Filter 4: Max pool depth ──
    if total_tao is not None and total_tao > FILTERS["max_pool_depth"]:
        return False, f"MAX_POOL ({total_tao:.0f}τ > {FILTERS['max_pool_depth']:.0f}τ)", metrics
    
    # ── Filter 5: Gini concentration ──
    # Use API-provided Gini first; if not available, it stays None (scored neutrally)
    if gini is not None and gini > FILTERS["gini_cap"]:
        return False, f"GINI ({gini:.2f} > {FILTERS['gini_cap']})", metrics
    
    # ── Filter 6: Monthly pump ──
    if pct_month is not None and pct_month > FILTERS["monthly_pump_cap"]:
        return False, f"MONTHLY_PUMP ({pct_month:.1f}% > {FILTERS['monthly_pump_cap']}%)", metrics
    
    # ── Filter 6: All-zero guard ──
    if pct_month == 0 and pct_week == 0 and pct_day == 0:
        return False, "ALL_ZERO_DATA", metrics
    
    # ── Filter 7: Accelerating sell-off ──
    if (pct_day is not None and pct_week is not None
            and pct_day < FILTERS["accel_sell_day"]
            and pct_week < FILTERS["accel_sell_week"]):
        return False, f"ACCEL_SELL (day={pct_day:.1f}%, week={pct_week:.1f}%)", metrics
    
#    # ── Filter 8: Flat momentum ──
#    if (pct_month is not None and pct_week is not None
#            and pct_month < FILTERS["flat_month_floor"]
#            and pct_week < FILTERS["flat_week_floor"]):
#        return False, f"FLAT_MOMENTUM (month={pct_month:.1f}%, week={pct_week:.1f}%)", metrics
    
#    # ── Filter 9: Dual downtrend ──
#    if (pct_month is not None and pct_week is not None
#            and pct_month < 0 and pct_week < 0):
        return False, f"DUAL_DOWNTREND (month={pct_month:.1f}%, week={pct_week:.1f}%)", metrics
#    
    # ── Filter 10: Structural decline ──
    if pct_month is not None and pct_month < FILTERS["structural_decline"]:
        return False, f"STRUCTURAL_DECLINE (month={pct_month:.1f}%)", metrics
    
    # ── Filter 11: Day crash ──
    if pct_day is not None and pct_day < FILTERS["day_crash"]:
        return False, f"DAY_CRASH (day={pct_day:.1f}%)", metrics
    
    # ── Filter 12: Immunity period ──
    if subnet_info:
        info = subnet_info.get(netuid, {})
        immunity = info.get("immunity_period") or info.get("immunity")
        is_active = info.get("is_active", True)
        # If subnet is explicitly marked as in immunity or not active, skip
#         if immunity and str(immunity).lower() not in ("0", "false", "none", "passed"):
#             return False, f"IMMUNITY_PERIOD", metrics
#         if not is_active:
#             return False, "INACTIVE", metrics
    
    # ── Filter 13: Zero emission ──
        emission = sf(info.get("emission")) or sf(info.get("emission_value")) or sf(info.get("tempo_emission"))
        if emission is not None and emission == 0:
            return False, "ZERO_EMISSION", metrics

    # ── All filters passed ──
    return True, None, metrics


# ─── Scoring Engine (Phase 2) ────────────────────────────────────────────────

def score_subnet(metrics: dict, gini: float | None = None) -> float:
    """Score a subnet that passed all pre-filters. Returns 0-100."""
    score = 0.0
    max_score = 0.0
    
    # Use API-provided Gini if available, otherwise use computed value
    effective_gini = metrics.get("gini") if metrics.get("gini") is not None else gini
    
    # ── Trend strength (multi-timeframe momentum) ──
    weight = SCORE_WEIGHTS["trend_strength"]
    max_score += weight * 100
    pct_day = metrics.get("pct_day")
    pct_week = metrics.get("pct_week")
    pct_month = metrics.get("pct_month")
    if pct_day is not None and pct_week is not None and pct_month is not None:
        # Normalize: cap at ±50% for scoring
        d = max(-50, min(50, pct_day))
        w = max(-50, min(50, pct_week))
        m = max(-50, min(50, pct_month))
        # Score: positive across all timeframes = strongest
        trend_raw = (d * 0.3 + w * 0.4 + m * 0.3)  # weighted avg
        trend_score = max(0, min(100, 50 + trend_raw))  # 0-100 scale
        score += weight * trend_score
    
    # ── Gini health (lower = better) ──
    weight = SCORE_WEIGHTS["gini_health"]
    max_score += weight * 100
    if effective_gini is not None:
        # 0.0 = perfect → 100 points, 0.85 = threshold → 0 points
        gini_score = max(0, (1 - effective_gini / 0.85)) * 100
        score += weight * gini_score
    else:
        # No Gini data — give neutral score (50)
        score += weight * 50
    
    # ── Pool depth sweet spot ──
    weight = SCORE_WEIGHTS["pool_depth_sweet"]
    max_score += weight * 100
    pool = metrics.get("pool_depth")
    if pool is not None:
        # Sweet spot: middle of the τ15k-τ150k range
        min_p, max_p = FILTERS["min_pool_depth"], FILTERS["max_pool_depth"]
        mid = (min_p + max_p) / 2
        # Distance from midpoint, normalized
        dist = abs(pool - mid) / (max_p - min_p)
        pool_score = max(0, (1 - dist * 2)) * 100
        score += weight * pool_score
    
    # ── Day momentum ──
    weight = SCORE_WEIGHTS["day_momentum"]
    max_score += weight * 100
    if pct_day is not None:
        day_score = max(0, min(100, 50 + pct_day * 5))  # +10% day = 100
        score += weight * day_score
    
    # ── Week momentum ──
    weight = SCORE_WEIGHTS["week_momentum"]
    max_score += weight * 100
    if pct_week is not None:
        week_score = max(0, min(100, 50 + pct_week * 2.5))  # +20% week = 100
        score += weight * week_score
    
    # ── Volume signal (with buy/sell pressure) ──
    weight = SCORE_WEIGHTS["volume_signal"]
    max_score += weight * 100
    vol = metrics.get("volume_24h")
    mcap = metrics.get("market_cap")
    buy_vol = metrics.get("buy_vol_1d")
    sell_vol = metrics.get("sell_vol_1d")
    
    if vol is not None and mcap is not None and mcap > 0:
        vol_ratio = vol / mcap
        # Good volume: 1-10% of mcap. Too high (>20%) might be wash trading
        if vol_ratio > 0.20:
            vol_score = 30  # Suspicious
        else:
            vol_score = min(100, vol_ratio * 1000)  # 10% = 100
        
        # Buy/sell pressure bonus: net buying = bonus, net selling = penalty
        if buy_vol is not None and sell_vol is not None and (buy_vol + sell_vol) > 0:
            buy_ratio = buy_vol / (buy_vol + sell_vol)  # 0.5 = neutral
            pressure_adj = (buy_ratio - 0.5) * 40  # ±20 point adjustment
            vol_score = max(0, min(100, vol_score + pressure_adj))
        
        score += weight * vol_score
    
    # Normalize to 0-100
    if max_score > 0:
        return round((score / max_score) * 100, 1)
    return 0.0


# ─── Holdings Audit ──────────────────────────────────────────────────────────

def audit_current_holdings(all_pools: dict, subnet_info: dict) -> list:
    """Check current holdings against pre-filters. Flag any that fail."""
    alerts = []
    
    for netuid, holding in CURRENT_HOLDINGS.items():
        if netuid == 0:
            # Root subnet — different rules, skip pre-filters
            continue
        
        pool = all_pools.get(netuid)
        if not pool:
            alerts.append({
                "type": "WARNING",
                "priority": "🟡",
                "netuid": netuid,
                "name": holding["name"],
                "reason": "NO_DATA — could not fetch pool data",
            })
            continue
        
        passed, reason, metrics = apply_prefilters(netuid, pool, subnet_info)
        if not passed:
            alerts.append({
                "type": "EXIT_WARNING",
                "priority": "🔴",
                "netuid": netuid,
                "name": holding["name"],
                "reason": reason,
                "metrics": metrics,
            })
    
    return alerts


def check_watchlist(passing_subnets: dict) -> list:
    """Check if any watchlist subnets are now passing filters."""
    signals = []
    for netuid, info in WATCHLIST.items():
        if netuid in passing_subnets:
            signals.append({
                "type": "WATCHLIST_PASSING",
                "priority": "👀",
                "netuid": netuid,
                "name": info["name"],
                "score": passing_subnets[netuid].get("score", 0),
            })
    return signals


# ─── Telegram Message Formatting ─────────────────────────────────────────────

def format_gordie_message(
    cycle_num: int,
    total_scanned: int,
    total_passed: int,
    holding_alerts: list,
    watchlist_signals: list,
    top_opportunities: list,
    holdings_status: list,
    tao_gbp: float | None,
    tao_usd: float | None,
) -> str:
    """Format the Gordie 30-min Telegram update."""
    
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    msg = f"📊 <b>GORDIE — Cycle #{cycle_num}</b>\n"
    msg += f"⏰ {now}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # TAO Price
    if tao_gbp and tao_usd:
        msg += f"💰 TAO: <b>${tao_usd:.2f}</b> / £{tao_gbp:.2f}\n\n"
    
    # Filter summary
    msg += f"🔍 Scanned: {total_scanned} | Passed filters: <b>{total_passed}</b>\n\n"
    
    # ── Holding alerts (most critical) ──
    if holding_alerts:
        msg += "⚠️ <b>HOLDING ALERTS:</b>\n"
        for a in holding_alerts:
            msg += f"{a['priority']} SN{a['netuid']} {a['name']} — {a['reason']}\n"
        msg += "\n"
    else:
        msg += "✅ All holdings passing pre-filters\n\n"
    
    # ── Current holdings status ──
    if holdings_status:
        msg += "📋 <b>HOLDINGS STATUS:</b>\n"
        for h in holdings_status:
            trend = ""
            if h.get("pct_day") is not None:
                d = h["pct_day"]
                arrow = "🟢" if d > 0 else "🔴" if d < 0 else "⚪"
                w_str = f" {h['pct_week']:+.1f}%w" if h.get("pct_week") is not None else ""
                trend = f" {arrow}{d:+.1f}%d{w_str}"
            price_str = f" {h['price']:.4f}τ" if h.get("price") else ""
            score_str = f" [{h['score']}]" if h.get("score") else ""
            gini_str = f" G:{h['gini']:.2f}" if h.get("gini") else ""
            msg += f"  SN{h['netuid']} {h['name']}{price_str}{trend}{gini_str}{score_str}\n"
        msg += "\n"
    
    # ── Watchlist signals ──
    if watchlist_signals:
        msg += "👀 <b>WATCHLIST:</b>\n"
        for w in watchlist_signals:
            msg += f"  SN{w['netuid']} {w['name']} — passing filters! Score: {w['score']}\n"
        msg += "\n"
    
    # ── Top opportunities ──
    if top_opportunities:
        msg += f"📈 <b>TOP {len(top_opportunities)} OPPORTUNITIES:</b>\n"
        for i, opp in enumerate(top_opportunities, 1):
            d = opp.get("pct_day", 0) or 0
            w = opp.get("pct_week", 0) or 0
            pool = opp.get("pool_depth", 0) or 0
            g = opp.get("gini")
            gini_str = f" G:{g:.2f}" if g else ""
            msg += (
                f"  {i}. SN{opp['netuid']} {opp['name']} "
                f"— <b>{opp['score']}</b> "
                f"| {d:+.1f}%d {w:+.1f}%w "
                f"| {pool:,.0f}τ{gini_str}\n"
            )
        msg += "\n"
    
    msg += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    next_time = datetime.now(timezone.utc).strftime("%H:%M")
    msg += f"Next cycle in {CYCLE_INTERVAL_SECONDS // 60} min"
    
    return msg


# ─── Main Cycle ──────────────────────────────────────────────────────────────

def run_cycle(state: dict) -> dict:
    """Execute one Gordie cycle: fetch → filter → score → alert."""
    
    cycle_num = state.get("cycle_num", 0) + 1
    state["cycle_num"] = cycle_num
    log.info(f"═══ Gordie Cycle #{cycle_num} starting ═══")
    
    # 1. Fetch TAO price
    tao_gbp, tao_usd = fetch_tao_price()
    if tao_gbp:
        log.info(f"TAO price: ${tao_usd:.2f} / £{tao_gbp:.2f}")
    
    # 2. Fetch all subnet pool data
    log.info("Fetching pool data for all subnets...")
    all_pools = fetch_all_pools()
    if not all_pools:
        log.error("No pool data returned — aborting cycle")
        send_telegram("🔴 <b>GORDIE ERROR</b>\nFailed to fetch subnet pool data. Check API key and connectivity.")
        return state
    
    # 3. Fetch subnet metadata (immunity, etc.)
    subnet_info = fetch_subnet_info()
    
    # 4. Apply pre-filters to all subnets
    log.info(f"Applying pre-filters to {len(all_pools)} subnets...")
    passed = {}
    rejected = {}
    
    for netuid, pool in all_pools.items():
        ok, reason, metrics = apply_prefilters(netuid, pool, subnet_info)
        if ok:
            passed[netuid] = metrics
        else:
            rejected[netuid] = {"reason": reason, "metrics": metrics}
    
    log.info(f"Pre-filter results: {len(passed)} passed, {len(rejected)} rejected")
    
    # 5. Score passing subnets
    log.info("Scoring passing subnets...")
    scored = []
    for netuid, metrics in passed.items():
        # Gini: prefer API-provided value (from pool data).
        # If not available in pool data, optionally compute from holder distribution.
        # Computing is expensive (1 API call per subnet), so only do it for top candidates.
        gini = metrics.get("gini")  # Already extracted from pool data in apply_prefilters
        
        s = score_subnet(metrics, gini)
        metrics["score"] = s
        scored.append(metrics)
    
    scored.sort(key=lambda x: x["score"], reverse=True)
    top_opportunities = scored[:TOP_N_DISPLAY]
    
    # 6. Audit current holdings
    holding_alerts = audit_current_holdings(all_pools, subnet_info)
    
    # 7. Build holdings status
    holdings_status = []
    for netuid, holding in CURRENT_HOLDINGS.items():
        pool = all_pools.get(netuid)
        entry = {
            "netuid": netuid,
            "name": holding["name"],
            "pct_day": None,
            "pct_week": None,
            "price": None,
            "gini": None,
            "score": None,
        }
        if pool:
            entry["pct_day"] = sf(pool.get("price_change_1_day")) or sf(pool.get("price_change_24_hr"))
            entry["pct_week"] = sf(pool.get("price_change_1_week")) or sf(pool.get("price_change_7_day"))
            entry["price"] = sf(pool.get("price"))
            entry["gini"] = sf(pool.get("gini")) or sf(pool.get("gini_coefficient"))
        if netuid in passed:
            entry["score"] = passed[netuid].get("score")
        holdings_status.append(entry)
    
    # 8. Check watchlist
    watchlist_signals = check_watchlist(passed)
    
    # 9. Format and send Telegram message
    message = format_gordie_message(
        cycle_num=cycle_num,
        total_scanned=len(all_pools),
        total_passed=len(passed),
        holding_alerts=holding_alerts,
        watchlist_signals=watchlist_signals,
        top_opportunities=top_opportunities,
        holdings_status=holdings_status,
        tao_gbp=tao_gbp,
        tao_usd=tao_usd,
    )
    
    send_telegram(message)
    log.info("Telegram message sent")
    
    # 10. Update state
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["last_passed_count"] = len(passed)
    state["last_rejected_count"] = len(rejected)
    state["previous_scores"] = {m["netuid"]: m["score"] for m in scored}
    
    # 11. Log summary
    append_history({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cycle": cycle_num,
        "scanned": len(all_pools),
        "passed": len(passed),
        "rejected": len(rejected),
        "top_5": [{"sn": m["netuid"], "score": m["score"]} for m in scored[:5]],
        "holding_alerts": len(holding_alerts),
        "tao_usd": tao_usd,
    })
    
    log.info(f"═══ Cycle #{cycle_num} complete ═══")
    return state


# ─── Entry Points ────────────────────────────────────────────────────────────

def run_once():
    """Run a single Gordie cycle."""
    state = load_state()
    state = run_cycle(state)
    save_state(state)


def run_loop():
    """Run Gordie in continuous loop (30-min cycles)."""
    log.info(f"Gordie starting in loop mode (interval: {CYCLE_INTERVAL_SECONDS}s)")
    send_telegram(
        "🤖 <b>GORDIE ONLINE</b>\n"
        f"Monitoring {MAX_NETUID} subnets every {CYCLE_INTERVAL_SECONDS // 60} min\n"
        f"Holdings: {', '.join(f'SN{n}' for n in sorted(CURRENT_HOLDINGS.keys()))}\n"
        f"Watchlist: {', '.join(f'SN{n}' for n in sorted(WATCHLIST.keys()))}\n"
        f"Blacklist: {', '.join(str(n) for n in sorted(BLACKLIST))}"
    )
    
    state = load_state()
    
    while True:
        try:
            state = run_cycle(state)
            save_state(state)
        except Exception as e:
            log.error(f"Cycle error: {e}\n{traceback.format_exc()}")
            send_telegram(f"🔴 <b>GORDIE ERROR</b>\n<pre>{str(e)[:200]}</pre>")
        
        log.info(f"Sleeping {CYCLE_INTERVAL_SECONDS}s until next cycle...")
        time.sleep(CYCLE_INTERVAL_SECONDS)


def run_audit():
    """Run a one-off audit of current holdings against pre-filters.
    Useful for immediate portfolio review without full cycle.
    """
    log.info("Running holdings audit...")
    all_pools = fetch_all_pools()
    subnet_info = fetch_subnet_info()
    
    if not all_pools:
        print("ERROR: Could not fetch pool data")
        return
    
    alerts = audit_current_holdings(all_pools, subnet_info)
    
    print("\n" + "=" * 60)
    print("GORDIE — Holdings Audit")
    print("=" * 60)
    
    for netuid, holding in CURRENT_HOLDINGS.items():
        pool = all_pools.get(netuid)
        if not pool:
            print(f"\n  SN{netuid} {holding['name']}: ⚠️  NO DATA")
            continue
        
        passed, reason, metrics = apply_prefilters(netuid, pool, subnet_info)
        status = "✅ PASS" if passed else f"🔴 FAIL: {reason}"
        
        price = metrics.get("price")
        depth = metrics.get("pool_depth")
        d = metrics.get("pct_day")
        w = metrics.get("pct_week")
        m = metrics.get("pct_month")
        
        print(f"\n  SN{netuid} {holding['name']}: {status}")
        if price: print(f"    Price: {price:.4f}τ")
        if depth: print(f"    Pool:  {depth:,.0f}τ")
        if d is not None: print(f"    24h:   {d:+.1f}%")
        if w is not None: print(f"    7d:    {w:+.1f}%")
        if m is not None: print(f"    30d:   {m:+.1f}%")
    
    if alerts:
        print("\n" + "⚠️  " * 15)
        print("ACTION REQUIRED:")
        for a in alerts:
            print(f"  {a['priority']} SN{a['netuid']} {a['name']}: {a['reason']}")
    else:
        print("\n✅ All holdings passing pre-filters")
    
    print("=" * 60)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    
    if mode == "once":
        run_once()
    elif mode == "loop":
        run_loop()
    elif mode == "audit":
        run_audit()
    elif mode == "test":
        print("Sending Gordie test message...")
        ok = send_telegram(
            "🤖 <b>GORDIE TEST</b>\n\n"
            "✅ Bot connected successfully\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"📡 Holdings: {len(CURRENT_HOLDINGS)} subnets\n"
            f"🚫 Blacklist: {len(BLACKLIST)} subnets\n"
            f"👀 Watchlist: {len(WATCHLIST)} subnets"
        )
        print("✅ Sent!" if ok else "❌ FAILED — check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars")
    else:
        print("Usage: python tao_gordie.py [once|loop|audit|test]")
        print("  once  — Run a single cycle (default)")
        print("  loop  — Run continuously every 30 min")
        print("  audit — Audit current holdings only")
        print("  test  — Send a test Telegram message")
