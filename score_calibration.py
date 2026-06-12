"""
Score Calibration Harness — TAO Monitor
=======================================
Tests and (lightly) optimises the weights the scoring engine gives each
criterion. It does NOT run an optimiser over the weight vector — on ~12
real-data subnets with a few weeks of history that overfits. Instead it
does the three sound-on-thin-data things:

  A. FACTOR IC + HIT-RATE  — does each criterion *predict forward return*?
     Per-cycle cross-sectional Spearman rank-corr between a factor's score
     and the subnet's forward return; mean IC, IC-IR (mean/std = stability),
     and a directional hit-rate. Reweight ∝ IC × IC-IR, not intuition.
     (Needs several cycles of history spanning at least one horizon.)

  B. PERTURBATION STABILITY — do the weights even matter yet?
     Jitter every weight by ±jitter (pp), renormalise, recompute the ranking
     on the latest cycle, measure churn (rank-corr + top-N overlap). If the
     book barely moves, precision is wasted effort. Needs NO forward data —
     runs today on a single cycle.

  C. ABLATION — is each criterion earning its place?
     Drop one factor (weight→0, renormalise), recompute the ranking, measure
     how much it changes. A factor whose removal barely moves the book is
     dead weight.

Same harness validates VTrust/emission once they're logged — any new
`f_<name>` column is picked up automatically.

--------------------------------------------------------------------------
LOG SCHEMA  (what run_scoring.py must emit — one row per subnet per cycle)
--------------------------------------------------------------------------
CSV with a header. Required columns:
    ts          ISO timestamp of the cycle      (e.g. 2026-06-12T14:00:00Z)
    subnet_id   int
    name        str
    price       float   token price in TAO at this cycle  (drives fwd return)
    composite   float   the composite health_score (0-100)

Factor columns — log every component the composite consumes, each as the
0-100 sub-score, prefixed `f_` (auto-detected):
    f_markov  f_trend  f_genie  f_momentum  f_pool   [f_vtrust  f_emission ...]

Optional (used for stratifying / labels, not required):
    regime      str   ("Bull"/"Bear"/"Sideways"/"Unknown")
    tier        str

Logging needs NO lookahead: snapshot scores + price each cycle. Forward
returns are computed here by joining each subnet's own future price.

Usage:
    python score_calibration.py --demo                 # synth + run end-to-end
    python score_calibration.py --log score_log.csv
    python score_calibration.py --log score_log.csv --horizons 1,7,14
    python score_calibration.py --log score_log.csv --jitter 0.10 --top-n 10
    python score_calibration.py --log score_log.csv \
        --weights "markov=0.30,trend=0.25,genie=0.20,momentum=0.15,pool=0.10"

Deps: numpy, pandas (already in the stack). Heuristic thresholds are rough
guides, not gospel — read IC alongside IC-IR and n_cycles.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

FACTOR_PREFIX = "f_"

# Mirrors the engine's current weights (by bare factor name, no f_ prefix).
DEFAULT_WEIGHTS = {
    "markov": 0.30,
    "trend": 0.25,
    "genie": 0.20,
    "momentum": 0.15,
    "pool": 0.10,
}

REQUIRED_COLS = {"ts", "subnet_id", "name", "price", "composite"}


# ─────────────────────────────────────────────────────────────────────────────
# IO
# ─────────────────────────────────────────────────────────────────────────────
def load_log(path: str) -> tuple[pd.DataFrame, list[str]]:
    df = pd.read_csv(path)
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise SystemExit(f"Log is missing required columns: {sorted(missing)}")
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce").dt.tz_localize(None)
    df = df.dropna(subset=["ts", "subnet_id", "price"]).copy()
    df["subnet_id"] = df["subnet_id"].astype(int)
    factor_cols = sorted(c for c in df.columns if c.startswith(FACTOR_PREFIX))
    if not factor_cols:
        raise SystemExit(f"No factor columns (expected '{FACTOR_PREFIX}<name>' columns).")
    return df.sort_values(["subnet_id", "ts"]).reset_index(drop=True), factor_cols


def factor_name(col: str) -> str:
    return col[len(FACTOR_PREFIX):]


# ─────────────────────────────────────────────────────────────────────────────
# Forward returns — join each subnet's own future price (no lookahead in log)
# ─────────────────────────────────────────────────────────────────────────────
def add_forward_returns(df: pd.DataFrame, horizon_days: list[int]) -> dict[int, str]:
    """For each row, find the subnet's price ~h days later and compute the
    return. Cadence-agnostic: uses timestamps, not row counts. Returns a map
    {horizon_days: column_name}."""
    cols: dict[int, str] = {}
    for h in horizon_days:
        col = f"fwd_{h}d"
        cols[h] = col
        df[col] = np.nan
        tol = max(pd.Timedelta(days=h) * 0.5, pd.Timedelta(days=1))
        for _, g in df.groupby("subnet_id", sort=False):
            ts = g["ts"].to_numpy()
            price = g["price"].to_numpy(dtype=float)
            target = ts + np.timedelta64(int(h * 24 * 3600), "s")
            idx = np.searchsorted(ts, target, side="left")
            out = np.full(len(g), np.nan)
            ok = idx < len(ts)
            ii = idx[ok]
            within = (ts[ii] - target[ok]) <= np.timedelta64(int(tol.total_seconds()), "s")
            base = price[np.flatnonzero(ok)[within]]
            fut = price[ii[within]]
            rows = np.flatnonzero(ok)[within]
            with np.errstate(divide="ignore", invalid="ignore"):
                out[rows] = np.where(base > 0, fut / base - 1.0, np.nan)
            df.loc[g.index, col] = out
    return cols


# ─────────────────────────────────────────────────────────────────────────────
# A. Factor IC + hit-rate
# ─────────────────────────────────────────────────────────────────────────────
def _spearman(a: pd.Series, b: pd.Series) -> float:
    m = a.notna() & b.notna()
    if m.sum() < 3 or a[m].nunique() < 2 or b[m].nunique() < 2:
        return np.nan
    return float(a[m].corr(b[m], method="spearman"))


def factor_ic(df: pd.DataFrame, factor_cols: list[str], fwd_col: str,
              min_names: int) -> pd.DataFrame:
    """Per-cycle cross-sectional Spearman(factor, forward return), then
    summarise across cycles. Also a pooled IC and a directional hit-rate."""
    rows = []
    cycles = [g for _, g in df.groupby("ts", sort=True) if g[fwd_col].notna().sum() >= min_names]
    for fc in factor_cols:
        per_cycle = [_spearman(g[fc], g[fwd_col]) for g in cycles]
        per_cycle = [x for x in per_cycle if not np.isnan(x)]
        # hit-rate: per cycle, does the top-half-by-factor out-return the bottom half?
        hits = tot = 0
        for g in cycles:
            gg = g[[fc, fwd_col]].dropna()
            if len(gg) < 4:
                continue
            med = gg[fc].median()
            top = gg[gg[fc] >= med][fwd_col].mean()
            bot = gg[gg[fc] < med][fwd_col].mean()
            if np.isfinite(top) and np.isfinite(bot):
                tot += 1
                hits += int(top > bot)
        pooled = _spearman(df[fc], df[fwd_col])
        ic_mean = float(np.mean(per_cycle)) if per_cycle else np.nan
        ic_std = float(np.std(per_cycle, ddof=1)) if len(per_cycle) > 1 else np.nan
        ir = ic_mean / ic_std if (ic_std and np.isfinite(ic_std) and ic_std > 0) else np.nan
        rows.append({
            "factor": factor_name(fc),
            "ic_mean": ic_mean,
            "ic_ir": ir,
            "pooled_ic": pooled,
            "hit_rate": (hits / tot) if tot else np.nan,
            "n_cycles": len(per_cycle),
        })
    return pd.DataFrame(rows).sort_values("ic_mean", ascending=False, na_position="last")


# ─────────────────────────────────────────────────────────────────────────────
# Composite recompute (for B & C) — weighted sum of present factors, renormalised
# ─────────────────────────────────────────────────────────────────────────────
def composite(latest: pd.DataFrame, factor_cols: list[str], weights: dict) -> pd.Series:
    w = {fc: max(0.0, weights.get(factor_name(fc), 0.0)) for fc in factor_cols}
    s = sum(w.values())
    if s <= 0:                                   # no weights → equal weight
        w = {fc: 1.0 for fc in factor_cols}
        s = float(len(factor_cols))
    out = sum(latest[fc].fillna(50.0) * (w[fc] / s) for fc in factor_cols)
    return pd.Series(out, index=latest.index)


def _topn_jaccard(a: pd.Series, b: pd.Series, ids: pd.Series, n: int) -> float:
    ta = set(ids[a.nlargest(n).index]); tb = set(ids[b.nlargest(n).index])
    u = ta | tb
    return len(ta & tb) / len(u) if u else 1.0


# ─────────────────────────────────────────────────────────────────────────────
# B. Perturbation stability
# ─────────────────────────────────────────────────────────────────────────────
def perturbation_stability(latest: pd.DataFrame, factor_cols: list[str], weights: dict,
                           jitter: float, trials: int, top_n: int, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    base = composite(latest, factor_cols, weights)
    base_rank = base.rank()
    ids = latest["subnet_id"]
    corrs, jacs = [], []
    names = [factor_name(fc) for fc in factor_cols]
    base_w = np.array([max(0.0, weights.get(nm, 0.0)) for nm in names], dtype=float)
    if base_w.sum() <= 0:
        base_w = np.ones(len(names))
    for _ in range(trials):
        jw = np.clip(base_w + rng.uniform(-jitter, jitter, size=base_w.shape), 0.0, None)
        if jw.sum() <= 0:
            continue
        wd = {nm: float(v) for nm, v in zip(names, jw)}
        comp = composite(latest, factor_cols, wd)
        c = _spearman(base_rank, comp)
        if not np.isnan(c):
            corrs.append(c)
        jacs.append(_topn_jaccard(base, comp, ids, min(top_n, len(latest))))
    return {
        "rank_corr_mean": float(np.mean(corrs)) if corrs else np.nan,
        "rank_corr_min": float(np.min(corrs)) if corrs else np.nan,
        "topn_jaccard_mean": float(np.mean(jacs)) if jacs else np.nan,
        "topn_jaccard_min": float(np.min(jacs)) if jacs else np.nan,
        "n_names": len(latest), "top_n": min(top_n, len(latest)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# C. Ablation
# ─────────────────────────────────────────────────────────────────────────────
def ablation(latest: pd.DataFrame, factor_cols: list[str], weights: dict,
             top_n: int) -> pd.DataFrame:
    base = composite(latest, factor_cols, weights)
    ids = latest["subnet_id"]
    rows = []
    for drop in factor_cols:
        kept = [c for c in factor_cols if c != drop]
        if not kept:
            continue
        comp = composite(latest, kept, weights)
        rows.append({
            "dropped": factor_name(drop),
            "rank_corr_vs_full": _spearman(base.rank(), comp),
            "topn_overlap": _topn_jaccard(base, comp, ids, min(top_n, len(latest))),
        })
    return pd.DataFrame(rows).sort_values("rank_corr_vs_full", na_position="first")


# ─────────────────────────────────────────────────────────────────────────────
# Demo data with a PLANTED signal (trend & vtrust predictive; momentum/pool noise)
# ─────────────────────────────────────────────────────────────────────────────
def make_demo_log(path: str, n_sub: int = 14, n_cycles: int = 45, seed: int = 7) -> str:
    rng = np.random.default_rng(seed)
    quality = rng.normal(0, 1, n_sub)                 # latent per-subnet quality
    price = rng.uniform(0.005, 0.03, n_sub)
    start = pd.Timestamp("2026-04-20T00:00:00Z")
    recs = []
    for c in range(n_cycles):
        ts = start + pd.Timedelta(days=c)
        ret = rng.normal(quality * 0.012, 0.05, n_sub)  # drift ∝ quality
        price = price * (1 + ret)
        def sc(load, noise):  # 0-100 factor that loads on quality by `load`
            return np.clip(50 + load * quality + rng.normal(0, noise, n_sub), 0, 100)
        f_trend = sc(18, 8)        # strong signal
        f_vtrust = sc(15, 9)       # strong signal (the new factor)
        f_markov = sc(10, 12)      # moderate
        f_genie = sc(7, 10)        # mild
        f_momentum = sc(0, 14)     # noise
        f_pool = sc(0, 13)         # noise
        comp = (0.30 * f_markov + 0.25 * f_trend + 0.20 * f_genie
                + 0.15 * f_momentum + 0.10 * f_pool)
        for i in range(n_sub):
            recs.append({
                "ts": ts.isoformat(), "subnet_id": 10 + i, "name": f"SN{10+i}",
                "price": round(float(price[i]), 8), "composite": round(float(comp[i]), 2),
                "f_markov": round(float(f_markov[i]), 2), "f_trend": round(float(f_trend[i]), 2),
                "f_genie": round(float(f_genie[i]), 2), "f_momentum": round(float(f_momentum[i]), 2),
                "f_pool": round(float(f_pool[i]), 2), "f_vtrust": round(float(f_vtrust[i]), 2),
                "regime": "Bull" if comp[i] > 60 else "Sideways" if comp[i] > 48 else "Bear",
            })
    pd.DataFrame(recs).to_csv(path, index=False)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Printing
# ─────────────────────────────────────────────────────────────────────────────
def _f(x, p=3):
    return "   n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.{p}f}"


def print_report(df, factor_cols, weights, horizons, jitter, trials, top_n, min_names):
    print("=" * 68)
    print(" SCORE CALIBRATION HARNESS")
    print("=" * 68)
    n_cycles = df["ts"].nunique()
    print(f" rows={len(df)}  subnets={df['subnet_id'].nunique()}  cycles={n_cycles}"
          f"  span={df['ts'].min().date()} → {df['ts'].max().date()}")
    print(f" factors: {', '.join(factor_name(c) for c in factor_cols)}")

    fwd = add_forward_returns(df, horizons)

    # ── A. Factor IC ──
    print("\n" + "-" * 68)
    print(" A. FACTOR IC + HIT-RATE  (does the criterion predict forward return?)")
    print("-" * 68)
    any_ic = False
    for h in horizons:
        col = fwd[h]
        usable = df.groupby("ts")[col].apply(lambda s: s.notna().sum() >= min_names).sum()
        if usable < 2:
            print(f"\n  {h:>2}d horizon: not enough history yet "
                  f"({usable} cycle(s) with ≥{min_names} forward returns). Need more logs.")
            continue
        any_ic = True
        tbl = factor_ic(df, factor_cols, col, min_names)
        print(f"\n  {h:>2}-day forward return:")
        print(f"    {'factor':<10} {'IC':>8} {'IC-IR':>8} {'pooled':>8} {'hit%':>7} {'cyc':>5}")
        for _, r in tbl.iterrows():
            hr = "  n/a" if np.isnan(r['hit_rate']) else f"{r['hit_rate']*100:4.0f}%"
            print(f"    {r['factor']:<10} {_f(r['ic_mean']):>8} {_f(r['ic_ir'],2):>8} "
                  f"{_f(r['pooled_ic']):>8} {hr:>7} {int(r['n_cycles']):>5}")
    if any_ic:
        print("\n  Guide: |IC|>0.05 useful, >0.10 strong, <0 flag. IC-IR>0.5 = stable.")
        print("  Reweight ∝ IC × IC-IR. A near-zero/negative IC factor is overweighted.")

    # ── B. Perturbation ──
    latest = df[df["ts"] == df["ts"].max()].copy()
    print("\n" + "-" * 68)
    print(" B. PERTURBATION STABILITY  (do the weights even matter yet?)")
    print("-" * 68)
    pert = perturbation_stability(latest, factor_cols, weights, jitter, trials, top_n)
    print(f"  latest cycle: {len(latest)} names | weight jitter ±{jitter:.0%} | {trials} trials")
    print(f"    ranking rank-corr vs baseline : mean {_f(pert['rank_corr_mean'])}"
          f"  worst {_f(pert['rank_corr_min'])}")
    print(f"    top-{pert['top_n']} set overlap (Jaccard)  : mean {_f(pert['topn_jaccard_mean'],2)}"
          f"  worst {_f(pert['topn_jaccard_min'],2)}")
    rc = pert["rank_corr_mean"]
    if np.isfinite(rc):
        verdict = ("ROBUST — weights are second-order; don't over-tune." if rc > 0.97
                   else "MODERATE — weights matter somewhat." if rc > 0.90
                   else "SENSITIVE — small weight changes flip the book; tune carefully.")
        print(f"    → {verdict}")

    # ── C. Ablation ──
    print("\n" + "-" * 68)
    print(" C. ABLATION  (is each criterion earning its place? latest cycle)")
    print("-" * 68)
    abl = ablation(latest, factor_cols, weights, top_n)
    print(f"    {'drop factor':<12} {'rank-corr vs full':>18} {'top-N overlap':>15}")
    for _, r in abl.iterrows():
        print(f"    {r['dropped']:<12} {_f(r['rank_corr_vs_full']):>18} "
              f"{_f(r['topn_overlap'],2):>15}")
    print("    Lower rank-corr when dropped = factor contributes MORE to the ranking.")
    print("=" * 68)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def parse_weights(s: str | None) -> dict:
    if not s:
        return dict(DEFAULT_WEIGHTS)
    out = {}
    for part in s.split(","):
        k, _, v = part.partition("=")
        out[k.strip()] = float(v)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="score_calibration")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--log", help="CSV log (see schema in module docstring)")
    src.add_argument("--demo", action="store_true", help="synthesise a planted-signal log and run")
    p.add_argument("--horizons", default="1,7,14", help="forward-return horizons in days")
    p.add_argument("--weights", default=None, help='e.g. "markov=0.30,trend=0.25,..."')
    p.add_argument("--jitter", type=float, default=0.10, help="weight perturbation ± in pp (0.10=±10pp)")
    p.add_argument("--trials", type=int, default=300)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--min-names", type=int, default=3, help="min names/cycle to compute cross-sectional IC")
    args = p.parse_args(argv)

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    weights = parse_weights(args.weights)

    if args.demo:
        path = make_demo_log("demo_score_log.csv")
        print(f"[demo] wrote planted-signal log → {path} "
              "(trend & vtrust predictive; momentum & pool are noise)\n")
    else:
        path = args.log

    df, factor_cols = load_log(path)
    print_report(df, factor_cols, weights, horizons, args.jitter, args.trials,
                 args.top_n, args.min_names)
    return 0


if __name__ == "__main__":
    sys.exit(main())
