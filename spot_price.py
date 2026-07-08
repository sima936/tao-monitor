"""Live TAO spot price in USD + GBP for the Telegram digest.

CoinGecko /simple/price — free, no key, one call gets both fiats.

Rate-limit safe: CoinGecko free tier throttles per IP, and Railway egress IPs
are shared, so consecutive live calls can silently return 429/None. On every
success we write the result to SPOT_CACHE_PATH (volume-backed if
$SPOT_CACHE_PATH is set — mirrors the Gini cache pattern). On any failure we
serve the cache if it's fresher than SPOT_CACHE_MAX_AGE_H (default 6h — one
cron cycle). Only returns None when both live and cache are unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_URL = "https://api.coingecko.com/api/v3/simple/price"
_PARAMS = {"ids": "bittensor", "vs_currencies": "usd,gbp"}
_HEADERS = {"User-Agent": "tao-monitor/1.0 (github.com/sima936/tao-monitor)"}
_TIMEOUT = 8.0

SPOT_CACHE_PATH = Path(
    os.environ.get(
        "SPOT_CACHE_PATH",
        str(Path(__file__).parent / "spot_cache.json"),
    )
)
SPOT_CACHE_MAX_AGE_H = float(os.environ.get("SPOT_CACHE_MAX_AGE_H", 6))


def _write_cache(usd: float, gbp: float) -> None:
    try:
        SPOT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SPOT_CACHE_PATH.write_text(
            json.dumps({"usd": usd, "gbp": gbp, "ts": time.time()})
        )
    except Exception as e:
        logger.warning(f"Spot cache write failed (non-fatal): {e}")


def _read_cache() -> dict | None:
    try:
        if not SPOT_CACHE_PATH.exists():
            return None
        d = json.loads(SPOT_CACHE_PATH.read_text())
        age_h = (time.time() - float(d.get("ts", 0))) / 3600.0
        if age_h > SPOT_CACHE_MAX_AGE_H:
            logger.info(
                f"Spot cache stale ({age_h:.1f}h > {SPOT_CACHE_MAX_AGE_H}h) — discarding"
            )
            return None
        return {"usd": float(d["usd"]), "gbp": float(d["gbp"])}
    except Exception as e:
        logger.warning(f"Spot cache read failed (non-fatal): {e}")
        return None


def get_tao_prices() -> dict | None:
    """Return {"usd": float, "gbp": float} — live if reachable, else cached,
    else None. Never raises."""
    try:
        r = requests.get(_URL, params=_PARAMS, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        data = (r.json() or {}).get("bittensor") or {}
        usd, gbp = data.get("usd"), data.get("gbp")
        if usd is None or gbp is None:
            logger.warning(f"CoinGecko returned partial: {data} — trying cache")
            return _read_cache()
        prices = {"usd": float(usd), "gbp": float(gbp)}
        _write_cache(prices["usd"], prices["gbp"])
        return prices
    except Exception as e:
        logger.warning(f"TAO spot fetch failed ({e}) — trying cache")
        return _read_cache()
