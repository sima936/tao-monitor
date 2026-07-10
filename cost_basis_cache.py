"""Cost basis cache — durable persistence layer for stops + P&L.

Wraps `fetch_cost_basis` (taostats) with disk caching so a taostats API blip
(rate limit, credit exhaustion, transient 5xx) doesn't leave the cron blind.
Symptom this fixes: LS34 "Stops SKIPPED: cost basis unavailable" → the whole
book runs without stops until the next successful taostats page-through.

Design:
    • On fetch success → save cache. Cache is the last-good full picture.
    • On fetch failure → load cache. Log age so staleness is visible.
    • New positions between refreshes → estimate from balance × current pool
      price and flag with `_estimated: True`. Rough (slippage + emissions
      unaccounted) but always available; stops resume immediately, P&L is
      approximate until the next fresh fetch corrects it.

Cache location: /data/cost_basis_cache.json by default (Railway volume mount
on spectacular-adaptation per LS34). Override via env COST_BASIS_CACHE_PATH.

Schema matches `fetch_cost_basis`'s return dict with one added field:
    _cache_written : ISO8601 timestamp of when this file was written
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("cost_basis_cache")

DEFAULT_CACHE_PATH = Path(
    os.environ.get("COST_BASIS_CACHE_PATH", "/data/cost_basis_cache.json")
)


def load_cost_basis_cache(path: Path = DEFAULT_CACHE_PATH) -> dict | None:
    """Return the cached cost basis dict, or None if unavailable/corrupt.

    Non-fatal on any error — caller treats None as "no cache, keep going".
    """
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        cb = json.loads(path.read_text())
        if not isinstance(cb, dict) or "positions" not in cb:
            logger.warning(f"Cost basis cache at {path} has bad schema; ignoring")
            return None
        return cb
    except Exception as e:
        logger.warning(f"Cost basis cache load failed: {e}")
        return None


def save_cost_basis_cache(cb: dict, path: Path = DEFAULT_CACHE_PATH) -> None:
    """Persist cost basis to disk atomically (temp file + rename).

    Non-fatal on any error — the cron continues without caching. Adds
    `_cache_written` so age can be computed on next load.
    """
    if not cb or not isinstance(cb, dict):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(cb)
        payload["_cache_written"] = datetime.now(timezone.utc).isoformat()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
    except Exception as e:
        logger.warning(f"Cost basis cache save failed: {e}")


def cache_age_hours(cb: dict) -> float | None:
    """Hours since the cache was written. Falls back to `_computed` if
    `_cache_written` is absent (older schema). Returns None if neither
    field parses."""
    ts_str = cb.get("_cache_written") or cb.get("_computed")
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        return delta.total_seconds() / 3600.0
    except Exception:
        return None


def augment_with_new_positions(
    cb: dict,
    bal_by_netuid: dict,
    metrics,               # list of SubnetMetric (or anything with .subnet_id + .token_price)
    holdings: list[int],
    min_alpha_balance: float = 0.001,
) -> tuple[dict, list[int]]:
    """Estimate cost basis for held netuids missing from the cached dict.

    Estimate = alpha_balance × current_pool_price. Rough (ignores slippage
    and any post-entry emissions), but always available, so stops + digest
    resume immediately. Marks each estimated position with `_estimated: True`
    so downstream can flag them if it wants.

    SN0 (root) is skipped — cost basis for delegated TAO is handled elsewhere.

    Returns (augmented_cb, list_of_estimated_netuids). If nothing to
    augment, returns the input dict unchanged.
    """
    if not bal_by_netuid or not holdings:
        return cb, []
    price_by_id = {}
    for m in (metrics or []):
        try:
            nid = int(m.subnet_id)
            price = float(getattr(m, "token_price", 0) or 0)
            if price > 0:
                price_by_id[nid] = price
        except (TypeError, ValueError, AttributeError):
            continue
    positions = dict(cb.get("positions") or {})
    estimated: list[int] = []
    for nid in holdings:
        try:
            nid_int = int(nid)
        except (TypeError, ValueError):
            continue
        if nid_int == 0:
            continue          # SN0 root — handled separately
        nid_str = str(nid_int)
        if nid_str in positions:
            continue          # already have a basis (fresh or prior cache)
        bal_tao = float(bal_by_netuid.get(nid_int, 0) or 0)
        if bal_tao <= min_alpha_balance:
            continue          # not actually held
        price = price_by_id.get(nid_int, 0.0)
        if price <= 0:
            continue          # no price data — can't estimate
        # bal_by_netuid is already spot-valued in TAO (see chain_fetch.py /
        # parse_stake_balances) — use it directly as the cost estimate,
        # don't multiply by price again (same bug as tao_bot_listener /pnl).
        est_tao = round(bal_tao, 6)
        positions[nid_str] = {
            "tao_invested": est_tao,
            "tao_in": est_tao,
            "tao_out": 0.0,
            "n_events": 0,
            "transfers": 0,
            "_estimated": True,
        }
        estimated.append(nid_int)
    if not estimated:
        return cb, []
    augmented = dict(cb)
    augmented["positions"] = positions
    return augmented, estimated
