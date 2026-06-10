"""
Subnet Scoring Engine — TAO Monitor v4
=======================================
Full 10-parameter scoring framework with strategy comparison test.

The 10 parameters (ranked by signal quality):
  1.  Gini/Genie concentration     — structural manipulation risk (hard filter)
  2.  TAO macro regime             — Markov on TAO itself, gates entire strategy
  3.  EMA slope direction          — long-term trend health (not price vs EMA)
  4.  Pullback depth from high     — mean-reversion entry signal
  5.  Pool depth trajectory        — capital inflow/outflow trend
  6.  Markov persistence           — how sticky is the current regime
  7.  EMA displacement rate        — how fast price is moving toward/away EMA
  8.  Volume trend direction        — growing volume = genuine interest
  9.  Subnet data maturity          — penalise thin history, boost reliable signals
  10. Relative performance vs TAO  — is this subnet generating alpha

Dual scores per subnet:
  entry_score  (0-100): ideal for NEW positions — pullback bias
  health_score (0-100): ideal for EXISTING holds — stability bias

TAO macro regime gates everything:
  Bull     → active rotation, buy pullbacks, take profits into strength
  Sideways → hold conviction, avoid new entries
  Bear     → retreat to SN0/Root, capital preservation

Strategy comparison test (run_comparison_test):
  Runs Phase 2 (momentum) and Phase 4 (pullback) scoring walk-forward
  on real price history and compares forward returns side by side.

Dependencies: numpy, pandas
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SUBNET_WINDOW    = 7       # Markov rolling return window (bars)
SUBNET_THRESHOLD = 0.10    # ±10% regime boundary
SUBNET_MIN_TRAIN = 60      # min bars before Markov is reliable
EMA_PERIOD       = 72      # EMA lookback period

# Siam's hard pre-filter thresholds
# Raised to accommodate real holdings (SN4 ~0.054, SN51 ~0.051, SN64 ~0.10+)
# Pool cap raised to match actual subnet pool sizes (SN62/68/75 ~35-130k TAO)
MAX_TOKEN_PRICE  = 0.15    # was 0.04 — covers all current holdings incl. SN64
MIN_POOL_DEPTH   = 5.0
MAX_POOL_DEPTH   = 500000.0  # was 5000 — covers SN4 (130k), SN51 (114k)
MAX_GENIE_SCORE  = 0.85

# Take-profit thresholds (% above EMA)
TP_WARN_PCT      = 0.15    # 15% above EMA → partial trim
TP_STRONG_PCT    = 0.25    # 25% above EMA → full trim

# Pullback entry zone (% off recent high)
PB_MIN           = 0.08    # at least 8% off high
PB_MAX           = 0.35    # not more than 35% (broken)

# TAO macro Markov params
TAO_WINDOW       = 14
TAO_THRESHOLD    = 0.07

# EMA slope lookback for trend direction
EMA_SLOPE_BARS   = 14

# Pool depth trajectory window
POOL_TREND_BARS  = 5       # compare pool depth now vs N snapshots ago

# Relative performance window
REL_PERF_BARS    = 7       # subnet vs TAO over this many bars

STATES = ["Bear", "Sideways", "Bull"]


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

class FilterResult(Enum):
    PASS          = "pass"
    FAIL_PRICE    = "fail_price_too_high"
    FAIL_POOL_MIN = "fail_pool_too_shallow"
    FAIL_POOL_MAX = "fail_pool_too_deep"
    FAIL_GENIE    = "fail_genie_concentrated"
    FAIL_NO_DATA  = "fail_insufficient_data"


class MacroRegime(Enum):
    BULL     = "Bull"
    SIDEWAYS = "Sideways"
    BEAR     = "Bear"
    UNKNOWN  = "Unknown"


@dataclass
class SubnetMetrics:
    """Raw metrics for one subnet — identical interface to v2/v3."""
    subnet_id:      int
    name:           str
    token_price:    float           # TAO
    pool_depth:     float           # TAO (current)
    genie_score:    float           # 0-1 concentration
    price_history:  list[float]     # oldest → newest
    timestamps:     list[str]
    volume_24h:     float = 0.0     # TAO
    volume_7d:      float = 0.0     # TAO
    pool_history:   list[float] = field(default_factory=list)   # param 5
    volume_history: list[float] = field(default_factory=list)   # param 8


@dataclass
class ParameterScores:
    """Individual score for each of the 10 parameters (0-100 each)."""
    p1_genie:           float   # concentration health (inverted)
    p2_macro:           float   # TAO macro alignment
    p3_ema_slope:       float   # long-term EMA direction
    p4_pullback:        float   # pullback depth quality
    p5_pool_trajectory: float   # pool depth trending up/down
    p6_markov_persist:  float   # regime stickiness
    p7_ema_displacement:float   # rate of price → EMA movement
    p8_volume_trend:    float   # volume expanding/contracting
    p9_data_maturity:   float   # history depth confidence weight
    p10_relative_perf:  float   # alpha vs TAO


@dataclass
class SubnetScore:
    """Full scored output for one subnet."""
    subnet_id:              int
    name:                   str
    entry_score:            float       # 0-100
    health_score:           float       # 0-100
    params:                 ParameterScores

    # Key computed values for display
    markov_regime:          str
    markov_signal:          float
    markov_persistence:     float       # P(same→same) for current regime
    markov_available:       bool
    pct_from_ema:           Optional[float]
    pct_from_recent_high:   Optional[float]
    ema_slope_pct:          Optional[float]
    pool_depth_trending:    str         # "up" / "down" / "flat" / "unknown"
    relative_perf_vs_tao:   Optional[float]
    pct_change_24h:         Optional[float]
    pct_change_7d:          Optional[float]
    token_price:            float
    pool_depth:             float
    genie_score_raw:        float

    transition_matrix:      Optional[list] = None
    alert_flags:            list[str] = field(default_factory=list)
    take_profit_flags:      list[str] = field(default_factory=list)
    entry_flags:            list[str] = field(default_factory=list)


@dataclass
class TaoMacroState:
    regime:        MacroRegime
    signal:        float
    bull_prob:     float
    bear_prob:     float
    strategy_mode: str
    available:     bool = True


@dataclass
class ScoringResult:
    timestamp:        str
    macro:            TaoMacroState
    total_subnets:    int
    passed_filters:   int
    failed_filters:   int
    ranked_by_entry:  list[SubnetScore]
    ranked_by_health: list[SubnetScore]
    filtered_out:     list[dict]
    top_n:            int = 10


# ─────────────────────────────────────────────────────────────────────────────
# Markov core
# ─────────────────────────────────────────────────────────────────────────────

def _label_regimes(close: pd.Series, window: int, threshold: float) -> pd.Series:
    rolling_return = close.pct_change(window)
    labels = pd.Series(1, index=close.index, dtype=int)
    labels[rolling_return >  threshold] = 2
    labels[rolling_return < -threshold] = 0
    return labels.loc[rolling_return.notna()]


def _build_transition_matrix(labels: pd.Series) -> np.ndarray:
    counts = np.zeros((3, 3), dtype=float)
    arr = np.asarray(labels, dtype=int)
    for i in range(len(arr) - 1):
        counts[arr[i], arr[i + 1]] += 1.0
    row_sums = counts.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    return counts / row_sums


def _run_markov(prices: list[float], timestamps: list[str], window: int, threshold: float) -> Optional[dict]:
    if len(prices) < window + 2:
        return None
    try:
        idx = (pd.to_datetime(timestamps, utc=False, errors="coerce")
               if timestamps and timestamps[0]
               else pd.RangeIndex(len(prices)))
        if hasattr(idx, "isna") and idx.isna().all():
            idx = pd.RangeIndex(len(prices))
    except Exception:
        idx = pd.RangeIndex(len(prices))

    close = pd.Series(prices, index=idx, dtype=float).dropna()
    if len(close) < window + 2:
        return None
    labels = _label_regimes(close, window, threshold)
    if len(labels) < 2:
        return None
    P = _build_transition_matrix(labels)
    state = int(labels.iloc[-1])
    return {
        "current_regime":  STATES[state],
        "current_state_i": state,
        "signal":          float(P[state, 2] - P[state, 0]),
        "next_bull":       float(P[state, 2]),
        "next_bear":       float(P[state, 0]),
        "persistence":     float(P[state, state]),   # P(same→same)
        "transition_matrix": [[float(x) for x in row] for row in P],
        "data_points":     len(labels),
    }


# ─────────────────────────────────────────────────────────────────────────────
# EMA utility
# ─────────────────────────────────────────────────────────────────────────────

def _ema(prices: np.ndarray, period: int) -> np.ndarray:
    p = min(period, len(prices) - 1)
    if p < 2:
        return np.full_like(prices, prices[-1])
    alpha = 2.0 / (p + 1)
    out = np.empty_like(prices)
    out[0] = prices[0]
    for i in range(1, len(prices)):
        out[i] = alpha * prices[i] + (1 - alpha) * out[i - 1]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# TAO macro regime (parameter 2)
# ─────────────────────────────────────────────────────────────────────────────

def compute_tao_macro(
    tao_prices: list[float],
    tao_timestamps: Optional[list[str]] = None,
) -> TaoMacroState:
    ts = tao_timestamps if tao_timestamps else []
    if not tao_prices or len(tao_prices) < TAO_WINDOW + 2:
        return TaoMacroState(MacroRegime.UNKNOWN, 0.0, 0.33, 0.33,
                             "⚠️ TAO macro unknown — no new entries. Hold existing.",
                             available=False)
    r = _run_markov(tao_prices, ts, TAO_WINDOW, TAO_THRESHOLD)
    if r is None:
        return TaoMacroState(MacroRegime.UNKNOWN, 0.0, 0.33, 0.33,
                             "⚠️ TAO macro unknown — no new entries. Hold existing.",
                             available=False)
    reg = r["current_regime"]
    if reg == "Bull":
        regime, mode = MacroRegime.BULL, "🟢 BULL — Rotate actively. Buy pullbacks. Take profits into strength."
    elif reg == "Bear":
        regime, mode = MacroRegime.BEAR, "🔴 BEAR — Capital preservation. Move to SN0. No new entries."
    else:
        regime, mode = MacroRegime.SIDEWAYS, "🟡 SIDEWAYS — Hold conviction. Avoid new entries. Trim weak."
    return TaoMacroState(regime, r["signal"], r["next_bull"], r["next_bear"], mode)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-filters (hard gates — unchanged from Siam)
# ─────────────────────────────────────────────────────────────────────────────

def apply_pre_filters(m: SubnetMetrics) -> FilterResult:
    if len(m.price_history) < SUBNET_WINDOW + 2:
        return FilterResult.FAIL_NO_DATA
    if m.token_price >= MAX_TOKEN_PRICE:
        return FilterResult.FAIL_PRICE
    if m.pool_depth < MIN_POOL_DEPTH:
        return FilterResult.FAIL_POOL_MIN
    if m.pool_depth > MAX_POOL_DEPTH:
        return FilterResult.FAIL_POOL_MAX
    # if m.genie_score >= MAX_GENIE_SCORE:
    #     return FilterResult.FAIL_GENIE
        
    return FilterResult.PASS


# ─────────────────────────────────────────────────────────────────────────────
# The 10 parameter scoring functions
# ─────────────────────────────────────────────────────────────────────────────

def p1_score_genie(genie: float) -> float:
    """Parameter 1 — Genie/Gini concentration (already filtered >0.85).
    Lower concentration = better distribution = higher score.
    0.0 → 100,  0.85 → 0
    """
    if genie <= 0.0:
        return 100.0
    if genie >= MAX_GENIE_SCORE:
        return 0.0
    return max(0.0, (1.0 - genie / MAX_GENIE_SCORE) * 100.0)


def p2_score_macro(macro: TaoMacroState) -> float:
    """Parameter 2 — TAO macro regime alignment.
    Bull → 80-100 (active mode)
    Sideways → 40-60 (cautious)
    Bear → 0-20 (defensive)
    Unknown → 30
    """
    if not macro.available:
        return 30.0
    if macro.regime == MacroRegime.BULL:
        # Scale within bull: stronger signal = higher score
        return min(100.0, 70.0 + macro.signal * 30.0)
    elif macro.regime == MacroRegime.SIDEWAYS:
        return 50.0
    elif macro.regime == MacroRegime.BEAR:
        return max(0.0, 20.0 + macro.signal * 20.0)
    return 30.0


def p3_score_ema_slope(prices: np.ndarray) -> tuple[float, float]:
    """Parameter 3 — EMA slope direction (trend health).
    Returns (score, ema_slope_pct).
    Measures EMA gradient over EMA_SLOPE_BARS, not price vs EMA.
    Rising EMA = healthy trend regardless of short-term price.
    +5%+ slope → 100,  flat → 60,  -5%+ → 0
    """
    if len(prices) < 5:
        return 50.0, 0.0
    ema = _ema(prices, EMA_PERIOD)
    lookback = min(EMA_SLOPE_BARS, len(ema) - 1)
    base = ema[-(lookback + 1)]
    slope = (ema[-1] - base) / base if base != 0 else 0.0
    score = 60.0 + (slope / 0.05) * 40.0
    return max(0.0, min(100.0, score)), slope


def p4_score_pullback(prices: np.ndarray, pct_from_ema: float) -> tuple[float, float]:
    """Parameter 4 — Pullback depth from recent high.
    Returns (score, pct_from_recent_high).
    Ideal entry: 8-25% off high, near EMA.
    At all-time high → low score (don't chase).
    >35% off high → low score (potentially broken).
    """
    if len(prices) < 10:
        return 50.0, None
    window = prices[-30:] if len(prices) >= 30 else prices
    recent_high = float(np.max(window))
    current = prices[-1]
    if recent_high == 0:
        return 50.0, None
    pct_off = (current - recent_high) / recent_high   # negative = below high

    depth = abs(pct_off)
    if PB_MIN <= depth <= PB_MAX:
        # In pullback zone — reward proximity to EMA
        ema_prox = abs(pct_from_ema)
        if ema_prox < 0.05:
            score = 95.0
        elif ema_prox < 0.10:
            score = 80.0
        elif pct_from_ema > 0.10:
            score = 60.0
        elif pct_from_ema < -0.10:
            score = 40.0
        else:
            score = 70.0
    elif depth < PB_MIN:
        # Near or at high — chasing
        score = 10.0 + (depth / PB_MIN) * 50.0
    else:
        # >35% off — potentially broken
        excess = depth - PB_MAX
        score = max(0.0, 40.0 - (excess / 0.15) * 40.0)

    return max(0.0, min(100.0, score)), pct_off


def p5_score_pool_trajectory(pool_history: list[float], current_pool: float) -> tuple[float, str]:
    """Parameter 5 — Pool depth trajectory (capital inflow/outflow).
    Returns (score, direction_label).
    Growing pool = genuine inflow = bullish structural signal.
    Shrinking pool = quiet exit even if price holds.
    """
    if not pool_history or len(pool_history) < 2:
        # No history — score on absolute depth sweet spot only
        if MIN_POOL_DEPTH < current_pool < MAX_POOL_DEPTH:
            if 20 <= current_pool <= 2000:
                return 60.0, "unknown"
            return 50.0, "unknown"
        return 30.0, "unknown"

    hist = np.array(pool_history[-POOL_TREND_BARS:], dtype=float)
    older = float(hist[0])
    newer = current_pool

    if older == 0:
        return 50.0, "unknown"

    pct_change = (newer - older) / older

    if pct_change > 0.10:
        direction = "up"
        score = min(100.0, 70.0 + pct_change * 100.0)
    elif pct_change > 0.02:
        direction = "up"
        score = 65.0
    elif pct_change > -0.02:
        direction = "flat"
        score = 55.0
    elif pct_change > -0.10:
        direction = "down"
        score = 35.0
    else:
        direction = "down"
        score = max(0.0, 25.0 + pct_change * 50.0)

    return max(0.0, min(100.0, score)), direction


def p6_score_markov_persistence(markov_result: Optional[dict]) -> float:
    """Parameter 6 — Markov regime persistence (diagonal stickiness).
    P(bull→bull) or P(bear→bear) for current regime.
    High persistence in Bull = strong hold/entry signal.
    High persistence in Bear = strong exit signal.
    Low persistence in any regime = unstable, be cautious.
    """
    if markov_result is None:
        return 50.0
    regime = markov_result["current_regime"]
    persistence = markov_result["persistence"]

    if regime == "Bull":
        # High persistence in Bull is good (>0.7 → 80-100)
        score = persistence * 100.0
    elif regime == "Bear":
        # High persistence in Bear is bad — invert it
        # We want to exit fast; low persistence in Bear = regime change coming = slightly better
        score = (1.0 - persistence) * 60.0
    else:
        # Sideways — moderate persistence is fine, very high means stuck
        score = 40.0 + (1.0 - abs(persistence - 0.6)) * 20.0

    return max(0.0, min(100.0, score))


def p7_score_ema_displacement_rate(prices: np.ndarray) -> tuple[float, float]:
    """Parameter 7 — Rate of price movement toward/away from EMA.
    Returns (score, pct_from_ema).
    Rapidly approaching EMA from above (in healthy pullback) = high score.
    Rapidly moving away from EMA upward = extended, lower entry score.
    Rapidly falling away from EMA downward = deteriorating.
    """
    if len(prices) < 5:
        return 50.0, 0.0

    ema = _ema(prices, EMA_PERIOD)
    current_price = prices[-1]
    current_ema = ema[-1]
    if current_ema == 0:
        return 50.0, 0.0

    pct_from_ema = (current_price - current_ema) / current_ema

    # Rate: how has pct_from_ema changed over last 3 bars?
    lookback = min(3, len(prices) - 1)
    past_price = prices[-(lookback + 1)]
    past_ema = ema[-(lookback + 1)]
    past_disp = (past_price - past_ema) / past_ema if past_ema != 0 else 0.0
    rate_of_change = pct_from_ema - past_disp  # positive = moving further away

    # Score logic:
    # Currently above EMA and approaching (rate negative) = pullback developing → good entry
    # Currently below EMA and approaching (rate positive) = recovering → good entry
    # Currently above EMA and accelerating away (rate positive) = chasing → low entry
    # Currently below EMA and accelerating away (rate negative) = breaking down → bad

    if pct_from_ema > 0:
        if rate_of_change < -0.02:   # pulling back toward EMA from above
            score = 85.0
        elif rate_of_change < 0:
            score = 70.0
        elif rate_of_change < 0.02:  # extended but stable
            score = 50.0
        else:                         # accelerating above EMA
            score = max(10.0, 40.0 - rate_of_change * 200.0)
    else:
        if rate_of_change > 0.02:    # recovering back toward EMA
            score = 75.0
        elif rate_of_change > 0:
            score = 60.0
        elif rate_of_change > -0.02: # below EMA but stable
            score = 40.0
        else:                         # accelerating below EMA
            score = max(0.0, 25.0 + rate_of_change * 200.0)

    return max(0.0, min(100.0, score)), pct_from_ema


def p8_score_volume_trend(volume_history: list[float], volume_24h: float) -> float:
    """Parameter 8 — Volume trend direction.
    Expanding volume = genuine interest growing.
    Contracting volume during rally = distribution warning.
    Returns 50 if no volume history (neutral, don't penalise missing data).
    """
    if not volume_history or len(volume_history) < 2:
        if volume_24h > 0:
            return 55.0   # at least we have something, slight positive
        return 50.0

    hist = np.array(volume_history, dtype=float)
    avg_recent = float(np.mean(hist[-3:])) if len(hist) >= 3 else float(hist[-1])
    avg_older = float(np.mean(hist[:3])) if len(hist) >= 3 else float(hist[0])

    if avg_older == 0:
        return 50.0

    vol_change = (avg_recent - avg_older) / avg_older

    if vol_change > 0.30:
        score = 90.0
    elif vol_change > 0.10:
        score = 75.0
    elif vol_change > -0.10:
        score = 55.0
    elif vol_change > -0.30:
        score = 35.0
    else:
        score = max(0.0, 20.0 + vol_change * 50.0)

    return max(0.0, min(100.0, score))


def p9_score_data_maturity(n_bars: int) -> float:
    """Parameter 9 — Data maturity / history depth.
    More history = more reliable signals = higher confidence multiplier.
    This isn't a signal itself — it's a confidence weight applied to
    the composite score. Used as a score component to slightly penalise
    subnets with thin history where Markov and EMA are unreliable.
    <20 bars → 20 (very unreliable)
    60 bars  → 60 (minimum reliable)
    150+ bars → 100 (full confidence)
    """
    if n_bars < 10:
        return 10.0
    if n_bars >= 150:
        return 100.0
    if n_bars < 60:
        return 20.0 + (n_bars / 60.0) * 40.0
    return 60.0 + ((n_bars - 60) / 90.0) * 40.0


def p10_score_relative_performance(
    subnet_prices: list[float],
    tao_prices: list[float],
) -> float:
    """Parameter 10 — Relative performance vs TAO.
    Is this subnet generating alpha over TAO?
    Outperforming TAO over REL_PERF_BARS = positive alpha = worth the extra risk.
    Underperforming = you'd be better off just holding TAO.
    Returns 50 if TAO history unavailable.
    """
    if not tao_prices or len(tao_prices) < REL_PERF_BARS + 1:
        return 50.0
    if len(subnet_prices) < REL_PERF_BARS + 1:
        return 50.0

    n = REL_PERF_BARS
    subnet_ret = (subnet_prices[-1] - subnet_prices[-(n + 1)]) / subnet_prices[-(n + 1)] \
        if subnet_prices[-(n + 1)] != 0 else 0.0
    tao_ret = (tao_prices[-1] - tao_prices[-(n + 1)]) / tao_prices[-(n + 1)] \
        if tao_prices[-(n + 1)] != 0 else 0.0

    alpha = subnet_ret - tao_ret

    # Map: -20% alpha → 0, 0% alpha → 50, +20% alpha → 100
    score = 50.0 + (alpha / 0.20) * 50.0
    return max(0.0, min(100.0, score))


# ─────────────────────────────────────────────────────────────────────────────
# Take-profit detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_take_profit(pct_from_ema: float, pct_from_high: float, markov_regime: str) -> list[str]:
    flags = []
    if pct_from_ema >= TP_STRONG_PCT:
        flags.append(f"TAKE_PROFIT_STRONG — {pct_from_ema:.0%} above EMA, consider full trim")
    elif pct_from_ema >= TP_WARN_PCT:
        flags.append(f"TAKE_PROFIT — {pct_from_ema:.0%} above EMA, consider partial trim")
    if pct_from_high is not None and abs(pct_from_high) < 0.03:
        flags.append("AT_RECENT_HIGH — price at peak, good exit zone")
    if markov_regime == "Bear" and pct_from_ema > 0.05:
        flags.append("REGIME_BEAR_WHILE_EXTENDED — trim before decline accelerates")
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Composite scorer — all 10 parameters
# ─────────────────────────────────────────────────────────────────────────────

# Entry score weights — pullback bias
ENTRY_W = {
    "p3_ema_slope":        0.20,
    "p4_pullback":         0.20,
    "p1_genie":            0.15,
    "p2_macro":            0.15,
    "p6_markov_persist":   0.10,
    "p7_ema_displacement": 0.08,
    "p5_pool_trajectory":  0.05,
    "p8_volume_trend":     0.04,
    "p9_data_maturity":    0.02,
    "p10_relative_perf":   0.01,
}

# Health score weights — stability bias
HEALTH_W = {
    "p6_markov_persist":   0.25,
    "p3_ema_slope":        0.20,
    "p1_genie":            0.20,
    "p10_relative_perf":   0.15,
    "p5_pool_trajectory":  0.10,
    "p8_volume_trend":     0.05,
    "p9_data_maturity":    0.03,
    "p7_ema_displacement": 0.02,
}

assert abs(sum(ENTRY_W.values()) - 1.0) < 0.001, "Entry weights must sum to 1.0"
assert abs(sum(HEALTH_W.values()) - 1.0) < 0.001, "Health weights must sum to 1.0"


def score_subnet(
    metrics: SubnetMetrics,
    macro: TaoMacroState,
    tao_prices: Optional[list[float]] = None,
) -> SubnetScore:
    """Score one subnet across all 10 parameters."""
    alerts: list[str] = []

    prices = np.array(metrics.price_history, dtype=float)

    # ── Parameter 1: Genie ───────────────────────────────────────────────────
    s_p1 = p1_score_genie(metrics.genie_score)
    
        

    # ── Parameter 2: Macro alignment ─────────────────────────────────────────
    s_p2 = p2_score_macro(macro)

    # ── Parameter 3: EMA slope ───────────────────────────────────────────────
    s_p3, ema_slope = p3_score_ema_slope(prices)

    # ── Parameter 7: EMA displacement rate (needed before p4) ────────────────
    s_p7, pct_from_ema = p7_score_ema_displacement_rate(prices)

    # ── Parameter 4: Pullback quality ────────────────────────────────────────
    s_p4, pct_from_high = p4_score_pullback(prices, pct_from_ema)

    # ── Parameter 5: Pool trajectory ─────────────────────────────────────────
    s_p5, pool_dir = p5_score_pool_trajectory(metrics.pool_history, metrics.pool_depth)

    # ── Parameter 6: Markov persistence ──────────────────────────────────────
    markov = _run_markov(metrics.price_history, metrics.timestamps, SUBNET_WINDOW, SUBNET_THRESHOLD)
    s_p6 = p6_score_markov_persistence(markov)

    if markov:
        markov_regime    = markov["current_regime"]
        markov_signal    = markov["signal"]
        markov_persist   = markov["persistence"]
        markov_available = True
        transition_matrix = markov["transition_matrix"]
        if markov_regime == "Bear":
            alerts.append("MARKOV_BEAR_REGIME")
    else:
        markov_regime    = "Unknown"
        markov_signal    = 0.0
        markov_persist   = 0.0
        markov_available = False
        transition_matrix = None
        alerts.append("MARKOV_INSUFFICIENT_DATA")

    # ── Parameter 8: Volume trend ─────────────────────────────────────────────
    s_p8 = p8_score_volume_trend(metrics.volume_history, metrics.volume_24h)

    # ── Parameter 9: Data maturity ───────────────────────────────────────────
    s_p9 = p9_score_data_maturity(len(metrics.price_history))

    # ── Parameter 10: Relative performance vs TAO ────────────────────────────
    s_p10 = p10_score_relative_performance(metrics.price_history, tao_prices or [])

    # ── Take-profit detection ─────────────────────────────────────────────────
    tp_flags = detect_take_profit(pct_from_ema, pct_from_high, markov_regime)

    # ── Entry flags ───────────────────────────────────────────────────────────
    entry_flags: list[str] = []
    if s_p4 >= 80:
        entry_flags.append("PULLBACK_ENTRY — ideal pullback zone")
    elif s_p4 <= 20:
        entry_flags.append("CHASING — near recent high, wait for pullback")
    if macro.regime == MacroRegime.BEAR:
        entry_flags.insert(0, "MACRO_BEAR — no new entries")
    elif macro.regime == MacroRegime.SIDEWAYS:
        entry_flags.insert(0, "MACRO_SIDEWAYS — cautious entry only")

    # ── pct changes for display ───────────────────────────────────────────────
    pct_24h = float((prices[-1] - prices[-2]) / prices[-2]) if len(prices) >= 2 and prices[-2] != 0 else None
    pct_7d  = float((prices[-1] - prices[-8]) / prices[-8]) if len(prices) >= 8 and prices[-8] != 0 else None

    # ── Pack parameter scores ─────────────────────────────────────────────────
    params = ParameterScores(
        p1_genie=round(s_p1, 1),
        p2_macro=round(s_p2, 1),
        p3_ema_slope=round(s_p3, 1),
        p4_pullback=round(s_p4, 1),
        p5_pool_trajectory=round(s_p5, 1),
        p6_markov_persist=round(s_p6, 1),
        p7_ema_displacement=round(s_p7, 1),
        p8_volume_trend=round(s_p8, 1),
        p9_data_maturity=round(s_p9, 1),
        p10_relative_perf=round(s_p10, 1),
    )

    # ── Weighted composite scores ─────────────────────────────────────────────
    pmap = {
        "p1_genie":          s_p1,
        "p2_macro":          s_p2,
        "p3_ema_slope":      s_p3,
        "p4_pullback":       s_p4,
        "p5_pool_trajectory":s_p5,
        "p6_markov_persist": s_p6,
        "p7_ema_displacement":s_p7,
        "p8_volume_trend":   s_p8,
        "p9_data_maturity":  s_p9,
        "p10_relative_perf": s_p10,
    }

    entry_score  = sum(ENTRY_W[k]  * pmap[k] for k in ENTRY_W)
    health_score = sum(HEALTH_W[k] * pmap[k] for k in HEALTH_W)

    # Macro suppression on entry
    if macro.regime == MacroRegime.BEAR:
        entry_score *= 0.3
    elif macro.regime == MacroRegime.SIDEWAYS:
        entry_score *= 0.7
    elif macro.regime == MacroRegime.UNKNOWN:
        entry_score *= 0.5

    return SubnetScore(
        subnet_id=metrics.subnet_id,
        name=metrics.name,
        entry_score=round(entry_score, 2),
        health_score=round(health_score, 2),
        params=params,
        markov_regime=markov_regime,
        markov_signal=round(markov_signal, 4),
        markov_persistence=round(markov_persist, 3),
        markov_available=markov_available,
        pct_from_ema=round(pct_from_ema, 4),
        pct_from_recent_high=round(pct_from_high, 4) if pct_from_high is not None else None,
        ema_slope_pct=round(ema_slope, 4),
        pool_depth_trending=pool_dir,
        relative_perf_vs_tao=round(s_p10 / 50.0 - 1.0, 4),  # store as actual alpha
        pct_change_24h=round(pct_24h, 4) if pct_24h is not None else None,
        pct_change_7d=round(pct_7d, 4) if pct_7d is not None else None,
        token_price=metrics.token_price,
        pool_depth=metrics.pool_depth,
        genie_score_raw=metrics.genie_score,
        transition_matrix=transition_matrix,
        alert_flags=alerts,
        take_profit_flags=tp_flags,
        entry_flags=entry_flags,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Full scoring cycle
# ─────────────────────────────────────────────────────────────────────────────

def run_scoring_cycle(
    all_subnets: list[SubnetMetrics],
    tao_price_history: Optional[list[float]] = None,
    tao_timestamps: Optional[list[str]] = None,
    top_n: int = 10,
    macro: Optional[TaoMacroState] = None,
) -> ScoringResult:
    # Accept pre-computed macro (from tao_macro.json) or compute from raw prices.
    # Pre-computed path is preferred — avoids re-running Markov on every cycle.
    if macro is None:
        macro = compute_tao_macro(tao_price_history or [], tao_timestamps)
    scored: list[SubnetScore] = []
    filtered_out: list[dict] = []

    for m in all_subnets:
        f = apply_pre_filters(m)
        if f == FilterResult.PASS:
            scored.append(score_subnet(m, macro, tao_price_history))
        else:
            filtered_out.append({
                "subnet_id": m.subnet_id, "name": m.name,
                "reason": f.value, "token_price": m.token_price,
                "genie_score": m.genie_score, "pool_depth": m.pool_depth,
            })

    ranked_entry  = sorted(scored, key=lambda s: s.entry_score,  reverse=True)
    ranked_health = sorted(scored, key=lambda s: s.health_score, reverse=True)

    return ScoringResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        macro=macro,
        total_subnets=len(all_subnets),
        passed_filters=len(scored),
        failed_filters=len(filtered_out),
        ranked_by_entry=ranked_entry,
        ranked_by_health=ranked_health,
        filtered_out=filtered_out,
        top_n=top_n,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Strategy comparison test
# ─────────────────────────────────────────────────────────────────────────────

def _v2_momentum_score(prices: np.ndarray) -> float:
    """Phase 2 (old) composite score — momentum chasing.
    Recreated exactly to compare against v4.
    """
    if len(prices) < 3:
        return 50.0
    # EMA position (old: reward being above EMA)
    ema = _ema(prices, min(72, len(prices) - 1))
    pct_above = (prices[-1] - ema[-1]) / ema[-1] if ema[-1] != 0 else 0.0
    trend_sub = max(0.0, min(100.0, 50.0 + (pct_above / 0.20) * 50.0))

    # Momentum (old: reward 24h + 7d positive change)
    pct_24h = (prices[-1] - prices[-2]) / prices[-2] if len(prices) >= 2 and prices[-2] != 0 else 0.0
    pct_7d  = (prices[-1] - prices[-8]) / prices[-8] if len(prices) >= 8 and prices[-8] != 0 else pct_24h
    blended = 0.4 * pct_24h + 0.6 * pct_7d
    mom_sub = max(0.0, min(100.0, 50.0 + (blended / 0.15) * 50.0))

    # Old composite (simplified — genie and pool held constant here)
    return 0.55 * trend_sub + 0.45 * mom_sub


def _v4_entry_score_raw(prices: np.ndarray) -> float:
    """Phase 4 (new) entry signal — pullback bias.
    Simplified version using only price-derivable parameters for comparison.
    """
    s_p3, ema_slope = p3_score_ema_slope(prices)
    s_p7, pct_from_ema = p7_score_ema_displacement_rate(prices)
    s_p4, _ = p4_score_pullback(prices, pct_from_ema)
    # Weighted by entry weights (price-only params)
    total_w = ENTRY_W["p3_ema_slope"] + ENTRY_W["p4_pullback"] + ENTRY_W["p7_ema_displacement"]
    score = (ENTRY_W["p3_ema_slope"] * s_p3
             + ENTRY_W["p4_pullback"] * s_p4
             + ENTRY_W["p7_ema_displacement"] * s_p7) / total_w
    return score


def run_comparison_test(
    metrics: SubnetMetrics,
    hold_bars: int = 7,
    min_history: int = 40,
    signal_threshold_v2: float = 65.0,   # v2: buy when momentum score > this
    signal_threshold_v4: float = 65.0,   # v4: buy when pullback score > this
) -> dict:
    """Walk-forward comparison of Phase 2 (momentum) vs Phase 4 (pullback).

    At each bar t from min_history onward:
      - Compute v2 score using only prices[:t]
      - Compute v4 score using only prices[:t]
      - If score > threshold, record forward return over hold_bars
      - Compare: which strategy produces better average forward returns?

    Returns a dict with side-by-side stats for both strategies.
    """
    prices = np.array(metrics.price_history, dtype=float)
    n = len(prices)

    if n < min_history + hold_bars:
        return {
            "subnet_id": metrics.subnet_id,
            "name": metrics.name,
            "error": f"insufficient data: {n} bars, need {min_history + hold_bars}",
        }

    v2_returns: list[float] = []
    v4_returns: list[float] = []
    random_returns: list[float] = []

    for t in range(min_history, n - hold_bars):
        window = prices[:t]
        fwd = (prices[t + hold_bars] - prices[t]) / prices[t] if prices[t] != 0 else 0.0
        random_returns.append(fwd)

        v2 = _v2_momentum_score(window)
        v4 = _v4_entry_score_raw(window)

        if v2 >= signal_threshold_v2:
            v2_returns.append(fwd)
        if v4 >= signal_threshold_v4:
            v4_returns.append(fwd)

    def _stats(returns: list[float], label: str) -> dict:
        if not returns:
            return {"label": label, "n": 0,
                    "mean_ret_pct": None, "median_ret_pct": None,
                    "win_rate_pct": None, "sharpe_approx": None}
        arr = np.array(returns)
        mean = float(arr.mean())
        std  = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
        sharpe = (mean / std * np.sqrt(252 / hold_bars)) if std > 0 else float("nan")
        return {
            "label": label,
            "n": len(arr),
            "mean_ret_pct":   round(mean * 100, 2),
            "median_ret_pct": round(float(np.median(arr)) * 100, 2),
            "win_rate_pct":   round(float((arr > 0).mean()) * 100, 1),
            "sharpe_approx":  round(sharpe, 3),
            "best_pct":       round(float(arr.max()) * 100, 2),
            "worst_pct":      round(float(arr.min()) * 100, 2),
        }

    v2_stats     = _stats(v2_returns,     "v2_momentum (old)")
    v4_stats     = _stats(v4_returns,     "v4_pullback  (new)")
    rand_stats   = _stats(random_returns, "random_baseline")

    # Verdict
    verdict = "INCONCLUSIVE — insufficient signal windows"
    if v2_stats["n"] >= 5 and v4_stats["n"] >= 5 and v2_stats["mean_ret_pct"] and v4_stats["mean_ret_pct"]:
        if v4_stats["mean_ret_pct"] > v2_stats["mean_ret_pct"]:
            diff = v4_stats["mean_ret_pct"] - v2_stats["mean_ret_pct"]
            verdict = f"v4 WINS by {diff:.2f}% mean return per trade"
        elif v2_stats["mean_ret_pct"] > v4_stats["mean_ret_pct"]:
            diff = v2_stats["mean_ret_pct"] - v4_stats["mean_ret_pct"]
            verdict = f"v2 WINS by {diff:.2f}% mean return per trade"
        else:
            verdict = "DRAW — similar mean returns"

    return {
        "subnet_id":    metrics.subnet_id,
        "name":         metrics.name,
        "bars_tested":  n,
        "test_windows": len(random_returns),
        "hold_bars":    hold_bars,
        "v2_threshold": signal_threshold_v2,
        "v4_threshold": signal_threshold_v4,
        "v2_momentum":  v2_stats,
        "v4_pullback":  v4_stats,
        "random":       rand_stats,
        "verdict":      verdict,
        "note": ("Walk-forward, no lookahead. "
                 f"{len(random_returns)} windows on {n} bars. "
                 "Directional evidence only — not statistically conclusive."),
    }


def print_comparison(result: dict) -> None:
    """Pretty-print a comparison test result."""
    if "error" in result:
        print(f"  SN{result['subnet_id']} ({result['name']}): {result['error']}")
        return

    print(f"\n{'='*62}")
    print(f"  SN{result['subnet_id']} ({result['name']}) — {result['bars_tested']} bars, "
          f"{result['test_windows']} test windows, hold={result['hold_bars']} bars")
    print(f"{'='*62}")
    print(f"  {'Strategy':<22} {'N':>5} {'Mean%':>8} {'Median%':>8} {'Win%':>7} {'Sharpe':>8}")
    print(f"  {'-'*62}")
    for key in ("v2_momentum", "v4_pullback", "random"):
        s = result[key]
        if s["n"] == 0:
            print(f"  {s['label']:<22} {'0':>5} {'—':>8} {'—':>8} {'—':>7} {'—':>8}")
        else:
            print(f"  {s['label']:<22} {s['n']:>5} "
                  f"{s['mean_ret_pct']:>7.2f}% "
                  f"{s['median_ret_pct']:>7.2f}% "
                  f"{s['win_rate_pct']:>6.1f}% "
                  f"{s['sharpe_approx'] if s['sharpe_approx'] is not None else '—':>8}")
    print(f"\n  ▶ Verdict: {result['verdict']}")
    print(f"  Note: {result['note']}")


# ─────────────────────────────────────────────────────────────────────────────
# Telegram formatter
# ─────────────────────────────────────────────────────────────────────────────

def format_telegram_alert(result, current_holdings=None, macro_header=None):
    """Holdings-first portfolio report.

    Sections: how your holdings are doing, what to trim/take-profit, what to
    review/exit, and a non-chasing (pullback) buy read gated by TAO macro.
    Drives BOTH the scheduled report (run_scoring.py) and on-demand /status.

    `macro_header` is accepted for backward-compat but intentionally ignored —
    the macro line is built from result.macro so there is no duplicate header.
    """
    holdings = set(current_holdings or [])

    def gini_str(g):
        # 0.5 is the placeholder sentinel from taostats_fetch (no metagraph yet).
        return "n/a*" if abs(g - 0.5) < 1e-9 else f"{g:.2f}"

    def conc_tag(g):
        return "  ⚠️CONCENTRATION" if (abs(g - 0.5) > 1e-9 and g >= 0.85) else ""

    def pct(x):
        return "n/a" if x is None else f"{x * 100:+.0f}%"

    def dot(r):
        return {"Bull": "🟢", "Bear": "🔴", "Sideways": "⚪"}.get(r, "❔")

    m = result.macro
    by_id = {s.subnet_id: s for s in result.ranked_by_entry}
    filtered_by_id = {f["subnet_id"]: f for f in result.filtered_out}

    L = [
        "📊 TAO MONITOR — Portfolio Report",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🌐 TAO macro: {m.regime.value} (signal {m.signal:+.2f})",
        f"   {m.strategy_mode}",
        "",
    ]

    # 1. Holdings first — weakest health at top so problems surface.
    held_scores = sorted(
        (by_id[h] for h in holdings if h in by_id),
        key=lambda s: s.health_score,
    )
    L.append("💼 YOUR HOLDINGS  (health 0-100)")
    if not held_scores and not any(h in filtered_by_id for h in holdings):
        L.append("  (no holdings data this cycle)")
    for s in held_scores:
        L.append(
            f"  SN{s.subnet_id} {s.name} [{s.health_score:.0f}] "
            f"{dot(s.markov_regime)}{s.markov_regime} "
            f"{s.token_price:.4f}τ  24h:{pct(s.pct_change_24h)} 7d:{pct(s.pct_change_7d)}  "
            f"Gini:{gini_str(s.genie_score_raw)}{conc_tag(s.genie_score_raw)}"
        )
    for h in holdings:
        if h in filtered_by_id:
            ff = filtered_by_id[h]
            L.append(f"  SN{h} {ff['name']} [--]  ⛔ {ff['reason']}")
    L.append("")

    # 2. Trim / take profit — only when GENUINELY extended above EMA.
    #    A take_profit_flag alone isn't enough: require real strength
    #    (>= +15% over EMA) so we never say "trim into strength" on a holding
    #    that's actually below its trend (e.g. a bleeding bear-regime name).
    TRIM_MIN_OVER_EMA = 0.15
    trims = []
    for s in held_scores:
        if (s.take_profit_flags
                and s.pct_from_ema is not None
                and s.pct_from_ema >= TRIM_MIN_OVER_EMA):
            trims.append(
                f"  SN{s.subnet_id} {s.name} — +{s.pct_from_ema * 100:.0f}% over EMA, trim into strength"
            )
    if trims:
        L.append("🔻 TRIM / TAKE PROFIT")
        L.extend(trims)
        L.append("")

    # 3. Review / exit — failed filters + downtrend/bear on holdings.
    exits = []
    for h in holdings:
        if h in filtered_by_id:
            exits.append(f"  SN{h} {filtered_by_id[h]['name']} — {filtered_by_id[h]['reason']}")
    for s in held_scores:
        for flag in s.alert_flags:
            if flag in ("MARKOV_BEAR_REGIME", "BELOW_EMA_DOWNTREND"):
                exits.append(f"  SN{s.subnet_id} {s.name} — {flag.lower()} (health {s.health_score:.0f})")
    if exits:
        L.append("⚠️ REVIEW / EXIT")
        L.extend(exits)
        L.append("")

    # 4. Buy — non-chasing, macro-gated. CHASING-tagged subnets excluded.
    if m.regime == MacroRegime.BEAR:
        L.append("🟢 BUY: Bear macro — capital preservation, no new entries.")
    else:
        candidates = [
            s for s in result.ranked_by_entry
            if s.subnet_id not in holdings
            and not any("CHASING" in fl for fl in s.entry_flags)
        ]
        candidates.sort(key=lambda s: s.entry_score, reverse=True)
        if m.regime == MacroRegime.BULL:
            L.append("🟢 BUY THE PULLBACK")
        else:
            L.append("👀 WATCH ONLY — hold, don't add (macro not bullish)")
        shown = 0
        for s in candidates[:result.top_n]:
            tag = "pullback" if any("PULLBACK" in fl for fl in s.entry_flags) else "neutral"
            offhigh = f"{s.pct_from_recent_high * 100:+.0f}% off high" if s.pct_from_recent_high is not None else ""
            L.append(
                f"  {shown + 1}. SN{s.subnet_id} {s.name} — entry {s.entry_score:.0f} "
                f"[{tag}] {offhigh}  Gini:{gini_str(s.genie_score_raw)}{conc_tag(s.genie_score_raw)}"
            )
            shown += 1
        if shown == 0:
            L.append("  Nothing in a clean pullback zone — no chases.")
    L.append("")

    L.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    L.append(f"⏰ {result.timestamp[:16]}  ·  {result.passed_filters}/{result.total_subnets} passing filters")
    L.append("* Gini n/a = real concentration not yet fetched (no fake 0.50).")
    return "\n".join(L)


def _make_demo_subnets() -> tuple[list[SubnetMetrics], list[float]]:
    np.random.seed(42)
    tao = list(np.cumsum(np.random.normal(0.003, 0.02, 200)) + 0.5)

    def _trend_then_pullback(n_up, n_down, start=0.01, up_drift=0.004, down_drift=-0.008):
        up   = list(np.cumsum(np.random.normal(up_drift,   0.012, n_up))   + start)
        down = list(np.cumsum(np.random.normal(down_drift, 0.010, n_down)) + up[-1])
        return up + down

    def _steady_uptrend(n, start=0.01, drift=0.002):
        return list(np.cumsum(np.random.normal(drift, 0.010, n)) + start)

    def _downtrend(n, start=0.03, drift=-0.003):
        return list(np.cumsum(np.random.normal(drift, 0.010, n)) + start)

    pool_trend_up   = [100, 110, 120, 130, 145]
    pool_trend_down = [300, 280, 250, 220, 190]
    pool_flat       = [80, 82, 79, 81, 80]

    subnets = [
        # Good pullback setup — uptrend then 15% dip, pool growing
        SubnetMetrics(
            subnet_id=4, name="Targon",
            token_price=0.015, pool_depth=145.0, genie_score=0.45,
            price_history=_trend_then_pullback(160, 20),
            timestamps=[],
            volume_history=[50, 55, 60, 70, 80],
            pool_history=pool_trend_up,
        ),
        # Chasing — at all-time high, no pullback
        SubnetMetrics(
            subnet_id=116, name="Unknown",
            token_price=0.025, pool_depth=200.0, genie_score=0.50,
            price_history=_steady_uptrend(150, drift=0.005),
            timestamps=[],
            volume_history=[40, 45, 42, 48, 50],
            pool_history=pool_flat,
        ),
        # Extended above EMA — take profit candidate
        SubnetMetrics(
            subnet_id=18, name="Zeus",
            token_price=0.018, pool_depth=300.0, genie_score=0.40,
            price_history=_steady_uptrend(150, drift=0.001)
                         + list(np.cumsum(np.random.normal(0.015, 0.008, 20)) + 0.02),
            timestamps=[],
            volume_history=[60, 65, 70, 90, 120],  # volume expanding on spike
            pool_history=pool_trend_up,
        ),
        # Downtrend, pool shrinking — weak hold
        SubnetMetrics(
            subnet_id=68, name="NOVA",
            token_price=0.022, pool_depth=190.0, genie_score=0.60,
            price_history=_downtrend(150),
            timestamps=[],
            volume_history=[100, 85, 70, 55, 40],  # volume contracting
            pool_history=pool_trend_down,
        ),
        # Fail genie
        SubnetMetrics(
            subnet_id=62, name="Ridges",
            token_price=0.012, pool_depth=200.0, genie_score=0.91,
            price_history=_steady_uptrend(60),
            timestamps=[],
        ),
        # Underperforming TAO, low alpha
        SubnetMetrics(
            subnet_id=51, name="lium.io",
            token_price=0.008, pool_depth=80.0, genie_score=0.55,
            price_history=_steady_uptrend(120, drift=0.001),
            timestamps=[],
            volume_history=[30, 30, 31, 29, 30],
            pool_history=pool_flat,
        ),
    ]
    return subnets, tao


if __name__ == "__main__":
    print("=" * 62)
    print("Subnet Scoring Engine v4 — 10 Parameter Framework")
    print("=" * 62)

    subnets, tao_history = _make_demo_subnets()
    holdings = [4, 18, 68, 51]

    result = run_scoring_cycle(subnets, tao_price_history=tao_history, top_n=5)

    print("\n" + format_telegram_alert(result, current_holdings=holdings))

    print("\n" + "=" * 62)
    print("PARAMETER BREAKDOWN (all scored subnets)")
    print("=" * 62)
    header = f"  {'SN':<18} {'P1':>5} {'P2':>5} {'P3':>5} {'P4':>5} {'P5':>5} {'P6':>5} {'P7':>5} {'P8':>5} {'P9':>5} {'P10':>5} | {'Entry':>6} {'Health':>7}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for s in sorted(result.ranked_by_entry, key=lambda x: x.entry_score, reverse=True):
        p = s.params
        print(
            f"  SN{s.subnet_id:<3} ({s.name:<10}) "
            f"{p.p1_genie:>5.0f} {p.p2_macro:>5.0f} {p.p3_ema_slope:>5.0f} "
            f"{p.p4_pullback:>5.0f} {p.p5_pool_trajectory:>5.0f} {p.p6_markov_persist:>5.0f} "
            f"{p.p7_ema_displacement:>5.0f} {p.p8_volume_trend:>5.0f} {p.p9_data_maturity:>5.0f} "
            f"{p.p10_relative_perf:>5.0f} | {s.entry_score:>6.1f} {s.health_score:>7.1f}"
        )
    print("  " + "-" * (len(header) - 2))
    print(f"  {'Col':18} P1=Genie P2=Macro P3=EMAslope P4=Pullback P5=PoolTrend")
    print(f"  {'':18} P6=MarkovPersist P7=EMAdisp P8=Volume P9=Maturity P10=Alpha")

    print("\n" + "=" * 62)
    print("STRATEGY COMPARISON TEST: v2 Momentum vs v4 Pullback")
    print("=" * 62)
    print("  (walk-forward, no lookahead, 7-bar hold)")
    for m in subnets:
        if m.genie_score < MAX_GENIE_SCORE:  # only test non-filtered
            r = run_comparison_test(m, hold_bars=7, min_history=40)
            print_comparison(r)

    print("\n" + "=" * 62)
    print("FILTERED OUT")
    print("=" * 62)
    for f in result.filtered_out:
        print(f"  SN{f['subnet_id']} ({f['name']}) — {f['reason']}")


def to_json(result) -> str:
    """Serialize full scoring result to JSON for dashboard/API."""
    from dataclasses import asdict
    import json
    macro = result.macro
    data = {
        "timestamp": result.timestamp,
        "macro": {
            "regime": macro.regime.value if (macro and macro.available) else "Unknown",
            "signal": round(macro.signal, 4) if macro else 0.0,
            "bull_prob": round(macro.bull_prob, 4) if macro else None,
            "bear_prob": round(macro.bear_prob, 4) if macro else None,
            "strategy_mode": macro.strategy_mode if macro else "",
            "available": bool(macro.available) if macro else False,
        },
        "summary": {
            "total_subnets": result.total_subnets,
            "passed_filters": result.passed_filters,
            "failed_filters": result.failed_filters,
        },
        "ranked": [asdict(s) for s in result.ranked_by_entry],
        "filtered_out": result.filtered_out,
    }
    return json.dumps(data, indent=2)
