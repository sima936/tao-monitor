"""snapshot_history.py — free, self-accumulated per-subnet price/pool history.

WHY THIS EXISTS
---------------
The chain (chain_fetch.fetch_all_subnet_metrics_via_chain) gives a *spot
snapshot* — current price/pool/volume, no history. taostats supplied history
but is credit-walled; CoinGecko covers TAO but not subnet alpha tokens. So
every per-subnet momentum field (24h/7d/30d %, the trend sparkline, Flow Pass,
the ALL_ZERO gate) was starved, and run_scoring pushed HARD-CODED 0.0 deltas to
the dashboard — which the dashboard reads as "flat", rejecting all 129 subnets
ALL_ZERO (gordie.html: `pctMonth===0 && pctWeek===0 && pctDay===0`).

This module builds our OWN history by appending one snapshot per subnet per
cron to a SQLite file on the Railway Volume, then computes real deltas by
looking back at the nearest stored point for each horizon. As history
accumulates, 24h% comes alive in ~1 day, 7d% in ~1 week, 30d% in ~1 month
(at the 6h cron cadence). 1h% needs a sub-hourly logger — see the --record CLI.

DESIGN CONTRACT (mirrors chain_fetch.py's philosophy)
-----------------------------------------------------
  - Pure stdlib (sqlite3). No new dependency.
  - Fails closed. ANY error -> record is best-effort (logged), compute returns
    {} . Never raises into the caller, never fabricates a number. Worst case we
    are exactly back to "no deltas" — never a wrong delta.
  - Unknown horizons are OMITTED, never emitted as 0.0. The dashboard's sf()
    turns a missing key into null -> "—" (honest "accumulating"), and the
    ALL_ZERO / FLAT gates only fire on real zeros, not on unknowns.
  - SQLite (not CSV) because two writers can exist — the 6h scoring cron AND an
    optional frequent --record logger — and SQLite has real locking; concurrent
    CSV appends corrupt.

STORAGE
-------
Point SNAPSHOT_DB_PATH at the SAME Railway Volume as SCORE_LOG_PATH /
GINI_CACHE_PATH, or it resets each ephemeral cron container.

PUBLIC API
----------
  record_snapshot(metrics, ts=None)        -> int rows written
  compute_deltas(price_now_by_netuid, ...) -> {netuid: {"1h":pct, "24h":..}}
  record_and_deltas(metrics, ts=None)      -> deltas dict (record then compute)
  prune(max_age_days=45)                   -> int rows deleted
  stats()                                  -> {"rows":..,"netuids":..,"span_days":..}

CLI
---
  python snapshot_history.py --record   # free chain read + append (frequent-logger cron)
  python snapshot_history.py --stats    # inspect the store
  python snapshot_history.py --dump-csv out.csv
  python snapshot_history.py --prune    # drop rows older than 45d
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# ── Persistence path (Volume-backed; same convention as run_scoring) ──────────
SNAPSHOT_DB_PATH = Path(
    os.environ.get(
        "SNAPSHOT_DB_PATH",
        str(Path(__file__).parent / "snapshot_history.db"),
    )
)

# ── Horizons. Each: (key, seconds_back, tolerance_seconds) ───────────────────
# Tolerance = how far from the exact target timestamp a stored point may be and
# still count. Generous enough to absorb cron jitter / missed runs, tight enough
# that a "24h" delta is never silently measured over 3 days. If no point lands
# in the band, the horizon is OMITTED (not zeroed).
HORIZONS: list[tuple[str, int, int]] = [
    ("1h", 3_600, 45 * 60),          # ±45 min  — only resolves with a sub-hourly logger
    ("24h", 86_400, 8 * 3_600),      # ±8 h
    ("7d", 7 * 86_400, 2 * 86_400),  # ±2 d
    ("30d", 30 * 86_400, 7 * 86_400),  # ±7 d
]

DEFAULT_PRUNE_DAYS = 45  # keep 30d horizon + margin

# Mute-proof stderr diagnostics, same style as chain_fetch._diag.
def _diag(msg: str) -> None:
    print(f"[snapshot_history] {msg}", file=sys.stderr, flush=True)


# ── Connection / schema ──────────────────────────────────────────────────────
def _connect(db_path: Path = SNAPSHOT_DB_PATH) -> sqlite3.Connection:
    """Open the store, creating schema on first use. WAL + busy_timeout so the
    6h cron and an optional frequent logger can write without clobbering."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            ts     INTEGER NOT NULL,   -- unix epoch seconds (UTC)
            netuid INTEGER NOT NULL,
            price  REAL,               -- alpha price in TAO
            pool   REAL,               -- pool depth in TAO
            vol    REAL,               -- 24h volume in TAO
            PRIMARY KEY (netuid, ts)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_netuid_ts ON snapshots(netuid, ts)")
    return conn


def _f(x, default=None):
    """Coerce a value (or a Balance-like .tao object) to float; default on fail."""
    try:
        return float(getattr(x, "tao", x))
    except (TypeError, ValueError):
        return default


# ── Record ───────────────────────────────────────────────────────────────────
def record_snapshot(
    metrics,
    ts: Optional[int] = None,
    db_path: Path = SNAPSHOT_DB_PATH,
) -> int:
    """Append one row per subnet for this instant. `metrics` is the engine's
    SubnetMetrics list (attrs: subnet_id, token_price, pool_depth, volume_24h)
    OR any list of dicts with those keys. Returns rows written (0 on any error
    — best effort, never raises). Skips rows with no usable price."""
    if not metrics:
        return 0
    ts = int(ts if ts is not None else time.time())
    rows = []
    for m in metrics:
        if isinstance(m, dict):
            nid = m.get("subnet_id", m.get("netuid"))
            price = _f(m.get("token_price", m.get("price")))
            pool = _f(m.get("pool_depth", m.get("total_tao")))
            vol = _f(m.get("volume_24h", m.get("tao_volume_24_hr")), 0.0)
        else:
            nid = getattr(m, "subnet_id", getattr(m, "netuid", None))
            price = _f(getattr(m, "token_price", None))
            pool = _f(getattr(m, "pool_depth", None))
            vol = _f(getattr(m, "volume_24h", 0.0), 0.0)
        if nid is None or price is None or price <= 0:
            continue  # unusable — don't store a point we can't take a delta from
        rows.append((ts, int(nid), price, pool, vol))

    if not rows:
        return 0
    try:
        conn = _connect(db_path)
        with conn:  # transaction
            conn.executemany(
                "INSERT OR REPLACE INTO snapshots(ts,netuid,price,pool,vol) "
                "VALUES (?,?,?,?,?)",
                rows,
            )
        conn.close()
        _diag(f"recorded {len(rows)} subnet points @ ts={ts}")
        return len(rows)
    except Exception as e:  # noqa: BLE001 — best effort
        _diag(f"record FAILED ({type(e).__name__}: {e}) — no points stored this cycle")
        return 0


# ── Compute deltas ────────────────────────────────────────────────────────────
def _nearest_price(
    conn: sqlite3.Connection,
    netuid: int,
    target_ts: int,
    tol: int,
    now_ts: int,
) -> Optional[float]:
    """Stored price for `netuid` nearest to target_ts within ±tol, excluding the
    just-recorded point at now_ts. None if nothing lands in the band."""
    lo, hi = target_ts - tol, target_ts + tol
    row = conn.execute(
        """
        SELECT price FROM snapshots
        WHERE netuid = ? AND ts BETWEEN ? AND ? AND ts < ?
        ORDER BY ABS(ts - ?) ASC
        LIMIT 1
        """,
        (netuid, lo, hi, now_ts, target_ts),
    ).fetchone()
    return float(row[0]) if row and row[0] and row[0] > 0 else None


def compute_deltas(
    price_now_by_netuid: dict[int, float],
    now_ts: Optional[int] = None,
    db_path: Path = SNAPSHOT_DB_PATH,
) -> dict[int, dict[str, float]]:
    """For each netuid with a current price, return percent changes vs the
    nearest stored point per horizon: {netuid: {"24h": +3.5, "7d": -2.1, ...}}.
    Horizons with no in-band history are OMITTED (caller emits nothing -> "—").
    Returns {} on any error (fails closed)."""
    if not price_now_by_netuid:
        return {}
    now_ts = int(now_ts if now_ts is not None else time.time())
    out: dict[int, dict[str, float]] = {}
    try:
        conn = _connect(db_path)
        for nid, p_now in price_now_by_netuid.items():
            p_now = _f(p_now)
            if p_now is None or p_now <= 0:
                continue
            nid = int(nid)
            d: dict[str, float] = {}
            for key, back, tol in HORIZONS:
                p_then = _nearest_price(conn, nid, now_ts - back, tol, now_ts)
                if p_then is not None and p_then > 0:
                    d[key] = (p_now / p_then - 1.0) * 100.0
            if d:
                out[nid] = d
        conn.close()
    except Exception as e:  # noqa: BLE001 — fail closed
        _diag(f"compute_deltas FAILED ({type(e).__name__}: {e}) — returning {{}}")
        return {}
    _diag(
        f"computed deltas for {len(out)}/{len(price_now_by_netuid)} subnets "
        f"(horizons with history)"
    )
    return out


def record_and_deltas(
    metrics,
    ts: Optional[int] = None,
    db_path: Path = SNAPSHOT_DB_PATH,
    prune_days: int = DEFAULT_PRUNE_DAYS,
) -> dict[int, dict[str, float]]:
    """One call for the cron: append this instant, prune old rows, return the
    deltas to inject into the pools snapshot. Order matters — record first so
    the current point exists for *future* runs; deltas look strictly backward
    (ts < now), so the just-inserted row can't contaminate them."""
    ts = int(ts if ts is not None else time.time())
    record_snapshot(metrics, ts=ts, db_path=db_path)
    try:
        prune(max_age_days=prune_days, db_path=db_path)
    except Exception:  # noqa: BLE001 — pruning is housekeeping, never fatal
        pass
    price_now = {}
    for m in metrics or []:
        if isinstance(m, dict):
            nid = m.get("subnet_id", m.get("netuid"))
            price = _f(m.get("token_price", m.get("price")))
        else:
            nid = getattr(m, "subnet_id", getattr(m, "netuid", None))
            price = _f(getattr(m, "token_price", None))
        if nid is not None and price and price > 0:
            price_now[int(nid)] = price
    return compute_deltas(price_now, now_ts=ts, db_path=db_path)


# ── Daily series for the scoring engine (SS3 / Opportunities seam) ────────────
# The Markov engine (subnet_scoring_engine) consumes a DAILY close series
# (SUBNET_WINDOW=7, SUBNET_MIN_TRAIN=60), not raw 6h points. This resamples the
# store to one close per UTC day (the last snapshot of each day) so it drops
# straight into run_scoring.apply_history_overrides via its
# {netuid: (closes, timestamps)} contract. NOTE: genuinely time-gated — the
# engine wants ~60 daily bars, so this only yields non-degenerate regimes after
# ~2 months of accumulation; until then GeckoTerminal/taostats remain the real
# sources and this is a fallback for the subnets they don't cover.
def daily_series_for_netuids(
    netuids,
    max_days: int = 120,
    db_path: Path = SNAPSHOT_DB_PATH,
) -> dict[int, tuple[list[float], list[str]]]:
    """{netuid: ([daily_close, ...], [iso_date, ...])} oldest→newest, last
    snapshot per UTC day. Returns {} on any error (fails closed)."""
    import datetime as _dt
    if not netuids:
        return {}
    cutoff = int(time.time()) - max_days * 86_400
    out: dict[int, tuple[list[float], list[str]]] = {}
    try:
        conn = _connect(db_path)
        for nid in netuids:
            nid = int(nid)
            rows = conn.execute(
                "SELECT ts, price FROM snapshots "
                "WHERE netuid=? AND ts>=? AND price>0 ORDER BY ts ASC",
                (nid, cutoff),
            ).fetchall()
            if not rows:
                continue
            by_day: dict[str, tuple[int, float]] = {}
            for ts, price in rows:
                day = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).date().isoformat()
                # keep the LAST (latest ts) snapshot for each UTC day
                if day not in by_day or ts > by_day[day][0]:
                    by_day[day] = (ts, float(price))
            days = sorted(by_day)
            if len(days) >= 2:  # need at least 2 closes to be useful
                closes = [by_day[d][1] for d in days]
                stamps = [d + "T00:00:00+00:00" for d in days]
                out[nid] = (closes, stamps)
        conn.close()
    except Exception as e:  # noqa: BLE001
        _diag(f"daily_series FAILED ({type(e).__name__}: {e}) — returning {{}}")
        return {}
    return out


# ── Housekeeping ──────────────────────────────────────────────────────────────
def prune(max_age_days: int = DEFAULT_PRUNE_DAYS, db_path: Path = SNAPSHOT_DB_PATH) -> int:
    """Delete rows older than max_age_days. Returns rows deleted."""
    cutoff = int(time.time()) - max_age_days * 86_400
    try:
        conn = _connect(db_path)
        with conn:
            cur = conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
            n = cur.rowcount
        conn.close()
        if n:
            _diag(f"pruned {n} rows older than {max_age_days}d")
        return max(n, 0)
    except Exception as e:  # noqa: BLE001
        _diag(f"prune FAILED ({type(e).__name__}: {e})")
        return 0


def stats(db_path: Path = SNAPSHOT_DB_PATH) -> dict:
    """Quick health read: row count, distinct subnets, time span in days."""
    try:
        conn = _connect(db_path)
        rows = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        nets = conn.execute("SELECT COUNT(DISTINCT netuid) FROM snapshots").fetchone()[0]
        span = conn.execute("SELECT MIN(ts), MAX(ts) FROM snapshots").fetchone()
        conn.close()
        span_days = ((span[1] - span[0]) / 86_400) if span and span[0] else 0.0
        return {"rows": rows, "netuids": nets, "span_days": round(span_days, 2),
                "db": str(db_path)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "db": str(db_path)}


# ── CLI ───────────────────────────────────────────────────────────────────────
def _cli_record() -> int:
    """Free chain read + append. Wire this to a 30–60min cron to bring 1h% (and
    sharper 24h%) alive without the heavy scoring cron."""
    try:
        from chain_fetch import fetch_all_subnet_metrics_via_chain
    except Exception as e:  # noqa: BLE001
        _diag(f"--record: chain_fetch import failed ({e})")
        return 1
    metrics = fetch_all_subnet_metrics_via_chain()
    if not metrics:
        _diag("--record: chain returned no metrics — nothing stored")
        return 1
    n = record_snapshot(metrics)
    prune()
    _diag(f"--record: stored {n} points; {stats()}")
    return 0 if n else 1


def _cli_dump_csv(path: str, db_path: Path = SNAPSHOT_DB_PATH) -> int:
    import csv
    try:
        conn = _connect(db_path)
        rows = conn.execute(
            "SELECT ts,netuid,price,pool,vol FROM snapshots ORDER BY ts,netuid"
        ).fetchall()
        conn.close()
    except Exception as e:  # noqa: BLE001
        _diag(f"--dump-csv FAILED ({e})")
        return 1
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "netuid", "price", "pool", "vol"])
        w.writerows(rows)
    _diag(f"--dump-csv: wrote {len(rows)} rows -> {path}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="snapshot_history")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--record", action="store_true",
                   help="free chain read + append (frequent-logger cron)")
    g.add_argument("--stats", action="store_true", help="inspect the store")
    g.add_argument("--prune", action="store_true",
                   help=f"drop rows older than {DEFAULT_PRUNE_DAYS}d")
    g.add_argument("--dump-csv", metavar="PATH", help="export the store to CSV")
    args = p.parse_args(argv)

    if args.record:
        return _cli_record()
    if args.stats:
        import json
        print(json.dumps(stats(), indent=2))
        return 0
    if args.prune:
        n = prune()
        _diag(f"pruned {n} rows")
        return 0
    if args.dump_csv:
        return _cli_dump_csv(args.dump_csv)
    return 1


if __name__ == "__main__":
    sys.exit(main())
