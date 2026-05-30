"""
Subnet Scoring Engine — TAO Monitor Phase 2
============================================
Combines Roan's Markov regime detection (via markov_regime.py) with
Siam Kidd's pre-filter framework for Bittensor subnet ranking.

Designed to drop into the existing TAO Monitor (Railway + Telegram).

Data flow:
  1. Fetch per-subnet metrics (price, pool depth, Genie, price history)
  2. Apply hard pre-filters (pass/fail gates from Siam's framework)
  3. Run Markov regime detection on survivors
  4. Score each subnet 0-100 composite
  5. Rank and output recommended allocations

Adaptations from stock-market Markov to subnet data:
  - Shorter lookback window (7 vs 20 days) — subnets have less history
  - Wider threshold (±10% vs ±5%) — subnet volatility is much higher
  - Lower min_train (60 vs 252) — many subnets < 1yr old
  - Graceful fallback when insufficient data for Markov (score on other factors)
  - No HMM by default — too little data for most subnets to converge

Dependencies: numpy, pandas (already in markov_regime.py)
Optional: markov_regime (import for full analyze(), or use embedded core)
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
# Constants — subnet-tuned defaults
# ─────────────────────────────────────────────────────────────────────────────

# Markov params adapted for subnet volatility
SUBNET_WINDOW = 7          # 7-day rolling return (vs 20 for stocks)
SUBNET_THRESHOLD = 0.10    # ±10% regime boundary (vs ±5% for stocks)
SUBNET_MIN_TRAIN = 60      # ~2 months minimum history (vs 252 for stocks)

# Filter thresholds
# Note: Siam's original MAX_TOKEN_PRICE=0.04 targets small unknown subnets.
# Raised here to accommodate established conviction holds (SN0, SN4, SN51 etc.)
# which naturally have higher prices and deeper pools.
# TODO Phase 2: split into two profiles — conviction holds vs active rotation.
MAX_TOKEN_PRICE = 1.0      # TAO — no upper price limit for established subnets
MIN_POOL_DEPTH = 5.0       # TAO — below this, too illiquid
MAX_POOL_DEPTH = 500000.0  # TAO — raised to include all established subnets
MAX_GENIE_SCORE = 0.85     # concentration — above this, manipulation risk

# Scoring weights (sum to 1.0)
WEIGHT_MARKOV_SIGNAL = 0.30    # bull-bear probability differential
WEIGHT_TREND_STRENGTH = 0.25   # price vs EMA position
WEIGHT_GENIE = 0.20            # lower concentration = better
WEIGHT_MOMENTUM = 0.15         # 24h + 7d price change
WEIGHT_POOL_DEPTH = 0.10       # sweet spot scoring

STATES = ["Bear", "Sideways", "Bull"]


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

class FilterResult(Enum):
    PASS = "pass"
    FAIL_PRICE = "fail_price_too_high"
    FAIL_POOL_MIN = "fail_pool_too_shallow"
    FAIL_POOL_MAX = "fail_pool_too_deep"
    FAIL_GENIE = "fail_genie_concentrated"
    FAIL_NO_DATA = "fail_insufficient_data"


@dataclass
class SubnetMetrics:
    """Raw metrics fetched for a single subnet."""
    subnet_id: int
    name: str
    token_price: float              # in TAO
    pool_depth: float               # in TAO
    genie_score: float              # 0-1 concentration
    price_history: list[float]      # recent closes (newest last), ideally 72+ bars
    timestamps: list[str]           # ISO timestamps matching price_history
    volume_24h: float = 0.0         # optional, in TAO
    volume_7d: float = 0.0          # optional


@dataclass
class SubnetScore:
    """Scored output for a single subnet that passed pre-filters."""
    subnet_id: int
    name: str
    composite_score: float          # 0-100
    markov_signal: float            # -1 to +1 (bull_p - bear_p)
    markov_regime: str              # "Bull" / "Bear" / "Sideways"
    trend_score: float              # 0-100 sub-score
    genie_score_raw: float          # original 0-1
    genie_score_inverted: float     # 0-100 (lower genie = higher score)
    momentum_score: float           # 0-100 sub-score
    pool_depth_score: float         # 0-100 sub-score
    token_price: float
    pool_depth: float
    pct_change_24h: Optional[float] = None
    pct_change_7d: Optional[float] = None
    markov_available: bool = True
    transition_matrix: Optional[list] = None
    alert_flags: list[str] = field(default_factory=list)


@dataclass
class ScoringResult:
    """Full output of one scoring cycle across all subnets."""
    timestamp: str
    total_subnets: int
    passed_filters: int
    failed_filters: int
    ranked: list[SubnetScore]
    filtered_out: list[dict]        # subnet_id + reason
    top_n: int = 10


# ─────────────────────────────────────────────────────────────────────────────
# Markov core (embedded from markov_regime.py, adapted for subnets)
# ─────────────────────────────────────────────────────────────────────────────

def label_regimes(
    close: pd.Series,
    window: int = SUBNET_WINDOW,
    threshold: float = SUBNET_THRESHOLD,
) -> pd.Series:
    """Label each period Bull/Bear/Sideways from rolling return.
    Adapted: wider threshold + shorter window for subnet volatility.
    """
    rolling_return = close.pct_change(window)
    labels = pd.Series(1, index=close.index, dtype=int)
    labels[rolling_return > threshold] = 2   # Bull
    labels[rolling_return < -threshold] = 0  # Bear
    return labels.loc[rolling_return.notna()]


def build_transition_matrix(labels: pd.Series) -> np.ndarray:
    """MLE 3x3 transition matrix from label sequence."""
    counts = np.zeros((3, 3), dtype=float)
    arr = np.asarray(labels, dtype=int)
    for i in range(len(arr) - 1):
        counts[arr[i], arr[i + 1]] += 1.0
    row_sums = counts.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0
    return counts / row_sums


def stationary_distribution(matrix: np.ndarray) -> np.ndarray:
    """Long-run regime mix (left eigenvector for eigenvalue 1)."""
    eigvals, eigvecs = np.linalg.eig(matrix.T)
    idx = np.argmin(np.abs(eigvals - 1.0))
    vec = np.abs(np.real(eigvecs[:, idx]))
    return vec / vec.sum()


def subnet_markov_analyze(
    price_history: list[float],
    timestamps: list[str],
    window: int = SUBNET_WINDOW,
    threshold: float = SUBNET_THRESHOLD,
    min_train: int = SUBNET_MIN_TRAIN,
) -> Optional[dict]:
    """Run Markov regime detection on a subnet's price history.

    Returns None if insufficient data. No walk-forward backtest
    (subnets rarely have enough history). No HMM (same reason).
    """
    if len(price_history) < window + 2:
        return None

    idx = pd.to_datetime(timestamps)
    close = pd.Series(price_history, index=idx, dtype=float).dropna()

    if len(close) < window + 2:
        return None

    labels = label_regimes(close, window=window, threshold=threshold)
    if len(labels) < 2:
        return None

    P = build_transition_matrix(labels)
    pi = stationary_distribution(P)

    current_state = int(labels.iloc[-1])
    bull_p = float(P[current_state, 2])
    bear_p = float(P[current_state, 0])

    return {
        "current_regime": STATES[current_state],
        "signal": bull_p - bear_p,
        "next_bull": bull_p,
        "next_bear": bear_p,
        "next_sideways": float(P[current_state, 1]),
        "persistence": {
            "bear": float(P[0, 0]),
            "sideways": float(P[1, 1]),
            "bull": float(P[2, 2]),
        },
        "stationary": {
            "bear": float(pi[0]),
            "sideways": float(pi[1]),
            "bull": float(pi[2]),
        },
        "transition_matrix": [[float(x) for x in row] for row in P],
        "data_points": len(labels),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pre-filters (Siam's hard gates)
# ─────────────────────────────────────────────────────────────────────────────

def apply_pre_filters(
    metrics: SubnetMetrics,
    max_price: float = MAX_TOKEN_PRICE,
    min_pool: float = MIN_POOL_DEPTH,
    max_pool: float = MAX_POOL_DEPTH,
    max_genie: float = MAX_GENIE_SCORE,
) -> FilterResult:
    """Apply Siam's 4 hard pre-filters. Fail on ANY gate."""

    if len(metrics.price_history) < 3:  # hard minimum, independent of window
        return FilterResult.FAIL_NO_DATA
    if metrics.token_price >= max_price:
        return FilterResult.FAIL_PRICE
    if metrics.pool_depth < min_pool:
        return FilterResult.FAIL_POOL_MIN
    if metrics.pool_depth > max_pool:
        return FilterResult.FAIL_POOL_MAX
    if metrics.genie_score >= max_genie:
        return FilterResult.FAIL_GENIE
    return FilterResult.PASS


# ─────────────────────────────────────────────────────────────────────────────
# Individual scoring components (each returns 0-100)
# ─────────────────────────────────────────────────────────────────────────────

def score_markov_signal(signal: float) -> float:
    """Map Markov signal (-1 to +1) to 0-100.

    -1.0 → 0,  0.0 → 50,  +1.0 → 100
    """
    return max(0.0, min(100.0, (signal + 1.0) * 50.0))


def score_trend_strength(price_history: list[float], ema_period: int = 72) -> float:
    """Score based on current price position relative to EMA.

    Above EMA = bullish (score > 50). Further above = higher score.
    Below EMA = bearish (score < 50).

    Falls back to shorter EMA if insufficient data.
    """
    if len(price_history) < 3:
        return 50.0  # neutral

    prices = np.array(price_history, dtype=float)

    # Adaptive EMA period — use what data we have
    actual_period = min(ema_period, len(prices) - 1)
    if actual_period < 3:
        return 50.0

    # Calculate EMA
    alpha = 2.0 / (actual_period + 1)
    ema = np.empty_like(prices)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = alpha * prices[i] + (1 - alpha) * ema[i - 1]

    current_price = prices[-1]
    current_ema = ema[-1]

    if current_ema == 0:
        return 50.0

    # Distance from EMA as percentage
    pct_from_ema = (current_price - current_ema) / current_ema

    # Map: -20% below → 0, at EMA → 50, +20% above → 100
    # Clamped to 0-100
    score = 50.0 + (pct_from_ema / 0.20) * 50.0
    return max(0.0, min(100.0, score))


def score_genie(genie: float) -> float:
    """Invert Genie: lower concentration = higher score.

    0.0 → 100 (perfect distribution)
    0.85 → 0  (at the filter boundary)

    Note: subnets with genie >= 0.85 already filtered out, so this
    only scores within the passing range.
    """
    if genie <= 0.0:
        return 100.0
    if genie >= MAX_GENIE_SCORE:
        return 0.0
    return max(0.0, (1.0 - genie / MAX_GENIE_SCORE) * 100.0)


def score_momentum(price_history: list[float]) -> float:
    """Score based on recent price momentum (24h + 7d changes).

    Weights 24h change at 40%, 7d change at 60%.
    Maps roughly: -15% → 0, 0% → 50, +15% → 100.
    """
    if len(price_history) < 2:
        return 50.0

    prices = np.array(price_history, dtype=float)
    current = prices[-1]

    # 24h change (assume last bar is most recent)
    pct_24h = (current - prices[-2]) / prices[-2] if prices[-2] != 0 else 0.0

    # 7d change (7 bars back if available)
    if len(prices) >= 8:
        pct_7d = (current - prices[-8]) / prices[-8] if prices[-8] != 0 else 0.0
    else:
        pct_7d = pct_24h  # fallback

    # Weighted blend
    blended = 0.4 * pct_24h + 0.6 * pct_7d

    # Map: -15% → 0, 0% → 50, +15% → 100
    score = 50.0 + (blended / 0.15) * 50.0
    return max(0.0, min(100.0, score))


def score_pool_depth(
    depth: float,
    sweet_min: float = 20.0,
    sweet_max: float = 2000.0,
) -> float:
    """Score pool depth on a sweet-spot curve.

    Too shallow (< sweet_min) or too deep (> sweet_max) = lower score.
    Sweet spot is the middle range = 100.
    """
    if depth <= 0:
        return 0.0

    if sweet_min <= depth <= sweet_max:
        return 100.0

    if depth < sweet_min:
        # Linear ramp from MIN_POOL_DEPTH to sweet_min
        if depth <= MIN_POOL_DEPTH:
            return 0.0
        return ((depth - MIN_POOL_DEPTH) / (sweet_min - MIN_POOL_DEPTH)) * 100.0

    # depth > sweet_max: linear decay to MAX_POOL_DEPTH
    if depth >= MAX_POOL_DEPTH:
        return 0.0
    return ((MAX_POOL_DEPTH - depth) / (MAX_POOL_DEPTH - sweet_max)) * 100.0


# ─────────────────────────────────────────────────────────────────────────────
# Composite scorer
# ─────────────────────────────────────────────────────────────────────────────

def score_subnet(metrics: SubnetMetrics) -> SubnetScore:
    """Score a single subnet that has already passed pre-filters.

    Returns a SubnetScore with composite 0-100 and all sub-scores.
    """
    alerts: list[str] = []

    # Markov regime detection
    markov = subnet_markov_analyze(
        metrics.price_history,
        metrics.timestamps,
    )

    if markov is not None:
        markov_signal = markov["signal"]
        markov_regime = markov["current_regime"]
        markov_sub = score_markov_signal(markov_signal)
        markov_available = True
        transition_matrix = markov["transition_matrix"]

        # Alert: regime just shifted to Bear
        if markov_regime == "Bear":
            alerts.append("MARKOV_BEAR_REGIME")
    else:
        # Insufficient data — score Markov component as neutral
        markov_signal = 0.0
        markov_regime = "Unknown"
        markov_sub = 50.0
        markov_available = False
        transition_matrix = None
        alerts.append("MARKOV_INSUFFICIENT_DATA")

    # Trend strength (72 EMA based)
    trend_sub = score_trend_strength(metrics.price_history, ema_period=72)

    # Genie concentration (inverted)
    genie_sub = score_genie(metrics.genie_score)

    # Momentum (24h + 7d)
    momentum_sub = score_momentum(metrics.price_history)

    # Pool depth sweet spot
    pool_sub = score_pool_depth(metrics.pool_depth)

    # Compute pct changes for display
    prices = metrics.price_history
    pct_24h = None
    pct_7d = None
    if len(prices) >= 2 and prices[-2] != 0:
        pct_24h = (prices[-1] - prices[-2]) / prices[-2]
    if len(prices) >= 8 and prices[-8] != 0:
        pct_7d = (prices[-1] - prices[-8]) / prices[-8]

    # Weighted composite
    if markov_available:
        composite = (
            WEIGHT_MARKOV_SIGNAL * markov_sub
            + WEIGHT_TREND_STRENGTH * trend_sub
            + WEIGHT_GENIE * genie_sub
            + WEIGHT_MOMENTUM * momentum_sub
            + WEIGHT_POOL_DEPTH * pool_sub
        )
    else:
        # Redistribute Markov weight to trend + momentum when unavailable
        adjusted_trend = WEIGHT_TREND_STRENGTH + (WEIGHT_MARKOV_SIGNAL * 0.6)
        adjusted_momentum = WEIGHT_MOMENTUM + (WEIGHT_MARKOV_SIGNAL * 0.4)
        composite = (
            adjusted_trend * trend_sub
            + WEIGHT_GENIE * genie_sub
            + adjusted_momentum * momentum_sub
            + WEIGHT_POOL_DEPTH * pool_sub
        )

    # Additional alert flags
    if metrics.genie_score > 0.75:
        alerts.append("GENIE_APPROACHING_THRESHOLD")
    if trend_sub < 30:
        alerts.append("BELOW_EMA_DOWNTREND")

    return SubnetScore(
        subnet_id=metrics.subnet_id,
        name=metrics.name,
        composite_score=round(composite, 2),
        markov_signal=round(markov_signal, 4),
        markov_regime=markov_regime,
        trend_score=round(trend_sub, 2),
        genie_score_raw=metrics.genie_score,
        genie_score_inverted=round(genie_sub, 2),
        momentum_score=round(momentum_sub, 2),
        pool_depth_score=round(pool_sub, 2),
        token_price=metrics.token_price,
        pool_depth=metrics.pool_depth,
        pct_change_24h=round(pct_24h, 4) if pct_24h is not None else None,
        pct_change_7d=round(pct_7d, 4) if pct_7d is not None else None,
        markov_available=markov_available,
        transition_matrix=transition_matrix,
        alert_flags=alerts,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Full scoring cycle — runs across all subnets
# ─────────────────────────────────────────────────────────────────────────────

def run_scoring_cycle(
    all_subnets: list[SubnetMetrics],
    top_n: int = 10,
) -> ScoringResult:
    """Run one complete scoring cycle across all subnets.

    1. Apply pre-filters to all subnets
    2. Score survivors
    3. Rank by composite score descending
    4. Return full result with ranked list + filtered-out reasons
    """
    ranked: list[SubnetScore] = []
    filtered_out: list[dict] = []

    for metrics in all_subnets:
        result = apply_pre_filters(metrics)

        if result == FilterResult.PASS:
            score = score_subnet(metrics)
            ranked.append(score)
        else:
            filtered_out.append({
                "subnet_id": metrics.subnet_id,
                "name": metrics.name,
                "reason": result.value,
                "token_price": metrics.token_price,
                "genie_score": metrics.genie_score,
                "pool_depth": metrics.pool_depth,
            })

    # Sort by composite score descending
    ranked.sort(key=lambda s: s.composite_score, reverse=True)

    return ScoringResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_subnets=len(all_subnets),
        passed_filters=len(ranked),
        failed_filters=len(filtered_out),
        ranked=ranked,
        filtered_out=filtered_out,
        top_n=top_n,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram message formatter (matches Gordie-style output)
# ─────────────────────────────────────────────────────────────────────────────

def format_telegram_alert(
    result: ScoringResult,
    current_holdings: Optional[list[int]] = None,
) -> str:
    """Format scoring result as a Telegram message.

    current_holdings: list of subnet IDs currently staked.
    Flags holdings that now fail pre-filters or dropped in ranking.
    """
    lines = [
        "📊 TAO MONITOR — Scoring Update",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"🟢 {result.passed_filters}/{result.total_subnets} subnets passing filters",
        "",
    ]

    # Alerts section — holdings that fail filters
    alert_lines = []
    if current_holdings:
        for f in result.filtered_out:
            if f["subnet_id"] in current_holdings:
                alert_lines.append(
                    f"🔴 SN{f['subnet_id']} ({f['name']}) — {f['reason']}"
                )

        # Check holdings that passed but have alerts
        for s in result.ranked:
            if s.subnet_id in current_holdings and s.alert_flags:
                for flag in s.alert_flags:
                    alert_lines.append(
                        f"⚠️ SN{s.subnet_id} ({s.name}) — {flag}"
                    )

    if alert_lines:
        lines.append("🚨 ALERTS:")
        lines.extend(alert_lines)
        lines.append("")

    # Top opportunities
    top = result.ranked[:result.top_n]
    if top:
        lines.append(f"📈 TOP {len(top)} SUBNETS:")
        for i, s in enumerate(top, 1):
            held = " 📌" if current_holdings and s.subnet_id in current_holdings else ""
            markov_tag = f" [{s.markov_regime}]" if s.markov_available else ""
            mom = f" 24h:{s.pct_change_24h:+.1%}" if s.pct_change_24h is not None else ""
            lines.append(
                f"  {i}. SN{s.subnet_id} ({s.name}) — "
                f"Score: {s.composite_score:.0f}/100{markov_tag}"
                f" | Genie: {s.genie_score_raw:.2f}{mom}{held}"
            )
        lines.append("")

    # Rebalance suggestions
    if current_holdings:
        held_set = set(current_holdings)
        top_ids = {s.subnet_id for s in top}

        exit_candidates = held_set - top_ids
        enter_candidates = top_ids - held_set

        if exit_candidates:
            lines.append("🔄 REBALANCE SUGGESTIONS:")
            for sid in exit_candidates:
                # Find why — filtered out or just ranked low?
                filtered = next((f for f in result.filtered_out if f["subnet_id"] == sid), None)
                if filtered:
                    lines.append(f"  EXIT SN{sid} — {filtered['reason']}")
                else:
                    scored = next((s for s in result.ranked if s.subnet_id == sid), None)
                    if scored:
                        lines.append(
                            f"  REVIEW SN{sid} ({scored.name}) — "
                            f"Score: {scored.composite_score:.0f} (not in top {result.top_n})"
                        )
            lines.append("")

        if enter_candidates:
            lines.append("🟢 CONSIDER ENTERING:")
            for sid in enter_candidates:
                scored = next((s for s in result.ranked if s.subnet_id == sid), None)
                if scored:
                    lines.append(
                        f"  SN{sid} ({scored.name}) — Score: {scored.composite_score:.0f}"
                    )
            lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"⏰ {result.timestamp}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# JSON export for API / dashboard consumption
# ─────────────────────────────────────────────────────────────────────────────

def to_json(result: ScoringResult) -> str:
    """Serialize full scoring result to JSON for dashboard/API."""
    data = {
        "timestamp": result.timestamp,
        "summary": {
            "total_subnets": result.total_subnets,
            "passed_filters": result.passed_filters,
            "failed_filters": result.failed_filters,
        },
        "ranked": [asdict(s) for s in result.ranked],
        "filtered_out": result.filtered_out,
    }
    return json.dumps(data, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Demo / smoke test with synthetic data
# ─────────────────────────────────────────────────────────────────────────────

def _generate_demo_data() -> list[SubnetMetrics]:
    """Generate synthetic subnet data for testing the scoring pipeline."""
    np.random.seed(42)

    subnets = [
        # Should pass and score well — uptrending, low genie, good pool
        SubnetMetrics(
            subnet_id=4, name="Targon",
            token_price=0.015, pool_depth=150.0, genie_score=0.45,
            price_history=list(np.cumsum(np.random.normal(0.002, 0.01, 100)) + 0.01),
            timestamps=[f"2026-{(i//30)+2:02d}-{(i%28)+1:02d}T00:00:00Z" for i in range(100)],
        ),
        # Should pass — sideways, moderate metrics
        SubnetMetrics(
            subnet_id=51, name="Celium",
            token_price=0.008, pool_depth=80.0, genie_score=0.62,
            price_history=list(np.cumsum(np.random.normal(0.0, 0.01, 80)) + 0.008),
            timestamps=[f"2026-{(i//30)+2:02d}-{(i%28)+1:02d}T00:00:00Z" for i in range(80)],
        ),
        # Should FAIL — genie too high
        SubnetMetrics(
            subnet_id=77, name="HighConcentration",
            token_price=0.012, pool_depth=200.0, genie_score=0.91,
            price_history=list(np.cumsum(np.random.normal(0.001, 0.01, 60)) + 0.012),
            timestamps=[f"2026-{(i//30)+3:02d}-{(i%28)+1:02d}T00:00:00Z" for i in range(60)],
        ),
        # Should FAIL — price too high
        SubnetMetrics(
            subnet_id=99, name="Expensive",
            token_price=0.065, pool_depth=500.0, genie_score=0.40,
            price_history=list(np.cumsum(np.random.normal(0.001, 0.008, 90)) + 0.065),
            timestamps=[f"2026-{(i//30)+2:02d}-{(i%28)+1:02d}T00:00:00Z" for i in range(90)],
        ),
        # Should FAIL — pool too shallow
        SubnetMetrics(
            subnet_id=120, name="TinyPool",
            token_price=0.003, pool_depth=2.0, genie_score=0.50,
            price_history=list(np.cumsum(np.random.normal(0.0, 0.02, 40)) + 0.003),
            timestamps=[f"2026-{(i//30)+3:02d}-{(i%28)+1:02d}T00:00:00Z" for i in range(40)],
        ),
        # Should pass — good metrics, downtrending (low score)
        SubnetMetrics(
            subnet_id=68, name="Commune",
            token_price=0.022, pool_depth=300.0, genie_score=0.55,
            price_history=list(np.cumsum(np.random.normal(-0.003, 0.01, 90)) + 0.03),
            timestamps=[f"2026-{(i//30)+2:02d}-{(i%28)+1:02d}T00:00:00Z" for i in range(90)],
        ),
    ]
    return subnets


if __name__ == "__main__":
    print("=" * 60)
    print("Subnet Scoring Engine — Demo Run")
    print("=" * 60)

    demo_subnets = _generate_demo_data()
    current_holdings = [4, 51, 68, 75]  # Simon's-ish staked subnets

    result = run_scoring_cycle(demo_subnets, top_n=5)

    # Print Telegram-formatted output
    msg = format_telegram_alert(result, current_holdings=current_holdings)
    print("\n" + msg)

    # Print detailed scores
    print("\n" + "=" * 60)
    print("DETAILED SCORES")
    print("=" * 60)
    for s in result.ranked:
        print(f"\nSN{s.subnet_id} ({s.name}) — Composite: {s.composite_score:.1f}/100")
        print(f"  Markov: {s.markov_regime} (signal: {s.markov_signal:+.4f}) "
              f"{'✓' if s.markov_available else '⚠ insufficient data'}")
        print(f"  Trend:  {s.trend_score:.0f}/100 | Genie: {s.genie_score_inverted:.0f}/100 "
              f"(raw: {s.genie_score_raw:.2f})")
        print(f"  Momentum: {s.momentum_score:.0f}/100 | Pool: {s.pool_depth_score:.0f}/100")
        if s.alert_flags:
            print(f"  ⚠ Alerts: {', '.join(s.alert_flags)}")
        if s.transition_matrix:
            P = np.array(s.transition_matrix)
            print(f"  Transition matrix:")
            print(f"    {'':>9s} {'Bear':>8s} {'Side':>8s} {'Bull':>8s}")
            for i, state in enumerate(STATES):
                row = "  ".join(f"{P[i,j]*100:6.1f}%" for j in range(3))
                print(f"    {state:>9s} {row}")

    # Print filtered-out subnets
    print("\n" + "=" * 60)
    print("FILTERED OUT")
    print("=" * 60)
    for f in result.filtered_out:
        print(f"  SN{f['subnet_id']} ({f['name']}) — {f['reason']}")
        held = " ← CURRENTLY STAKED" if f['subnet_id'] in current_holdings else ""
        print(f"    Price: {f['token_price']:.4f} TAO | "
              f"Genie: {f['genie_score']:.2f} | "
              f"Pool: {f['pool_depth']:.0f} TAO{held}")
