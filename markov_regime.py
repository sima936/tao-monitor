# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "pandas", "yfinance", "hmmlearn", "scipy"]
# ///
"""Markov regime detection — a composable library + CLI.

This is the proven Markov framework from Roan (@RohOnChain), refactored from
the on-camera onboarding prompt into one clean, importable module.

It does five things, all asset-agnostic:

  1. label_regimes(...)        Bull / Bear / Sideways from a rolling return
  2. build_transition_matrix() MLE 3x3 transition matrix from the label sequence
  3. nstep_forecast(P, n)      Chapman-Kolmogorov: P^n is the n-step matrix
  4. stationary_distribution() the long-run regime mix (left eigenvector)
  5. walk_forward_backtest()   no-lookahead, re-estimated-every-step Sharpe + maxDD

Plus fit_hmm(...) for the optional Hidden Markov Model upgrade, and a top-level
analyze(...) that returns one structured dict for agents to consume.

Two ways to feed it data so it drops into any pipeline regardless of asset:
  --ticker SYMBOL   fetch daily history via yfinance (free, no key)
  --csv PATH        your own price series (needs a date column + a close column)

Two output modes:
  (default)         pretty terminal output — the on-camera demo
  --json            the analyze() dict as JSON to stdout, nothing else

Framework: Roan (@RohOnChain). Refactored into a Claude Code plugin by
Lewis Jackson. Backtests are historical, not forward-looking.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# State indices are fixed: 0 = Bear, 1 = Sideways, 2 = Bull.
STATES = ["Bear", "Sideways", "Bull"]

DEFAULT_WINDOW = 20
DEFAULT_THRESHOLD = 0.05  # ±5% rolling return cutoff
DEFAULT_YEARS = 10
DEFAULT_MIN_TRAIN = 252


# --------------------------------------------------------------------------- #
# Data loading — asset-agnostic. Either a ticker or a user's own CSV.
# --------------------------------------------------------------------------- #
def fetch_ticker(ticker: str, years: int = DEFAULT_YEARS) -> pd.Series:
    """Fetch a daily close series via yfinance, with one retry on empty data."""
    import yfinance as yf

    end = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    start = end - pd.DateOffset(years=years)

    df = pd.DataFrame()
    for attempt in (1, 2):
        try:
            df = yf.download(
                ticker,
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                progress=False,
                auto_adjust=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! yfinance error on attempt {attempt}: {exc}", file=sys.stderr)
            df = pd.DataFrame()

        if not df.empty:
            break
        if attempt == 1:
            print(
                "  ! yfinance returned empty data — retrying in 30s.", file=sys.stderr
            )
            time.sleep(30)

    if df.empty:
        raise RuntimeError(
            f"yfinance returned empty data for {ticker} after retry. "
            "Yahoo may be rate-limiting. Try again in a few minutes."
        )

    # Some yfinance versions return a MultiIndex column frame.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    close = df["Close"].dropna()
    close.name = ticker
    return close


def load_csv(path: str) -> pd.Series:
    """Load a user's own price series from CSV.

    Requirements are deliberately loose so this drops into any pipeline:
      - one column that parses as dates (first match of: date, time,
        timestamp, datetime, or the first column)
      - one close column (first match of: close, adj close, adj_close,
        price, last, or — if only one numeric column exists — that column)
    """
    df = pd.read_csv(path)
    if df.empty:
        raise RuntimeError(f"{path} is empty.")

    cols = {c.lower().strip(): c for c in df.columns}

    date_col = None
    for key in ("date", "time", "timestamp", "datetime"):
        if key in cols:
            date_col = cols[key]
            break
    if date_col is None:
        date_col = df.columns[0]

    close_col = None
    for key in ("close", "adj close", "adj_close", "adjclose", "price", "last"):
        if key in cols:
            close_col = cols[key]
            break
    if close_col is None:
        numeric = df.select_dtypes("number").columns.tolist()
        numeric = [c for c in numeric if c != date_col]
        if len(numeric) == 1:
            close_col = numeric[0]
        else:
            raise RuntimeError(
                f"Could not find a close column in {path}. "
                f"Add a column named one of: close, adj close, price, last. "
                f"Saw columns: {list(df.columns)}"
            )

    out = df[[date_col, close_col]].copy()
    out[date_col] = pd.to_datetime(out[date_col], utc=False, errors="coerce")
    out = out.dropna(subset=[date_col]).sort_values(date_col)
    close = pd.Series(
        pd.to_numeric(out[close_col], errors="coerce").to_numpy(),
        index=pd.DatetimeIndex(out[date_col]),
        name=Path(path).stem,
    ).dropna()
    if close.empty:
        raise RuntimeError(f"No usable rows after parsing {path}.")
    return close


# --------------------------------------------------------------------------- #
# Core model — pure functions.
# --------------------------------------------------------------------------- #
def label_regimes(
    close: pd.Series,
    window: int = DEFAULT_WINDOW,
    threshold: float = DEFAULT_THRESHOLD,
) -> pd.Series:
    """Label each day from the trailing `window`-day return.

    Bull (2)     : rolling return >  +threshold
    Bear (0)     : rolling return <  -threshold
    Sideways (1) : otherwise
    """
    rolling_return = close.pct_change(window)
    labels = pd.Series(1, index=close.index, dtype=int)  # default Sideways
    labels[rolling_return > threshold] = 2  # Bull
    labels[rolling_return < -threshold] = 0  # Bear
    return labels.loc[rolling_return.notna()]


def build_transition_matrix(labels: pd.Series) -> np.ndarray:
    """MLE estimate of the 3x3 transition matrix by counting transitions."""
    counts = np.zeros((3, 3), dtype=float)
    arr = np.asarray(labels, dtype=int)
    for i in range(len(arr) - 1):
        counts[arr[i], arr[i + 1]] += 1.0
    row_sums = counts.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1.0  # empty row -> stay put / no info
    return counts / row_sums


def nstep_forecast(matrix: np.ndarray, n: int) -> np.ndarray:
    """Chapman-Kolmogorov: the n-step transition matrix is P raised to n."""
    return np.linalg.matrix_power(matrix, n)


def stationary_distribution(matrix: np.ndarray) -> np.ndarray:
    """Left eigenvector of P for eigenvalue 1, normalised to sum to 1.

    This is the long-run regime mix the chain converges to regardless of
    where it starts.
    """
    eigvals, eigvecs = np.linalg.eig(matrix.T)
    idx = np.argmin(np.abs(eigvals - 1.0))
    vec = np.abs(np.real(eigvecs[:, idx]))
    return vec / vec.sum()


def signal_from_matrix(matrix: np.ndarray, current_state: int) -> float:
    """The signal: P(next=Bull | current) - P(next=Bear | current).

    Positive -> long bias, negative -> short bias, magnitude -> conviction.
    """
    return float(matrix[current_state, 2] - matrix[current_state, 0])


def walk_forward_backtest(
    close: pd.Series,
    labels: pd.Series,
    min_train: int = DEFAULT_MIN_TRAIN,
) -> dict:
    """No-lookahead walk-forward backtest.

    At each day t: fit the transition matrix on labels[:t] only, read the
    signal from the current state, take a +1/0/-1 position, score it against
    the next day's return.

    Incremental O(n): instead of rebuilding the full count matrix every step
    (the original O(n^2)), we maintain a running 3x3 count matrix and add one
    transition per step. MLE counting is a pure sum, so the per-step matrix is
    bit-for-bit identical to the from-scratch rebuild — same numbers, fast.
    """
    daily_returns = close.pct_change().dropna()
    common_index = labels.index.intersection(daily_returns.index)
    labels = labels.loc[common_index]
    daily_returns = daily_returns.loc[common_index]

    if len(labels) < min_train + 30:
        return {"sharpe": float("nan"), "max_drawdown": float("nan"), "n_trades": 0}

    lab = np.asarray(labels, dtype=int)
    rets = daily_returns.to_numpy(dtype=float)

    # Seed running counts with all transitions strictly inside [0, min_train).
    counts = np.zeros((3, 3), dtype=float)
    for i in range(min_train - 1):
        counts[lab[i], lab[i + 1]] += 1.0

    strategy_returns = np.empty(len(lab) - 1 - min_train, dtype=float)
    for k, t in enumerate(range(min_train, len(lab) - 1)):
        # counts now holds exactly the transitions among labels[:t]
        # (indices 0..t-1), which is what the from-scratch build used.
        row_sums = counts.sum(axis=1, keepdims=True)
        safe = np.where(row_sums == 0, 1.0, row_sums)
        P_t = counts / safe

        current_state = lab[t]
        signal = float(P_t[current_state, 2] - P_t[current_state, 0])
        position = float(np.sign(signal))
        strategy_returns[k] = position * rets[t + 1]

        # Slide the window forward by one: add the transition t-1 -> t so that
        # next iteration's `counts` covers labels[:t+1].
        counts[lab[t - 1], lab[t]] += 1.0

    sr = strategy_returns
    std = sr.std(ddof=1) if len(sr) > 1 else 0.0
    if std == 0 or not np.isfinite(std):
        sharpe = float("nan")
    else:
        sharpe = float(sr.mean() / std * np.sqrt(252))

    equity = (1.0 + sr).cumprod()
    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    max_dd = float(drawdown.min()) if len(drawdown) else float("nan")

    return {"sharpe": sharpe, "max_drawdown": max_dd, "n_trades": int(len(sr))}


def fit_hmm(returns: pd.Series, n_components: int = 3, random_state: int = 42):
    """Fit a Gaussian HMM on daily returns. Returns (model, hidden_states).

    Lazy import so the observable model still works if hmmlearn failed to
    compile (common on Windows without MSVC build tools). Returns (None, None)
    if hmmlearn is unavailable.

    Baum-Welch finds local maxima — for production work, fit several
    random_state values and keep the best by log-likelihood.
    """
    try:
        from hmmlearn import hmm  # noqa: PLC0415  (intentional lazy import)
    except Exception:  # noqa: BLE001  (ImportError or compiled-ext failure)
        return None, None

    X = returns.dropna().to_numpy(dtype=float).reshape(-1, 1)
    model = hmm.GaussianHMM(
        n_components=n_components,
        covariance_type="diag",
        n_iter=200,
        random_state=random_state,
    )
    model.fit(X)
    hidden_states = model.predict(X)
    return model, hidden_states


def _hmm_summary(close: pd.Series, enabled: bool) -> dict:
    """Build the HMM section of the analyze() dict, degrading gracefully."""
    if not enabled:
        return {"available": False, "reason": "disabled via --no-hmm"}
    try:
        model, _ = fit_hmm(close.pct_change().dropna(), n_components=3)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"hmm runtime error: {exc}"}
    if model is None:
        return {
            "available": False,
            "reason": "hmmlearn not installed or failed to compile",
        }

    means = np.array([model.means_[k][0] for k in range(model.n_components)])
    order = np.argsort(means)  # ascending mean return
    rank_names = ["Bear", "Sideways", "Bull"]
    regimes = []
    for rank, k in enumerate(order):
        regimes.append(
            {
                "label": rank_names[rank],
                "latent_state": int(k),
                "mean_daily_return": float(means[k]),
            }
        )
    return {
        "available": True,
        "regimes": regimes,
        "caveat": (
            "HMM states are labelled by ascending mean return, so a positive "
            "'Bear' mean just means the worst latent state was still net-"
            "positive over this window. Baum-Welch finds local maxima; for "
            "production fit several random_state values."
        ),
    }


# --------------------------------------------------------------------------- #
# Top-level analyze() — the single structured dict agents consume.
# --------------------------------------------------------------------------- #
def analyze(
    close: pd.Series,
    *,
    source: str,
    window: int = DEFAULT_WINDOW,
    threshold: float = DEFAULT_THRESHOLD,
    min_train: int = DEFAULT_MIN_TRAIN,
    hmm: bool = True,
) -> dict:
    """Run the whole framework and return one structured dict.

    See SKILL.md for the full field-by-field JSON contract.
    """
    close = close.dropna()
    labels = label_regimes(close, window=window, threshold=threshold)
    if len(labels) < 2:
        raise RuntimeError(
            "Not enough data to label regimes — need more rows than the "
            f"rolling window ({window}). Got {len(close)} price rows."
        )

    P = build_transition_matrix(labels)
    pi = stationary_distribution(P)

    current_state = int(labels.iloc[-1])
    next_probs = P[current_state]  # P(next | current) over [Bear, Side, Bull]
    bull_p = float(next_probs[2])
    bear_p = float(next_probs[0])
    side_p = float(next_probs[1])

    bt = walk_forward_backtest(close, labels, min_train=min_train)

    return {
        "source": source,
        "rows": int(len(close)),
        "date_start": str(close.index.min().date()),
        "date_end": str(close.index.max().date()),
        "params": {
            "window": window,
            "threshold": threshold,
            "min_train": min_train,
        },
        "states": STATES,
        "current_regime": STATES[current_state],
        "next_state_probabilities": {
            "bear": bear_p,
            "sideways": side_p,
            "bull": bull_p,
        },
        "signal": bull_p - bear_p,
        "transition_matrix": [[float(x) for x in row] for row in P],
        "persistence_diagonal": {
            "bear": float(P[0, 0]),
            "sideways": float(P[1, 1]),
            "bull": float(P[2, 2]),
        },
        "stationary_distribution": {
            "bear": float(pi[0]),
            "sideways": float(pi[1]),
            "bull": float(pi[2]),
        },
        "walk_forward": {
            "sharpe": bt["sharpe"],
            "max_drawdown": bt["max_drawdown"],
            "n_trades": bt["n_trades"],
        },
        "hmm": _hmm_summary(close, hmm),
        "framework": "Roan (@RohOnChain)",
        "disclaimer": "Backtests are historical, not forward-looking.",
    }


# --------------------------------------------------------------------------- #
# Pretty terminal output — the on-camera demo. Keep it.
# --------------------------------------------------------------------------- #
def _print_pretty(a: dict) -> None:
    P = np.array(a["transition_matrix"])
    print(
        f"\nmarkov-regime — source={a['source']} "
        f"window={a['params']['window']} threshold={a['params']['threshold']}"
    )
    print(f"  {a['rows']} rows | {a['date_start']} -> {a['date_end']}")

    print("\nTransition matrix (rows = from, cols = to):")
    print(f"            {'Bear':>9s} {'Sideways':>9s} {'Bull':>9s}")
    for i, from_state in enumerate(STATES):
        row = "  ".join(f"{P[i, j] * 100:7.2f}%" for j in range(3))
        print(f"  {from_state:>9s}  {row}")

    pd_diag = a["persistence_diagonal"]
    print("\nPersistence diagonal (how sticky each regime is):")
    print(f"  Bear -> Bear:         {pd_diag['bear'] * 100:.2f}%")
    print(f"  Sideways -> Sideways: {pd_diag['sideways'] * 100:.2f}%")
    print(f"  Bull -> Bull:         {pd_diag['bull'] * 100:.2f}%")

    sd = a["stationary_distribution"]
    print("\nStationary distribution (long-run regime mix):")
    print(f"       Bear: {sd['bear'] * 100:.2f}%")
    print(f"   Sideways: {sd['sideways'] * 100:.2f}%")
    print(f"       Bull: {sd['bull'] * 100:.2f}%")

    np_ = a["next_state_probabilities"]
    print(f"\nCurrent regime: {a['current_regime']}")
    print("Next-day probabilities from here:")
    print(
        f"   Bull: {np_['bull'] * 100:.2f}%   "
        f"Bear: {np_['bear'] * 100:.2f}%   "
        f"Sideways: {np_['sideways'] * 100:.2f}%"
    )
    print(f"Signal (bull_prob - bear_prob): {a['signal']:+.4f}")

    wf = a["walk_forward"]
    print("\nWalk-forward backtest (matrix re-estimated every step, no lookahead):")
    if np.isfinite(wf["sharpe"]):
        print(f"  Sharpe (annualised): {wf['sharpe']:.3f}")
    else:
        print("  Sharpe: NaN (insufficient data — try a longer history)")
    if np.isfinite(wf["max_drawdown"]):
        print(f"  Max drawdown:        {wf['max_drawdown'] * 100:.2f}%")
    else:
        print("  Max drawdown: NaN")
    print(f"  Trades evaluated:    {wf['n_trades']}")

    hmm = a["hmm"]
    if hmm.get("available"):
        print("\nHidden Markov Model (Baum-Welch + Viterbi):")
        for r in hmm["regimes"]:
            print(
                f"  {r['label']:<9s} (latent state {r['latent_state']}): "
                f"{r['mean_daily_return'] * 100:+.3f}% mean daily return"
            )
        print(f"  Note: {hmm['caveat']}")
    else:
        print(
            f"\nHMM skipped: {hmm.get('reason', 'unavailable')} "
            "(observable model above is unaffected)."
        )

    print("\n----------------------------------------------------------------")
    print(" Framework: Roan (@RohOnChain). Refactored into a Claude Code")
    print(" plugin by Lewis Jackson. Backtests are historical, not forward-")
    print(" looking. Point the matrix at whatever you trade.")
    print("----------------------------------------------------------------\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="markov_regime",
        description="Markov regime detection for any asset (Roan / @RohOnChain).",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--ticker", help="Symbol to fetch via yfinance, e.g. BTC-USD")
    src.add_argument("--csv", help="Path to your own CSV (date column + close column)")
    parser.add_argument(
        "--years",
        type=int,
        default=DEFAULT_YEARS,
        help=f"Years of history when using --ticker (default {DEFAULT_YEARS})",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW,
        help=f"Rolling-return window in days (default {DEFAULT_WINDOW})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Regime label threshold on rolling return (default {DEFAULT_THRESHOLD} = ±5%%)",
    )
    parser.add_argument(
        "--min-train",
        type=int,
        default=DEFAULT_MIN_TRAIN,
        help=f"Min training rows before the walk-forward starts (default {DEFAULT_MIN_TRAIN})",
    )
    parser.add_argument(
        "--no-hmm",
        action="store_true",
        help="Skip the HMM fit even if hmmlearn is available",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the analyze() dict as JSON to stdout and nothing else",
    )
    args = parser.parse_args(argv)

    try:
        if args.ticker:
            if not args.json:
                print(
                    f"  fetching {args.ticker} from Yahoo Finance...", file=sys.stderr
                )
            close = fetch_ticker(args.ticker, years=args.years)
            source = args.ticker
        else:
            close = load_csv(args.csv)
            source = args.csv

        result = analyze(
            close,
            source=source,
            window=args.window,
            threshold=args.threshold,
            min_train=args.min_train,
            hmm=not args.no_hmm,
        )
    except Exception as exc:  # noqa: BLE001
        if args.json:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"\nERROR: {exc}\n", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result))
    else:
        _print_pretty(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
