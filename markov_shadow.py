"""Markov regime signal — per-subnet shadow-log at k=0.

Records the Roan/RohOnChain Markov signal (P(bull next) − P(bear next))
for each held position on every cron WITHOUT actually influencing sizing.
The point is to collect enough forward-return data (~60d) to score the
information coefficient and only THEN decide whether to dial k > 0 and
let the signal tilt position size.

Source of price closes:
    snapshot_history.daily_series_for_netuids() — the same store that
    powers the digest's 📊 footer. Real per-cron prices → last close
    per UTC day. NOT SubnetMetric.price_history (that's flat synthetic
    on the chain path — see chain_fetch.py comment).

Window sizing:
    Markov's canonical window is 20 trading days. Subnet store currently
    holds ~16 days — insufficient. Default here is 10 days so we start
    collecting signal data immediately; env-tunable via
    MARKOV_SHADOW_WINDOW to raise it as history grows.

Fields written to markov_shadow_log.csv:
    event_ts, netuid, name, price_now,
    current_regime, signal,
    bull_prob, bear_prob, sideways_prob,
    persistence_bull, persistence_bear, persistence_sideways,
    n_price_points, window, threshold,
    fwd_return_1d, fwd_return_7d, fwd_return_14d   # backfilled later
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

# Reuse the same core the on-camera Markov skill uses — labels, transition
# matrix, and STATES ordering all consistent. No yfinance / hmmlearn calls.
from markov_regime import (
    label_regimes,
    build_transition_matrix,
    STATES,
)

logger = logging.getLogger("markov_shadow")

DEFAULT_WINDOW = int(os.environ.get("MARKOV_SHADOW_WINDOW", "10"))
DEFAULT_THRESHOLD = float(os.environ.get("MARKOV_SHADOW_THRESHOLD", "0.05"))
MIN_HISTORY_POINTS = DEFAULT_WINDOW + 3   # need window + a few transitions

MARKOV_SHADOW_LOG_PATH = Path(
    os.environ.get(
        "MARKOV_SHADOW_LOG_PATH",
        str(Path(__file__).parent / "markov_shadow_log.csv"),
    )
)

SHADOW_FIELDS = [
    "event_ts", "netuid", "name",
    "price_now",
    "current_regime", "signal",
    "bull_prob", "bear_prob", "sideways_prob",
    "persistence_bull", "persistence_bear", "persistence_sideways",
    "n_price_points",
    "window", "threshold",
    "fwd_return_1d", "fwd_return_7d", "fwd_return_14d",
]


def compute_markov_signal(
    closes: list[float],
    window: int = DEFAULT_WINDOW,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict | None:
    """Compute the Markov signal for one subnet's daily close series.

    Returns None if the series is too short to produce a meaningful signal
    (need at least window + a few transitions after labelling).

    The signal itself is P(next=Bull | current_state) − P(next=Bear | current_state).
    Positive → bullish tilt, magnitude → conviction. At k=0 (shadow-log
    mode), the value is recorded but doesn't feed sizing.
    """
    if not closes or len(closes) < window + 3:
        return None
    series = pd.Series(pd.to_numeric(pd.Series(closes), errors="coerce")).dropna()
    if len(series) < window + 3:
        return None
    labels = label_regimes(series, window=window, threshold=threshold)
    if len(labels) < 3:
        return None
    P = build_transition_matrix(labels)
    current_state = int(labels.iloc[-1])
    next_probs = P[current_state]
    bull_p = float(next_probs[2])
    bear_p = float(next_probs[0])
    side_p = float(next_probs[1])
    return {
        "current_regime": STATES[current_state],
        "signal": round(bull_p - bear_p, 6),
        "bull_prob": round(bull_p, 6),
        "bear_prob": round(bear_p, 6),
        "sideways_prob": round(side_p, 6),
        "persistence_bull": round(float(P[2, 2]), 6),
        "persistence_bear": round(float(P[0, 0]), 6),
        "persistence_sideways": round(float(P[1, 1]), 6),
        "n_price_points": int(len(series)),
        "window": int(window),
        "threshold": float(threshold),
    }


def append_shadow_log(rows: list[dict], path: Path = MARKOV_SHADOW_LOG_PATH) -> None:
    """Append shadow-log rows to CSV. Non-fatal on failure.

    Schema-migration-safe: if the file exists with a shorter/older header,
    it's renamed to `.legacy-<timestamp>` and a fresh log starts with the
    current SHADOW_FIELDS. Same idiom as tp_cl_stops.append_outcome_log.
    """
    if not rows:
        return
    try:
        import time
        path.parent.mkdir(parents=True, exist_ok=True)
        # Header-mismatch migration (schema evolution safety)
        if path.exists() and path.stat().st_size > 0:
            try:
                with path.open("r", newline="") as fh:
                    header = [c.strip() for c in fh.readline().rstrip("\n").split(",")]
                if set(header) < set(SHADOW_FIELDS):
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    legacy = path.with_suffix(path.suffix + f".legacy-{stamp}")
                    path.rename(legacy)
            except Exception:
                pass
        new_file = (not path.exists()) or path.stat().st_size == 0
        with path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=SHADOW_FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in SHADOW_FIELDS})
    except Exception as e:
        logger.warning(f"Markov shadow log append failed: {e}")
