"""hermes_lite — pre-Hermes calibration harness.

CLI-only. Runs the STRATEGY spec's step 2 (perturbation-stability) and
step 3 (IC scoring) on data that already exists — snapshot_history +
outcome_log + markov_shadow_log. No waiting for forward returns to
accumulate live: we backfill them from the historical snapshot store.

Same protocol Hermes will run when it's ready. Runs today.

USAGE
-----
    python hermes_lite.py ic       [--horizon 7]  [--window 10]
    python hermes_lite.py stability [--param TRAIL_PCT]
    python hermes_lite.py backfill [--horizon 7]
    python hermes_lite.py summary

FILES
-----
    goal.yaml                     tunable surface + thresholds
    markov_shadow_log.csv         signals to score
    outcome_log.csv               events to perturb
    /data/snapshots.db            backfill source

Does NOT touch live state, live positions, live cron. Read-only + local
CSV/report output.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


# ── Config loader ───────────────────────────────────────────────────────
def load_goal(path: Path = Path(__file__).parent / "goal.yaml") -> dict:
    """Parse goal.yaml. Uses stdlib only where possible; falls back to
    PyYAML if available (Railway has it via bittensor deps)."""
    try:
        import yaml
        return yaml.safe_load(path.read_text())
    except ImportError:
        # Very defensive fallback — parse only the fields we actually use.
        # Not robust for arbitrary YAML but sufficient for this file.
        return _minimal_yaml_parse(path.read_text())


def _minimal_yaml_parse(src: str) -> dict:
    """Emergency parser. Returns only `tunables` and `ic`/`stability` keys."""
    out: dict = {"tunables": {}, "ic": {}, "stability": {}}
    for line in src.splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
    return out


# ── Snapshot store adapter — read historical closes for fwd_return backfill ─
def load_snapshot_series(
    db_path: Path = Path("/data/snapshots.db"),
    max_days: int = 120,
) -> dict[int, pd.DataFrame]:
    """Return {netuid: DataFrame(ts, price)} — all snapshots for the window.

    Uses snapshot_history.daily_series_for_netuids under the hood if
    available; falls back to a direct sqlite read if not (portable to
    a machine without the full stack)."""
    try:
        # Prefer the project's own reader (it handles the schema authoritatively)
        from snapshot_history import daily_series_for_netuids, _connect
        conn = _connect(db_path)
        netuids = [r[0] for r in conn.execute(
            "SELECT DISTINCT netuid FROM snapshots"
        ).fetchall()]
        conn.close()
        series_map = daily_series_for_netuids(netuids, max_days=max_days, db_path=db_path)
        return {
            nid: pd.DataFrame({"ts": stamps, "price": closes})
            for nid, (closes, stamps) in series_map.items()
        }
    except Exception:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        cutoff = int(time.time()) - max_days * 86400
        rows = conn.execute(
            "SELECT netuid, ts, price FROM snapshots "
            "WHERE ts >= ? AND price > 0 ORDER BY netuid, ts ASC",
            (cutoff,),
        ).fetchall()
        conn.close()
        out: dict[int, pd.DataFrame] = {}
        for nid, ts, price in rows:
            df = out.setdefault(int(nid), pd.DataFrame({"ts": [], "price": []}))
            df.loc[len(df)] = [ts, price]
        return out


# ── Forward-return backfill ────────────────────────────────────────────
def backfill_fwd_returns(
    log_path: Path,
    ts_col: str = "event_ts",
    netuid_col: str = "netuid",
    price_col: str = "price_now",
    horizons_days: list[int] = (1, 7, 14),
    snapshots: dict[int, pd.DataFrame] | None = None,
) -> int:
    """Fill fwd_return_{h}d columns on any log row where they're blank.

    Method: for each row's (netuid, ts), find the snapshot closest to
    ts + horizon and compute (price_then / price_now) - 1. No lookahead
    (we only join to snapshots that existed by ts+horizon), no fabricated
    data (if no snapshot exists within a tolerance, leave blank).

    Returns count of rows updated. Writes in place.
    """
    if not log_path.exists() or log_path.stat().st_size == 0:
        print(f"  {log_path.name}: empty or missing, nothing to backfill")
        return 0

    snapshots = snapshots if snapshots is not None else load_snapshot_series()
    if not snapshots:
        print("  no snapshot data available for backfill")
        return 0

    # Read log, backfill in memory, rewrite. Small enough to load fully.
    rows = list(csv.DictReader(open(log_path)))
    if not rows:
        return 0
    fieldnames = list(rows[0].keys())
    # Guarantee horizon columns exist (some legacy rows won't have them)
    for h in horizons_days:
        col = f"fwd_return_{h}d"
        if col not in fieldnames:
            fieldnames.append(col)
            for r in rows:
                r.setdefault(col, "")

    tolerance_hours = 12   # snapshot within ±12h of target counts
    updated = 0

    # Pre-index snapshots by netuid → sorted (ts, price) arrays for fast lookup
    idx: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for nid, df in snapshots.items():
        if df.empty:
            continue
        # Portable ts → unix epoch: force datetime64[ns] then integer-divide.
        # Pandas 2.x defaults new datetime64 columns to [s] resolution while
        # 1.x uses [ns]; forcing [ns] first makes the arithmetic version-safe.
        dts = pd.to_datetime(df["ts"])
        try:
            ts_vals = dts.values.astype("datetime64[ns]").astype("int64") // 10**9
        except Exception:
            # Fallback for exotic dtypes
            ts_vals = np.array([int(t.timestamp()) for t in dts])
        idx[int(nid)] = (ts_vals, df["price"].astype(float).to_numpy())

    for r in rows:
        try:
            nid = int(r[netuid_col])
            ts = float(r[ts_col])
            p0 = float(r[price_col]) if r.get(price_col) else None
        except (ValueError, TypeError, KeyError):
            continue
        if nid not in idx or p0 is None or p0 <= 0:
            continue
        ts_arr, px_arr = idx[nid]
        for h in horizons_days:
            col = f"fwd_return_{h}d"
            if r.get(col) not in ("", None):
                continue   # already filled — never overwrite
            target_ts = ts + h * 86400
            # find nearest snapshot within tolerance
            diffs = np.abs(ts_arr - target_ts)
            j = int(np.argmin(diffs))
            if diffs[j] > tolerance_hours * 3600:
                continue
            p1 = float(px_arr[j])
            if p1 <= 0:
                continue
            r[col] = round(p1 / p0 - 1.0, 6)
            updated += 1

    # Rewrite log
    with log_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"  {log_path.name}: {updated} fwd_return cells filled "
          f"({len(rows)} rows total)")
    return updated


# ── IC scoring ──────────────────────────────────────────────────────────
def spearman_ic(signal: list[float], fwd: list[float]) -> float | None:
    """Spearman rank correlation. Returns None if under-sampled or degenerate."""
    if len(signal) < 5 or len(signal) != len(fwd):
        return None
    s = pd.Series(signal).rank()
    f = pd.Series(fwd).rank()
    if s.nunique() < 2 or f.nunique() < 2:
        return None
    return float(s.corr(f))


def ic_report(
    shadow_log_path: Path,
    horizons: list[int] = (1, 7, 14),
) -> dict:
    """Score IC per subnet + pooled at each horizon."""
    if not shadow_log_path.exists() or shadow_log_path.stat().st_size == 0:
        return {"error": "no shadow log"}
    df = pd.read_csv(shadow_log_path)
    if df.empty:
        return {"error": "empty shadow log"}
    report: dict = {"per_horizon": {}, "n_rows": len(df)}
    for h in horizons:
        col = f"fwd_return_{h}d"
        if col not in df.columns:
            continue
        # Only rows with a filled fwd return
        d = df[["netuid", "signal", col]].dropna()
        d = d[d[col].astype(str) != ""]
        if d.empty:
            report["per_horizon"][h] = {"n": 0, "pooled_ic": None, "per_subnet": {}}
            continue
        d["signal"] = pd.to_numeric(d["signal"], errors="coerce")
        d[col] = pd.to_numeric(d[col], errors="coerce")
        d = d.dropna()
        pooled = spearman_ic(d["signal"].tolist(), d[col].tolist())
        per_subnet: dict[int, dict] = {}
        for nid, grp in d.groupby("netuid"):
            per_subnet[int(nid)] = {
                "n": len(grp),
                "ic": spearman_ic(grp["signal"].tolist(), grp[col].tolist()),
            }
        # IC stability = IQR of per-subnet ICs (need ≥ 4 subnets)
        ics = [v["ic"] for v in per_subnet.values() if v["ic"] is not None]
        stability_iqr = None
        if len(ics) >= 4:
            q1, q3 = np.percentile(ics, [25, 75])
            stability_iqr = float(q3 - q1)
        report["per_horizon"][h] = {
            "n": len(d),
            "pooled_ic": pooled,
            "ic_stability_iqr": stability_iqr,
            "per_subnet": per_subnet,
        }
    return report


# ── Perturbation-stability ──────────────────────────────────────────────
def perturbation_stability(
    outcome_log_path: Path,
    param: str,
    jitter: float,
    goal_config: dict | None = None,
) -> dict:
    """Replay outcome_log under param jittered ±jitter and measure exit churn.

    Simple approximation: count how many TRAIL_STOP / HARD_STOP events would
    change classification under the perturbed threshold. Doesn't simulate
    full trade paths (that would need snapshot_history walk-through) — this
    is a first-order estimate.

    Returns dict with pct_events_changed at ±jitter.
    """
    if not outcome_log_path.exists() or outcome_log_path.stat().st_size == 0:
        return {"param": param, "error": "no outcome log"}
    df = pd.read_csv(outcome_log_path)
    if df.empty:
        return {"param": param, "error": "empty outcome log"}

    # Only care about stop-related rows
    stops = df[df["event_type"].isin(["TRAIL_STOP", "HARD_STOP"])].copy()
    if stops.empty:
        return {"param": param, "error": "no stop events yet", "n_events": 0}

    changed_lo = 0
    changed_hi = 0
    for _, r in stops.iterrows():
        try:
            pnl = float(r.get("pnl_pct", 0)) / 100.0
        except (ValueError, TypeError):
            continue
        if param == "STOP_PCT":
            base = float(r.get("stop_pct_used", 0.30) or 0.30)
            # Under stricter stop, was pnl already worse than -stricter?
            fires_under_base = pnl <= -base
            fires_under_lo   = pnl <= -(base - jitter)   # tighter
            fires_under_hi   = pnl <= -(base + jitter)   # looser
            if fires_under_lo != fires_under_base:
                changed_lo += 1
            if fires_under_hi != fires_under_base:
                changed_hi += 1
        elif param == "TRAIL_PCT":
            base = float(r.get("trail_pct_used", 0.25) or 0.25)
            # Similar logic — we don't have peak_value on the row, so this
            # is an approximation using pnl as a lower bound proxy.
            fires_under_base = pnl <= -base
            fires_under_lo   = pnl <= -(base - jitter)
            fires_under_hi   = pnl <= -(base + jitter)
            if fires_under_lo != fires_under_base:
                changed_lo += 1
            if fires_under_hi != fires_under_base:
                changed_hi += 1

    n = len(stops)
    return {
        "param": param,
        "jitter": jitter,
        "n_events": int(n),
        "pct_changed_low":  round(changed_lo / n, 3) if n else 0,
        "pct_changed_high": round(changed_hi / n, 3) if n else 0,
    }


# ── Summary printer ────────────────────────────────────────────────────
def _render_ic(report: dict) -> str:
    if "error" in report:
        return f"  IC: {report['error']}"
    lines = [f"  IC scoring on {report['n_rows']} rows:"]
    for h, block in report["per_horizon"].items():
        n = block["n"]
        p = block["pooled_ic"]
        s = block["ic_stability_iqr"]
        p_str = f"{p:+.3f}" if p is not None else "n/a"
        s_str = f"IQR {s:.3f}" if s is not None else "IQR n/a"
        eff = (abs(p) * (1 - min(s or 1, 1))) if (p is not None and s is not None) else None
        eff_str = f"eff IC {eff:+.3f}" if eff is not None else "eff IC n/a"
        lines.append(f"    horizon {h}d: n={n}, pooled IC {p_str}, {s_str}, {eff_str}")
    return "\n".join(lines)


def _render_stability(reports: list[dict]) -> str:
    if not reports:
        return "  Stability: no reports"
    lines = ["  Perturbation-stability:"]
    for r in reports:
        if "error" in r:
            lines.append(f"    {r.get('param','?')}: {r['error']}")
            continue
        lo = r["pct_changed_low"] * 100
        hi = r["pct_changed_high"] * 100
        tag = "🟢 quiet"
        if max(lo, hi) > 40:
            tag = "🔴 chaotic (overfit risk)"
        elif max(lo, hi) < 5:
            tag = "⚪ noise-level (precision doesn't matter here)"
        else:
            tag = "🟡 meaningful"
        lines.append(f"    {r['param']} ±{r['jitter']}: "
                     f"-{lo:.1f}% / +{hi:.1f}% churn over {r['n_events']} events  {tag}")
    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────
def main(argv=None):
    p = argparse.ArgumentParser(prog="hermes_lite")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("backfill",  help="fill fwd_return columns on the logs from snapshot_history")
    sub.add_parser("ic",        help="IC report on markov_shadow_log.csv")
    ps = sub.add_parser("stability", help="perturbation-stability on outcome_log.csv")
    ps.add_argument("--param", default=None,
                    help="param to jitter (TRAIL_PCT, STOP_PCT). Default: both.")
    sub.add_parser("summary",   help="full report: backfill + IC + stability")

    args = p.parse_args(argv)

    here = Path(__file__).parent
    outcome_log = Path(os.environ.get("OUTCOME_LOG_PATH", here / "outcome_log.csv"))
    shadow_log  = Path(os.environ.get("MARKOV_SHADOW_LOG_PATH", here / "markov_shadow_log.csv"))
    goal        = load_goal(here / "goal.yaml") if (here / "goal.yaml").exists() else {}

    if args.cmd == "backfill":
        print("Backfilling fwd_returns from snapshot_history...")
        snap = load_snapshot_series()
        backfill_fwd_returns(shadow_log, snapshots=snap)
        backfill_fwd_returns(outcome_log, snapshots=snap)
        return 0

    if args.cmd == "ic":
        report = ic_report(shadow_log)
        print(f"\nIC report on {shadow_log.name}")
        print("=" * 62)
        print(_render_ic(report))
        return 0

    if args.cmd == "stability":
        params = [args.param] if args.param else ["TRAIL_PCT", "STOP_PCT"]
        jitters = {
            "TRAIL_PCT": 0.03,
            "STOP_PCT":  0.05,
        }
        reports = [
            perturbation_stability(outcome_log, prm, jitters.get(prm, 0.05))
            for prm in params
        ]
        print(f"\nStability report on {outcome_log.name}")
        print("=" * 62)
        print(_render_stability(reports))
        return 0

    if args.cmd == "summary":
        print("=" * 62)
        print(" HERMES LITE — pre-Hermes calibration report")
        print("=" * 62)
        print("\n▶ Step 1: backfill fwd_returns from snapshot_history")
        snap = load_snapshot_series()
        n_span = None
        if snap:
            _epoch_arrays = []
            for _df in snap.values():
                if _df.empty:
                    continue
                _dts = pd.to_datetime(_df["ts"])
                try:
                    _epoch = _dts.values.astype("datetime64[ns]").astype("int64") // 10**9
                except Exception:
                    _epoch = np.array([int(t.timestamp()) for t in _dts])
                _epoch_arrays.append(_epoch)
            all_ts = np.concatenate(_epoch_arrays) if _epoch_arrays else np.array([])
            if len(all_ts):
                span_d = (all_ts.max() - all_ts.min()) / 86400.0
                n_span = span_d
                print(f"  snapshot_history: {len(snap)} netuids, span {span_d:.1f}d")
        backfill_fwd_returns(shadow_log, snapshots=snap)
        backfill_fwd_returns(outcome_log, snapshots=snap)

        print("\n▶ Step 2: perturbation-stability on stops")
        reports = [
            perturbation_stability(outcome_log, "TRAIL_PCT", 0.03),
            perturbation_stability(outcome_log, "STOP_PCT",  0.05),
        ]
        print(_render_stability(reports))

        print("\n▶ Step 3: IC scoring on Markov shadow signal")
        ic = ic_report(shadow_log)
        print(_render_ic(ic))

        print("\n▶ Verdict")
        print("=" * 62)
        # Interpretation
        if n_span is not None and n_span < 25:
            print(f"  Data horizon short ({n_span:.1f}d). Numbers are indicative,")
            print(f"  not yet decisive. Re-run weekly; verdict crystallises")
            print(f"  around day 60 (~ another {max(0, 60 - n_span):.0f}d).")
        # Extract effective IC at 7d horizon (the actionable one)
        h7 = (ic.get("per_horizon", {}) or {}).get(7, {})
        pooled = h7.get("pooled_ic")
        stab = h7.get("ic_stability_iqr")
        if pooled is not None and stab is not None:
            eff = abs(pooled) * (1 - min(stab, 1))
            if eff > 0.03:
                sign = "positive" if pooled > 0 else "NEGATIVE"
                print(f"  Markov signal: effective IC {eff:+.3f} at 7d ({sign})")
                if pooled > 0:
                    print("    → candidate for markov_size_tilt_k > 0 (do NOT flip yet;")
                    print("      confirm at 60d before touching the dial).")
                else:
                    print("    → signal is anti-predictive — DO NOT flip k. Investigate")
                    print("      whether label mapping is inverted (FIX-2 in markov_regime).")
            else:
                print(f"  Markov signal: effective IC {eff:.3f} at 7d — below threshold.")
                print("    → keep k=0. Signal is not yet distinguishable from noise.")
        else:
            print("  Markov signal: insufficient data for IC judgement. Continue shadow-logging.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
