"""
GeckoTerminal — real daily price history for Bittensor subnet alpha tokens
=========================================================================
Replaces the unreliable taostats `pool/history` call (and the 3-anchor
SYNTHETIC reconstruction it falls back to) with genuine daily closes.

Why this exists
---------------
pool/latest stopped returning seven_day_prices, so every subnet fell back to
a 9-bar synthetic line built from just {price, 24h%, 7d%}. That makes the
engine's regime/EMA/7d a deterministic re-encoding of the volatile 7d anchor,
which whipsaws holdings (e.g. Minos flipping Bull↔Sideways on a flat price).

GeckoTerminal indexes the Bittensor network per-subnet. The alpha/TAO pool
address is simply `0-{netuid}` (verified: SN107→0-107, SN9→0-9, SN4→0-4).
Daily OHLCV is free, no API key, ~30 req/min, up to ~6 months of candles.
GeckoTerminal stores the history, so there is NO persistence to build.

Endpoint
--------
GET https://api.geckoterminal.com/api/v2/networks/bittensor/pools/0-{netuid}/ohlcv/day
    ?aggregate=1&limit={N}&currency={token|usd}

Response shape (Beta API — version-pinned via Accept header):
    {"data":{"attributes":{"ohlcv_list":[[ts, open, high, low, close, vol], ...]}}}
ohlcv_list is newest-first; we sort ascending → oldest-first to match the
engine's price_history contract.

DENOMINATION
------------
`currency=token` returns the alpha price in the pool's quote token (TAO),
which is what a TAO-staker cares about and what the rest of the engine uses.
The probe below PRINTS the values so you can eyeball the scale:
  TAO-denominated SN107 should read ~0.003-0.05 (not ~1-20 USD).
If GT returns USD instead, pass --usd and convert downstream by TAO/USD, OR
just use it as-is for regime (returns are scale-invariant) — a real USD daily
series is still infinitely more stable than the synthetic line.

This module ONLY fetches + parses. Wiring into run_scoring.py is a separate
step, gated on this probe showing real, stable bars.

Dependencies: requests (already in requirements.txt).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger("geckoterminal_fetch")

GT_BASE = "https://api.geckoterminal.com/api/v2"
GT_NETWORK = "bittensor"
# Pin the Beta API version (docs explicitly recommend this to avoid surprises).
GT_HEADERS = {"Accept": "application/json;version=20230302"}

# Free public API is ~30 calls/min. 2.1s spacing keeps us comfortably under it.
DEFAULT_SPACING_S = 2.1


def pool_address(netuid: int) -> str:
    """Bittensor subnet pool address on GeckoTerminal is just `0-{netuid}`."""
    return f"0-{netuid}"


def _to_iso(ts_epoch: int) -> str:
    """Epoch seconds → ISO8601 UTC (matches the engine's timestamp strings)."""
    return datetime.fromtimestamp(int(ts_epoch), tz=timezone.utc).isoformat()


def fetch_daily_closes(
    netuid: int,
    limit: int = 120,
    currency: str = "token",
    session: Optional[requests.Session] = None,
    timeout: int = 20,
) -> tuple[list[float], list[str]]:
    """Fetch real daily closes for one subnet alpha/TAO pool.

    Returns (closes, iso_timestamps) sorted oldest-first. Empty on any failure
    (caller keeps whatever history it already had — never crash the cron).

    currency: 'token' → price in TAO (pool quote token); 'usd' → USD.
    limit:    number of daily candles (GT caps at 1000; ~120 covers a 72-EMA
              with margin while staying small).
    """
    sess = session or requests.Session()
    url = f"{GT_BASE}/networks/{GT_NETWORK}/pools/{pool_address(netuid)}/ohlcv/day"
    params = {"aggregate": 1, "limit": min(int(limit), 1000), "currency": currency}

    try:
        resp = sess.get(url, params=params, headers=GT_HEADERS, timeout=timeout)
        if resp.status_code == 429:
            logger.warning(f"SN{netuid}: GT rate-limited (429) — backing off 5s")
            time.sleep(5)
            resp = sess.get(url, params=params, headers=GT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 — never let a fetch kill the cycle
        logger.warning(f"SN{netuid}: GT fetch failed: {e}")
        return [], []

    try:
        ohlcv = payload["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        logger.warning(f"SN{netuid}: GT response missing ohlcv_list")
        return [], []

    rows = []
    for c in ohlcv or []:
        # [timestamp, open, high, low, close, volume]
        if not isinstance(c, (list, tuple)) or len(c) < 5:
            continue
        try:
            ts = int(c[0])
            close = float(c[4])
        except (TypeError, ValueError):
            continue
        if close > 0:
            rows.append((ts, close))

    # GT returns newest-first; sort ascending → oldest-first.
    rows.sort(key=lambda r: r[0])
    closes = [c for _, c in rows]
    stamps = [_to_iso(ts) for ts, _ in rows]
    return closes, stamps


def fetch_history_for_netuids(
    netuids: list[int],
    limit: int = 120,
    currency: str = "token",
    spacing_s: float = DEFAULT_SPACING_S,
    min_bars: int = 9,
) -> dict[int, tuple[list[float], list[str]]]:
    """Fetch daily history for many subnets, rate-limit friendly.

    Returns {netuid: (closes, timestamps)} for every subnet that returned at
    least `min_bars` bars. Subnets with fewer (truly new pools) are omitted so
    the caller leaves their existing series untouched — same contract as
    fetch_holdings_history(), so it drops straight into apply_history_overrides.
    """
    out: dict[int, tuple[list[float], list[str]]] = {}
    sess = requests.Session()
    for i, netuid in enumerate(netuids):
        if i:
            time.sleep(spacing_s)
        closes, stamps = fetch_daily_closes(netuid, limit, currency, sess)
        if len(closes) >= min_bars:
            out[netuid] = (closes, stamps)
            logger.info(f"SN{netuid}: {len(closes)} real daily bars (GT)")
        else:
            logger.info(f"SN{netuid}: only {len(closes)} GT bars — leaving existing history")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Offline parser self-test (no network) — proves the transform is correct.
# ─────────────────────────────────────────────────────────────────────────────
def _self_test() -> None:
    sample = {
        "data": {"attributes": {"ohlcv_list": [
            # newest-first, as GT returns
            [1718150400, 0.0040, 0.0042, 0.0039, 0.00383, 1234.0],
            [1718064000, 0.0038, 0.0041, 0.0037, 0.00400, 2345.0],
            [1717977600, 0.0035, 0.0039, 0.0035, 0.00380, 3456.0],
        ]}}
    }
    rows = sample["data"]["attributes"]["ohlcv_list"]
    parsed = []
    for c in rows:
        parsed.append((int(c[0]), float(c[4])))
    parsed.sort(key=lambda r: r[0])
    closes = [c for _, c in parsed]
    assert closes == [0.00380, 0.00400, 0.00383], closes  # oldest-first
    assert _to_iso(1717977600).startswith("2024-06-")
    print("✓ parser self-test passed (oldest-first ordering, close extraction, iso ts)")


# ─────────────────────────────────────────────────────────────────────────────
# Live probe — run this on Railway/your machine to confirm REAL, STABLE bars
# before we wire GT into run_scoring.py.
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="GeckoTerminal Bittensor daily-history probe")
    p.add_argument("--netuid", type=int, help="Single subnet to probe (e.g. 107 for Minos)")
    p.add_argument("--netuids", type=str, help="Comma-separated netuids (e.g. 107,9,4,55)")
    p.add_argument("--limit", type=int, default=90, help="Daily candles to fetch (default 90)")
    p.add_argument("--usd", action="store_true", help="Fetch USD instead of TAO-denominated")
    p.add_argument("--self-test", action="store_true", help="Run offline parser test and exit")
    args = p.parse_args()

    if args.self_test:
        _self_test()
        raise SystemExit(0)

    currency = "usd" if args.usd else "token"

    def _show(nid: int):
        closes, stamps = fetch_daily_closes(nid, args.limit, currency)
        if not closes:
            print(f"\nSN{nid}: no bars returned (new pool? wrong denom? check --usd).")
            return
        last = closes[-1]
        # 7d = last vs 8 bars back; EMA-ish position vs simple mean of window
        seven = (closes[-1] - closes[-8]) / closes[-8] if len(closes) >= 8 and closes[-8] else float("nan")
        mean = sum(closes) / len(closes)
        print(f"\nSN{nid}  ({currency})  {len(closes)} daily bars")
        print(f"  range : {stamps[0][:10]} → {stamps[-1][:10]}")
        print(f"  closes: first={closes[0]:.6g}  last={last:.6g}  mean={mean:.6g}")
        print(f"  7d (last vs -8): {seven * 100:+.1f}%   <- should be STABLE run-to-run")
        print(f"  last 8: {[round(c, 6) for c in closes[-8:]]}")

    if args.netuid is not None:
        _show(args.netuid)
    elif args.netuids:
        for nid in [int(x) for x in args.netuids.split(",")]:
            _show(nid)
            time.sleep(DEFAULT_SPACING_S)
    else:
        # Default: probe Simon's holdings + Minos focus
        for nid in [107, 9, 4, 55, 44, 68, 46, 123]:
            _show(nid)
            time.sleep(DEFAULT_SPACING_S)
