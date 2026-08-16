"""Watchlist price-alert trigger logic — pure functions, no I/O.

Extracted from run_scoring.py so it's importable by tests without pulling
the bittensor/taostats stack. See run_scoring.py 'Watchlist price alerts'
block for the call site and format helpers.
"""
from __future__ import annotations

from typing import Optional


def evaluate_price_alert(
    pct24: Optional[float],
    up_wick: float,
    down_wick: float,
    up_ts: float,
    down_ts: float,
    now_ts: float,
    pct_thresh: float,
    wick_thresh: float,
    rate_limit_s: float,
) -> tuple[bool, bool, float, float]:
    """Decide whether to fire an up-alert, down-alert, both, or neither.

    Triggers:
      * up:   pct24 >= +pct_thresh  OR  up_wick   >= wick_thresh
      * down: pct24 <= -pct_thresh  OR  down_wick >= wick_thresh

    Rate limit is per-direction so a fresh up-alert doesn't suppress a
    subsequent down-alert (different signal, different opportunity).

    Returns (fire_up, fire_down, new_up_ts, new_down_ts). When a direction
    doesn't fire, the timestamp is passed through unchanged so state persists
    cleanly across cycles.
    """
    up_fire = (pct24 is not None and pct24 >= pct_thresh) or up_wick >= wick_thresh
    down_fire = (pct24 is not None and pct24 <= -pct_thresh) or down_wick >= wick_thresh
    can_up = (now_ts - up_ts) >= rate_limit_s
    can_down = (now_ts - down_ts) >= rate_limit_s
    fire_up = up_fire and can_up
    fire_down = down_fire and can_down
    return (
        fire_up,
        fire_down,
        (now_ts if fire_up else up_ts),
        (now_ts if fire_down else down_ts),
    )
