"""
TAO Monitor — Data Fetch Layer
================================
Pulls real subnet metrics from the Taostats API and maps them
to SubnetMetrics objects for the scoring engine.

Primary endpoint: GET /api/dtao/pool/latest/v1
  - Returns price, liquidity, volume, 7-day price history, sentiment
  - One call per subnet, or omit netuid for all subnets

Secondary endpoint: GET /api/dtao/metagraph/latest/v1?netuid=N
  - Returns all neurons with stake amounts
  - Used to compute wallet concentration (Genie-equivalent)

Rate limit: 5 calls/min on free tier.
Strategy: fetch all pools in one call (no netuid param), then
selectively fetch metagraph for subnets that pass initial filters.

Usage:
    from taostats_fetch import TaostatsClient, fetch_all_subnet_metrics

    client = TaostatsClient(api_key="tao-xxxxx:yyyyyy")
    metrics = fetch_all_subnet_metrics(client)
    # metrics is a list of SubnetMetrics ready for run_scoring_cycle()

Dependencies: requests (add to requirements.txt)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

# Import the scoring engine's data structure
try:
    from subnet_scoring_engine import SubnetMetrics
except ImportError:
    # Fallback if running standalone — define minimal SubnetMetrics
    from dataclasses import field as _field

    @dataclass
    class SubnetMetrics:  # type: ignore[no-redef]
        subnet_id: int
        name: str
        token_price: float
        pool_depth: float
        genie_score: float
        price_history: list[float]
        timestamps: list[str]
        volume_24h: float = 0.0
        volume_7d: float = 0.0


logger = logging.getLogger("taostats_fetch")

# ─────────────────────────────────────────────────────────────────────────────
# API Client
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL = "https://api.taostats.io"

# Endpoints
POOL_LATEST = "/api/dtao/pool/latest/v1"
POOL_HISTORY = "/api/dtao/pool/history/v1"
METAGRAPH_LATEST = "/api/dtao/metagraph/latest/v1"
SUBNET_INFO = "/api/dtao/subnet/latest/v1"


class TaostatsClient:
    """Thin wrapper around the Taostats API with rate limiting."""

    def __init__(self, api_key: str, rate_limit_delay: float = 12.5):
        """
        api_key: Your taostats API key (format: tao-xxxxx:yyyyyy)
        rate_limit_delay: Seconds between calls (12.5s = ~5/min for free tier)
        """
        self.api_key = api_key
        self.rate_limit_delay = rate_limit_delay
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": api_key,
            "Accept": "application/json",
        })
        self._last_call_time = 0.0

    def _rate_limit(self):
        """Enforce minimum delay between API calls."""
        elapsed = time.time() - self._last_call_time
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)
        self._last_call_time = time.time()

    def get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """Make a GET request with rate limiting and error handling."""
        self._rate_limit()
        url = f"{BASE_URL}{endpoint}"

        try:
            resp = self.session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error {resp.status_code} for {url}: {e}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for {url}: {e}")
            raise

    def get_all_subnet_info(self) -> dict:
        """Fetch subnet info for ALL subnets in one call.

        Returns a dict of {netuid: name} for filling in unknown subnet names.
        """
        try:
            data = self.get(SUBNET_INFO)
            result = {}
            for subnet in data.get("data", []):
                netuid = int(subnet.get("netuid", -1))
                name = subnet.get("name") or subnet.get("subnet_name") or ""
                if netuid >= 0 and name:
                    result[netuid] = name
            logger.info(f"Fetched names for {len(result)} subnets")
            return result
        except Exception as e:
            logger.warning(f"Subnet info fetch failed: {e} — names will fall back to SN{{netuid}}")
            return {}

    def get_all_pools(self) -> list[dict]:
        """Fetch pool data for ALL subnets in one call."""
        data = self.get(POOL_LATEST)
        return data.get("data", [])

    def get_pool(self, netuid: int) -> Optional[dict]:
        """Fetch pool data for a single subnet."""
        data = self.get(POOL_LATEST, params={"netuid": netuid})
        pools = data.get("data", [])
        return pools[0] if pools else None

    def get_pool_history(self, netuid: int, limit: int = 200) -> list[dict]:
        """Fetch historical pool snapshots for a subnet.

        Used to build extended price history beyond the 7-day window.
        """
        data = self.get(POOL_HISTORY, params={"netuid": netuid, "limit": limit})
        return data.get("data", [])

    def get_metagraph(self, netuid: int) -> list[dict]:
        """Fetch metagraph (all neurons) for a subnet.

        Used to compute wallet concentration (Genie equivalent).
        """
        data = self.get(METAGRAPH_LATEST, params={"netuid": netuid})
        return data.get("data", [])


# ─────────────────────────────────────────────────────────────────────────────
# Wallet Concentration (Genie-equivalent)
# ─────────────────────────────────────────────────────────────────────────────

def compute_gini_coefficient(stakes: list[float]) -> float:
    """Compute Gini coefficient from a list of stake amounts.

    0.0 = perfectly equal distribution
    1.0 = one wallet holds everything

    This is the Genie-equivalent metric. Siam's threshold is 0.85.
    """
    if not stakes or len(stakes) < 2:
        return 0.0

    stakes = sorted(stakes)
    n = len(stakes)
    total = sum(stakes)

    if total == 0:
        return 0.0

    # Standard Gini formula
    cumulative = 0.0
    for i, s in enumerate(stakes):
        cumulative += (2 * (i + 1) - n - 1) * s

    return cumulative / (n * total)


def compute_top_holder_concentration(stakes: list[float], top_n: int = 10) -> float:
    """What % of total stake is held by the top N wallets.

    Alternative to Gini — more intuitive for Siam's framework.
    Returns 0-1 (0 = no concentration, 1 = top N hold everything).
    """
    if not stakes:
        return 0.0

    total = sum(stakes)
    if total == 0:
        return 0.0

    sorted_stakes = sorted(stakes, reverse=True)
    top_sum = sum(sorted_stakes[:top_n])

    return top_sum / total


def concentration_from_metagraph(metagraph_data: list[dict]) -> float:
    """Extract stake amounts from metagraph and compute Gini.

    The metagraph endpoint returns neurons with stake info.
    We aggregate by coldkey to get per-wallet totals.
    """
    # Aggregate stakes by coldkey (unique wallet)
    wallet_stakes: dict[str, float] = {}

    for neuron in metagraph_data:
        # The metagraph returns stake per hotkey — group by coldkey
        coldkey = neuron.get("coldkey", {})
        if isinstance(coldkey, dict):
            coldkey_addr = coldkey.get("ss58", "unknown")
        else:
            coldkey_addr = str(coldkey)

        # Stake might be in rao (divide by 1e9) or TAO — check the field
        stake = neuron.get("stake", 0)
        if isinstance(stake, str):
            stake = float(stake)

        # Convert from rao to TAO if the value is very large
        if stake > 1_000_000:
            stake = stake / 1e9

        wallet_stakes[coldkey_addr] = wallet_stakes.get(coldkey_addr, 0) + stake

    stakes = list(wallet_stakes.values())
    if not stakes:
        return 0.0

    return compute_gini_coefficient(stakes)


# ─────────────────────────────────────────────────────────────────────────────
# Pool data → SubnetMetrics mapping
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(val, default=0.0) -> float:
    """Safely convert API values to float."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def pool_to_metrics(
    pool: dict,
    genie_score: float = 0.5,  # default if metagraph not fetched yet
) -> SubnetMetrics:
    """Convert a taostats pool/latest response to a SubnetMetrics object.

    Known fields from /api/dtao/pool/latest/v1:
      - netuid: int
      - name: str (subnet name)
      - price: str (alpha price in TAO)
      - total_tao: str (TAO in pool = pool depth)
      - market_cap: str
      - tao_volume_24_hr: str
      - seven_day_prices: list[dict] with {price, timestamp} entries
      - price_change_1_hour, price_change_1_day, price_change_1_week: str
      - fear_and_greed_index: float
      - fear_and_greed_sentiment: str
      - liquidity: str
      - buys_24_hr, sells_24_hr: str
      - highest_price_24_hr, lowest_price_24_hr: str
    """
    netuid = int(pool.get("netuid", 0))
    name = pool.get("name", f"SN{netuid}")

    # Token price in TAO
    token_price = _safe_float(pool.get("price"))

    # Pool depth = TAO in the liquidity pool
    pool_depth = _safe_float(pool.get("total_tao"))
    # If total_tao is in rao, convert
    if pool_depth > 1_000_000:
        pool_depth = pool_depth / 1e9

    # Volume
    volume_24h = _safe_float(pool.get("tao_volume_24_hr"))
    if volume_24h > 1_000_000:
        volume_24h = volume_24h / 1e9

    # Price history from seven_day_prices
    seven_day = pool.get("seven_day_prices", [])
    price_history = []
    timestamps = []

    if isinstance(seven_day, list):
        for entry in seven_day:
            if isinstance(entry, dict):
                p = _safe_float(entry.get("price"))
                t = entry.get("timestamp", "")
                if p > 0:
                    price_history.append(p)
                    timestamps.append(t)
            elif isinstance(entry, (int, float, str)):
                # Some API versions return just a list of prices
                p = _safe_float(entry)
                if p > 0:
                    price_history.append(p)
                    timestamps.append("")

    # Ensure chronological order (oldest first)
    if timestamps and timestamps[0] > timestamps[-1]:
        price_history.reverse()
        timestamps.reverse()

    # Fallback: synthesise price history from pct change fields when
    # seven_day_prices is empty (Taostats returns [] for this field).
    # Reconstructs 4 price points: 7d ago, 24h ago, 1h ago, now.
    # Sufficient for pre-filter gate and trend/momentum scoring.
    if len(price_history) < 3 and token_price > 0:
        pct_1h = _safe_float(pool.get("price_change_1_hour"),  default=0.0) / 100.0
        pct_1d = _safe_float(pool.get("price_change_1_day"),   default=0.0) / 100.0
        pct_1w = _safe_float(pool.get("price_change_1_week"),  default=0.0) / 100.0

        # Work backwards from current price
        price_now = token_price
        price_1h  = price_now / (1 + pct_1h) if (1 + pct_1h) != 0 else price_now
        price_1d  = price_now / (1 + pct_1d) if (1 + pct_1d) != 0 else price_now
        price_1w  = price_now / (1 + pct_1w) if (1 + pct_1w) != 0 else price_now

        # Oldest first: 7d ago, 24h ago, 1h ago, now
        price_history = [price_1w, price_1d, price_1h, price_now]
        timestamps    = ["synthetic_7d", "synthetic_1d", "synthetic_1h", "synthetic_now"]
        logger.debug(f"SN{netuid} ({name}): synthesised 4 price points from pct changes")

    return SubnetMetrics(
        subnet_id=netuid,
        name=name,
        token_price=token_price,
        pool_depth=pool_depth,
        genie_score=genie_score,
        price_history=price_history,
        timestamps=timestamps,
        volume_24h=volume_24h,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Full fetch pipeline
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all_subnet_metrics(
    client: TaostatsClient,
    fetch_concentration: bool = True,
    concentration_netuids: Optional[list[int]] = None,
) -> list[SubnetMetrics]:
    """Fetch metrics for all subnets and return SubnetMetrics list.

    Step 1: Single API call to get all pool data (1 call, always)
    Step 2: Fetch metagraph for Simon's holdings only, every other run
            (7 calls every other run = ~6,480 calls/month on free tier)

    The run counter is stored in /tmp so metagraph is fetched on odd runs only.

    Args:
        client: TaostatsClient instance
        fetch_concentration: Whether to compute Genie scores from metagraph
        concentration_netuids: Override — fetch metagraph for these netuids only
    """
    import os

    # Simon's current holdings — metagraph fetched for these only
    HOLDINGS = [0, 4, 51, 62, 64, 68, 75]

    logger.info("Fetching all subnet pools...")
    pools = client.get_all_pools()
    logger.info(f"Got {len(pools)} subnet pools")

    # Fetch subnet name registry (1 API call, fills in Unknown names)
    subnet_names = client.get_all_subnet_info()

    # First pass: convert pools to metrics with default genie
    metrics_map: dict[int, SubnetMetrics] = {}
    for pool in pools:
        m = pool_to_metrics(pool, genie_score=0.5)
        # Fill in name from registry if pool returned empty
        if (not m.name or m.name == f"SN{m.subnet_id}") and m.subnet_id in subnet_names:
            m.name = subnet_names[m.subnet_id]
        metrics_map[m.subnet_id] = m

    # Second pass: fetch metagraph for holdings every other run
    if fetch_concentration:
        counter_file = "/app/metagraph_run_counter.txt"
        try:
            run_count = int(open(counter_file).read().strip()) if os.path.exists(counter_file) else 0
        except Exception:
            run_count = 0
        run_count += 1
        open(counter_file, "w").write(str(run_count))

        target_netuids = concentration_netuids or []

        if run_count % 4 == 1:  # every 4th run = every 2 hours
            target_netuids = target_netuids or HOLDINGS
            logger.info(f"Run {run_count}: fetching metagraph for holdings {target_netuids}")

            for netuid in target_netuids:
                try:
                    metagraph = client.get_metagraph(netuid)
                    genie = concentration_from_metagraph(metagraph)
                    metrics_map[netuid].genie_score = genie
                    logger.info(f"  SN{netuid}: Gini={genie:.3f}")
                except Exception as e:
                    logger.warning(f"  SN{netuid}: metagraph fetch failed: {e}")
        else:
            logger.info(f"Run {run_count}: skipping metagraph (every 4th run = every 2hrs)")

    return list(metrics_map.values())


def fetch_extended_history(
    client: TaostatsClient,
    netuid: int,
    limit: int = 200,
) -> tuple[list[float], list[str]]:
    """Fetch extended price history from pool/history endpoint.

    Returns (prices, timestamps) sorted oldest-first.
    Use this to build 72+ bar history for EMA calculation.

    Note: costs 1 API call per subnet. Use selectively.
    """
    history = client.get_pool_history(netuid, limit=limit)

    prices = []
    timestamps = []
    for entry in history:
        p = _safe_float(entry.get("price"))
        t = entry.get("timestamp", "")
        if p > 0:
            prices.append(p)
            timestamps.append(t)

    # Ensure chronological order
    if timestamps and len(timestamps) > 1 and timestamps[0] > timestamps[-1]:
        prices.reverse()
        timestamps.reverse()

    return prices, timestamps


# ─────────────────────────────────────────────────────────────────────────────
# Quick test / CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Test Taostats API fetch")
    parser.add_argument("--api-key", required=True, help="Taostats API key")
    parser.add_argument("--netuid", type=int, help="Fetch single subnet (default: all)")
    parser.add_argument("--concentration", action="store_true",
                        help="Also fetch metagraph for Gini calculation")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    client = TaostatsClient(api_key=args.api_key)

    if args.netuid:
        print(f"\nFetching SN{args.netuid}...")
        pool = client.get_pool(args.netuid)
        if pool:
            if args.json:
                print(json.dumps(pool, indent=2))
            else:
                m = pool_to_metrics(pool)
                print(f"  Name: {m.name}")
                print(f"  Price: {m.token_price:.6f} TAO")
                print(f"  Pool depth: {m.pool_depth:.2f} TAO")
                print(f"  Price history: {len(m.price_history)} bars")
                print(f"  Volume 24h: {m.volume_24h:.2f} TAO")

                if args.concentration:
                    print(f"\n  Fetching metagraph for concentration...")
                    metagraph = client.get_metagraph(args.netuid)
                    gini = concentration_from_metagraph(metagraph)
                    print(f"  Gini coefficient: {gini:.4f}")
                    print(f"  {'⚠️ ABOVE 0.85 THRESHOLD' if gini >= 0.85 else '✓ Below threshold'}")
        else:
            print(f"  No data returned for SN{args.netuid}")
    else:
        print("\nFetching all subnet pools...")
        pools = client.get_all_pools()
        print(f"Got {len(pools)} subnets\n")

        for pool in pools[:10]:  # Print first 10
            m = pool_to_metrics(pool)
            print(f"  SN{m.subnet_id:>3d} ({m.name:>20s}) | "
                  f"Price: {m.token_price:.6f} TAO | "
                  f"Pool: {m.pool_depth:>10.2f} TAO | "
                  f"History: {len(m.price_history)} bars")

        if len(pools) > 10:
            print(f"  ... and {len(pools) - 10} more")

        print(f"\nTo run with scoring: pipe into subnet_scoring_engine.py")
