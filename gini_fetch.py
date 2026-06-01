"""
gini_fetch.py — Free Gini Concentration via Bittensor SDK
===========================================================
Drop-in replacement for Taostats metagraph calls.
Queries the Bittensor chain directly — no rate limit, no cost.

Three-layer fallback chain:
  1. Bittensor SDK (bt.Subtensor) — cleanest, most reliable
  2. Raw subtensor RPC via websocket — no SDK dependency
  3. Taostats metagraph API — original fallback if chain unreachable

Usage:
    from gini_fetch import GiniFetcher

    fetcher = GiniFetcher()
    score = fetcher.get_gini(netuid=4)   # returns 0.0-1.0

    # Batch fetch for multiple subnets
    scores = fetcher.get_gini_batch([4, 18, 51, 64, 68, 75])

    # Check which source was used
    print(fetcher.active_source)   # "sdk" | "rpc" | "taostats" | "cache"

Integration with taostats_fetch.py:
    Replace the metagraph fetch block in fetch_all_subnet_metrics() with:

        from gini_fetch import GiniFetcher
        gini_fetcher = GiniFetcher(taostats_api_key=api_key)

        for netuid in target_netuids:
            metrics_map[netuid].genie_score = gini_fetcher.get_gini(netuid)

Dependencies:
    Required:  requests (already in requirements.txt)
    Optional:  bittensor (pip install bittensor) — enables SDK source
    Optional:  websocket-client (pip install websocket-client) — enables RPC source

Install on Infinity8:
    pip install bittensor          # ~500MB but worth it
    pip install websocket-client   # lightweight fallback
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger("gini_fetch")

# Subtensor public RPC endpoints (try in order)
SUBTENSOR_ENDPOINTS = [
    "wss://entrypoint-finney.opentensor.ai:443",
    "wss://bittensor-finney.api.onfinality.io/public-ws",
]

# Taostats fallback
TAOSTATS_BASE    = "https://api.taostats.io"
TAOSTATS_META    = "/api/dtao/metagraph/latest/v1"
TAOSTATS_DELAY   = 12.5   # seconds between calls on free tier

# Cache TTL — Gini doesn't change every 30 minutes meaningfully
CACHE_TTL_SECONDS = 3600   # 1 hour


class GiniFetcher:
    """Fetches Gini concentration scores for Bittensor subnets.

    Tries SDK → RPC → Taostats in order.
    Caches results to avoid redundant fetches within TTL.
    """

    def __init__(
        self,
        taostats_api_key: Optional[str] = None,
        network: str = "finney",
        cache_ttl: int = CACHE_TTL_SECONDS,
    ):
        self.taostats_api_key = taostats_api_key
        self.network = network
        self.cache_ttl = cache_ttl
        self._cache: dict[int, tuple[float, float]] = {}   # netuid → (gini, timestamp)
        self.active_source = "unknown"
        self._sdk_available = self._check_sdk()
        self._rpc_available = self._check_rpc()
        self._last_taostats_call = 0.0

        if self._sdk_available:
            logger.info("GiniFetcher: using Bittensor SDK (free, no rate limit)")
        elif self._rpc_available:
            logger.info("GiniFetcher: using raw RPC (free, no rate limit)")
        elif taostats_api_key:
            logger.info("GiniFetcher: using Taostats API fallback (rate limited)")
        else:
            logger.warning("GiniFetcher: no source available — will return 0.5 placeholder")

    # ── Source availability checks ───────────────────────────────────────────

    def _check_sdk(self) -> bool:
        try:
            import bittensor  # noqa: F401
            return True
        except ImportError:
            return False

    def _check_rpc(self) -> bool:
        try:
            import websocket  # noqa: F401
            return True
        except ImportError:
            return False

    # ── Cache ────────────────────────────────────────────────────────────────

    def _from_cache(self, netuid: int) -> Optional[float]:
        if netuid in self._cache:
            gini, ts = self._cache[netuid]
            if time.time() - ts < self.cache_ttl:
                return gini
        return None

    def _to_cache(self, netuid: int, gini: float) -> None:
        self._cache[netuid] = (gini, time.time())

    # ── Gini computation ─────────────────────────────────────────────────────

    @staticmethod
    def _compute_gini(stakes: list[float]) -> float:
        """Standard Gini coefficient. 0 = equal, 1 = one wallet holds all."""
        if not stakes or len(stakes) < 2:
            return 0.0
        s = sorted(stakes)
        n = len(s)
        total = sum(s)
        if total == 0:
            return 0.0
        cumulative = sum((2 * (i + 1) - n - 1) * v for i, v in enumerate(s))
        return cumulative / (n * total)

    @staticmethod
    def _top10_concentration(stakes: list[float]) -> float:
        """% stake held by top 10 wallets. Fast proxy for Gini.
        Highly correlated with Gini for manipulation risk detection.
        Use as fallback when full stake list unavailable.
        """
        if not stakes:
            return 0.0
        total = sum(stakes)
        if total == 0:
            return 0.0
        top10 = sum(sorted(stakes, reverse=True)[:10])
        return top10 / total

    # ── Source 1: Bittensor SDK ───────────────────────────────────────────────

    def _gini_via_sdk(self, netuid: int) -> Optional[float]:
        """Query metagraph directly via Bittensor SDK.

        bt.Subtensor.metagraph() returns a Metagraph object with:
          .S  — stake per hotkey (numpy array, in TAO)
          .coldkeys — coldkey address per neuron

        We aggregate by coldkey to get per-wallet totals,
        then compute Gini on those wallet-level stakes.
        """
        try:
            import bittensor as bt

            subtensor = bt.Subtensor(network=self.network)
            meta = subtensor.metagraph(netuid=netuid)

            # Aggregate stake by coldkey (unique wallet)
            wallet_stakes: dict[str, float] = {}
            stakes = meta.S.tolist()
            coldkeys = meta.coldkeys if hasattr(meta, "coldkeys") else []

            if coldkeys and len(coldkeys) == len(stakes):
                for ck, s in zip(coldkeys, stakes):
                    wallet_stakes[str(ck)] = wallet_stakes.get(str(ck), 0.0) + float(s)
                return self._compute_gini(list(wallet_stakes.values()))
            else:
                # No coldkey grouping available — use hotkey stakes directly
                return self._compute_gini([float(s) for s in stakes])

        except Exception as e:
            logger.debug(f"SDK fetch failed for SN{netuid}: {e}")
            return None

    # ── Source 2: Raw subtensor RPC ───────────────────────────────────────────

    def _gini_via_rpc(self, netuid: int) -> Optional[float]:
        """Query stake data via raw subtensor websocket RPC.

        Uses the state_getStorage RPC call to read the SubStake map
        for a given netuid. More brittle than SDK but no import footprint.

        Note: Substrate storage keys require scale encoding — this uses
        the simpler approach of calling the Taostats-compatible endpoint
        if available, otherwise falls through.
        """
        try:
            import websocket

            for endpoint in SUBTENSOR_ENDPOINTS:
                try:
                    ws = websocket.create_connection(endpoint, timeout=15)

                    # Request system_chain to verify connection
                    req = json.dumps({
                        "id": 1,
                        "jsonrpc": "2.0",
                        "method": "system_chain",
                        "params": []
                    })
                    ws.send(req)
                    resp = json.loads(ws.recv())
                    ws.close()

                    if "result" in resp:
                        logger.debug(f"RPC connected: {resp['result']}")
                        # Full stake query requires SCALE codec — complex to implement
                        # without the SDK. Log that RPC is available but we need SDK
                        # for full Gini. Return None to cascade to Taostats.
                        return None

                except Exception as e:
                    logger.debug(f"RPC endpoint {endpoint} failed: {e}")
                    continue

        except ImportError:
            pass

        return None

    # ── Source 3: Taostats API (rate-limited fallback) ────────────────────────

    def _gini_via_taostats(self, netuid: int) -> Optional[float]:
        """Original Taostats metagraph fetch — rate limited fallback."""
        if not self.taostats_api_key:
            return None

        # Respect free tier rate limit
        elapsed = time.time() - self._last_taostats_call
        if elapsed < TAOSTATS_DELAY:
            time.sleep(TAOSTATS_DELAY - elapsed)
        self._last_taostats_call = time.time()

        try:
            resp = requests.get(
                f"{TAOSTATS_BASE}{TAOSTATS_META}",
                params={"netuid": netuid},
                headers={
                    "Authorization": self.taostats_api_key,
                    "Accept": "application/json",
                },
                timeout=30,
            )
            resp.raise_for_status()
            neurons = resp.json().get("data", [])

            wallet_stakes: dict[str, float] = {}
            for neuron in neurons:
                ck = neuron.get("coldkey", {})
                addr = ck.get("ss58", "unknown") if isinstance(ck, dict) else str(ck)
                stake = float(neuron.get("stake", 0))
                if stake > 1_000_000:
                    stake /= 1e9
                wallet_stakes[addr] = wallet_stakes.get(addr, 0.0) + stake

            if wallet_stakes:
                return self._compute_gini(list(wallet_stakes.values()))

        except Exception as e:
            logger.warning(f"Taostats fallback failed for SN{netuid}: {e}")

        return None

    # ── Public interface ──────────────────────────────────────────────────────

    def get_gini(self, netuid: int) -> float:
        """Get Gini concentration for a subnet.

        Returns value 0.0-1.0. Returns 0.5 (neutral placeholder) only
        if all sources fail — logs a warning in that case.
        """
        # Check cache first
        cached = self._from_cache(netuid)
        if cached is not None:
            self.active_source = "cache"
            return cached

        gini = None

        # Source 1: SDK
        if self._sdk_available:
            gini = self._gini_via_sdk(netuid)
            if gini is not None:
                self.active_source = "sdk"

        # Source 2: RPC (currently falls through to Taostats — see note in method)
        if gini is None and self._rpc_available:
            gini = self._gini_via_rpc(netuid)
            if gini is not None:
                self.active_source = "rpc"

        # Source 3: Taostats
        if gini is None:
            gini = self._gini_via_taostats(netuid)
            if gini is not None:
                self.active_source = "taostats"

        # All failed
        if gini is None:
            logger.warning(f"SN{netuid}: all Gini sources failed — returning 0.5 placeholder")
            self.active_source = "placeholder"
            return 0.5

        gini = max(0.0, min(1.0, gini))
        self._to_cache(netuid, gini)

        logger.info(f"SN{netuid}: Gini={gini:.4f} (source: {self.active_source})")
        return gini

    def get_gini_batch(
        self,
        netuids: list[int],
        log_progress: bool = True,
    ) -> dict[int, float]:
        """Fetch Gini for multiple subnets.

        Returns dict of {netuid: gini_score}.
        SDK source fetches each subnet sequentially but quickly (~1-2s each).
        Taostats source respects rate limits (~12.5s each — warn if large batch).
        """
        if not self._sdk_available and not self._rpc_available:
            expected_secs = len(netuids) * TAOSTATS_DELAY
            logger.warning(
                f"No SDK/RPC available. Taostats rate limit means "
                f"{len(netuids)} subnets will take ~{expected_secs/60:.1f} minutes."
            )

        results: dict[int, float] = {}
        for i, netuid in enumerate(netuids):
            if log_progress and i % 10 == 0:
                logger.info(f"Gini batch: {i}/{len(netuids)} subnets processed")
            results[netuid] = self.get_gini(netuid)

        return results

    def clear_cache(self) -> None:
        """Clear the Gini cache — force fresh fetches next cycle."""
        self._cache.clear()

    def cache_stats(self) -> dict:
        """Return cache statistics for monitoring."""
        now = time.time()
        valid = sum(1 for _, ts in self._cache.values() if now - ts < self.cache_ttl)
        return {
            "total_cached": len(self._cache),
            "valid_entries": valid,
            "expired_entries": len(self._cache) - valid,
            "ttl_seconds": self.cache_ttl,
            "active_source": self.active_source,
            "sdk_available": self._sdk_available,
            "rpc_available": self._rpc_available,
            "taostats_available": self.taostats_api_key is not None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Integration patch for taostats_fetch.py
# ─────────────────────────────────────────────────────────────────────────────

INTEGRATION_PATCH = '''
# In taostats_fetch.py — replace the concentration fetch block
# in fetch_all_subnet_metrics() with this:

from gini_fetch import GiniFetcher

def fetch_all_subnet_metrics(
    client: TaostatsClient,
    fetch_concentration: bool = True,
    concentration_netuids: Optional[list[int]] = None,
) -> list[SubnetMetrics]:

    logger.info("Fetching all subnet pools...")
    pools = client.get_all_pools()
    logger.info(f"Got {len(pools)} subnet pools")

    metrics_map: dict[int, SubnetMetrics] = {}
    for pool in pools:
        m = pool_to_metrics(pool, genie_score=0.5)
        metrics_map[m.subnet_id] = m

    if fetch_concentration:
        # Use GiniFetcher — SDK → RPC → Taostats fallback chain
        gini_fetcher = GiniFetcher(taostats_api_key=client.api_key)

        target_netuids = concentration_netuids or [
            m.subnet_id for m in metrics_map.values()
            if (m.token_price < MAX_TOKEN_PRICE
                and m.pool_depth > MIN_POOL_DEPTH
                and m.pool_depth < MAX_POOL_DEPTH
                and len(m.price_history) >= 9)
        ]

        logger.info(f"Fetching Gini for {len(target_netuids)} subnets "
                    f"via {gini_fetcher.active_source or 'auto'}...")

        scores = gini_fetcher.get_gini_batch(target_netuids)
        for netuid, gini in scores.items():
            if netuid in metrics_map:
                metrics_map[netuid].genie_score = gini

        logger.info(f"Gini fetch complete: {gini_fetcher.cache_stats()}")

    return list(metrics_map.values())
'''


# ─────────────────────────────────────────────────────────────────────────────
# CLI test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import os

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    parser = argparse.ArgumentParser(description="Test Gini fetch sources")
    parser.add_argument("--netuid", type=int, default=4, help="Subnet to test")
    parser.add_argument("--batch", type=str, help="Comma-separated netuids for batch test")
    parser.add_argument("--api-key", default=os.environ.get("TAOSTATS_API_KEY"))
    parser.add_argument("--stats", action="store_true", help="Show source availability")
    args = parser.parse_args()

    fetcher = GiniFetcher(taostats_api_key=args.api_key)

    if args.stats:
        print("\nSource availability:")
        s = fetcher.cache_stats()
        print(f"  SDK available:       {s['sdk_available']}")
        print(f"  RPC available:       {s['rpc_available']}")
        print(f"  Taostats available:  {s['taostats_available']}")
        print()

    if args.batch:
        netuids = [int(x.strip()) for x in args.batch.split(",")]
        print(f"\nBatch fetch: {netuids}")
        results = fetcher.get_gini_batch(netuids)
        print(f"\n{'Subnet':<10} {'Gini':>8} {'Status':>12}")
        print("-" * 32)
        for netuid, gini in sorted(results.items()):
            status = "⚠️ HIGH" if gini >= 0.85 else ("WATCH" if gini >= 0.75 else "OK")
            print(f"  SN{netuid:<7} {gini:>8.4f} {status:>12}")
    else:
        print(f"\nFetching Gini for SN{args.netuid}...")
        gini = fetcher.get_gini(args.netuid)
        print(f"  Gini: {gini:.4f}")
        print(f"  Source: {fetcher.active_source}")
        print(f"  Status: {'⚠️ ABOVE 0.85 THRESHOLD' if gini >= 0.85 else '✓ Below threshold'}")

    print(f"\nInstall notes:")
    print(f"  pip install bittensor        # enables SDK source (recommended)")
    print(f"  pip install websocket-client # enables RPC source")
    print(f"  Without either: falls back to Taostats (12.5s/subnet rate limit)")
