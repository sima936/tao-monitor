"""Live TAO spot price in USD + GBP for the Telegram digest.

CoinGecko /simple/price — free, no key, one call gets both fiats.
Soft-fail: any error → returns None so the digest still ships without prices.
"""
from __future__ import annotations

import logging
import requests

logger = logging.getLogger(__name__)

_URL = "https://api.coingecko.com/api/v3/simple/price"
_PARAMS = {"ids": "bittensor", "vs_currencies": "usd,gbp"}
_HEADERS = {"User-Agent": "tao-monitor/1.0 (github.com/sima936/tao-monitor)"}
_TIMEOUT = 8.0


def get_tao_prices() -> dict | None:
    """Return {"usd": float, "gbp": float} or None on any failure."""
    try:
        r = requests.get(_URL, params=_PARAMS, headers=_HEADERS, timeout=_TIMEOUT)
        r.raise_for_status()
        data = (r.json() or {}).get("bittensor") or {}
        usd, gbp = data.get("usd"), data.get("gbp")
        if usd is None or gbp is None:
            logger.warning(f"CoinGecko returned partial: {data}")
            return None
        return {"usd": float(usd), "gbp": float(gbp)}
    except Exception as e:
        logger.warning(f"TAO spot fetch failed (non-fatal): {e}")
        return None
