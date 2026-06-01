"""
fetch_tao_macro.py — TAO Macro Regime Fetcher
===============================================
Runs Markov regime detection on TAO-USD price history
and writes the result to tao_macro.json for run_scoring.py.

Cron: run every 6 hours (regime doesn't change that fast)
    0 */6 * * * cd /home/simar/tao-monitor && python3 fetch_tao_macro.py

Requires: markov_regime.py in same directory, yfinance installed
    pip3 install yfinance numpy pandas --break-system-packages
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUTPUT_PATH = Path(__file__).parent / "tao_macro.json"

# TAO trades on some exchanges as TAO-USD via yfinance
# If TAO-USD fails, fall back to writing a "unavailable" state
TICKER = "TAO22974-USD"  # CoinGecko-style yfinance ticker for Bittensor


def main():
    # Add script directory to path so markov_regime imports cleanly
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))

    try:
        from markov_regime import analyze, fetch_ticker
    except ImportError as e:
        print(f"ERROR: Could not import markov_regime: {e}")
        _write_unavailable(f"import error: {e}")
        return

    print(f"Fetching TAO price history ({TICKER})...")
    tickers_to_try = [TICKER, "TAO-USD", "TAO22974-USD"]

    close = None
    for ticker in tickers_to_try:
        try:
            close = fetch_ticker(ticker, years=1)
            if len(close) > 30:
                print(f"  Got {len(close)} rows via {ticker}")
                break
        except Exception as e:
            print(f"  {ticker} failed: {e}")
            close = None

    if close is None or len(close) < 30:
        print("WARNING: Could not fetch TAO price data — writing unavailable state")
        _write_unavailable("price fetch failed")
        return

    try:
        result = analyze(
            close,
            source=TICKER,
            window=20,
            threshold=0.05,
            min_train=60,
            hmm=False,
        )
        OUTPUT_PATH.write_text(json.dumps(result, indent=2))
        regime = result["current_regime"]
        signal = result["signal"]
        print(f"TAO macro: {regime} (signal {signal:+.3f}) → written to {OUTPUT_PATH}")
    except Exception as e:
        print(f"ERROR: Markov analysis failed: {e}")
        _write_unavailable(f"analysis error: {e}")


def _write_unavailable(reason: str):
    OUTPUT_PATH.write_text(json.dumps({
        "current_regime": "Unknown",
        "signal": 0.0,
        "next_state_probabilities": {"bull": 0.33, "bear": 0.33, "sideways": 0.34},
        "unavailable_reason": reason,
    }, indent=2))


if __name__ == "__main__":
    main()
