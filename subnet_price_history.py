"""Per-subnet daily price-history store — v2 real-history layer.

Motivation
----------
Before this module the chain_fetch path (all 129 subnets) was giving every
subnet a FLAT synthetic 9-bar price history (bars all equal to the current
price), which drove `pct_change_24h` / `pct_change_7d` on the scoring engine to
0.0 and `p9_data_maturity` to a constant ~50. That was fine for the 15
holdings+watchlist+top-movers we spent the GT budget on via
`geckoterminal_fetch.fetch_history_for_netuids`, but everyone else was
scored on invented flatness. Symptom: the Opportunities dashboard tab
displayed 12 subnets with identical placeholder scores.

The right fix is a persistent per-subnet history for the full 129-subnet
universe, honestly reflecting price movement. This module is that store.

Design
------
- SQLite at SUBNET_HISTORY_DB_PATH (env, default under the SA volume mount).
- One table `price_bars(netuid INT, ts INT, close REAL, PRIMARY KEY(netuid,
  ts))`. Timestamps are UTC-day boundaries in epoch seconds (00:00:00Z of
  each UTC day) so a subnet's daily bars are unambiguous and idempotent.
- `record_bars` uses INSERT OR REPLACE, so re-fetching a day is safe and
  simply overwrites with the latest close.
- Reads return oldest-first (matches the scoring engine's contract).

Budget-aware fetching
---------------------
GeckoTerminal free = 30 calls/min hard limit. `geckoterminal_fetch` uses 4s
spacing (~15/min) for headroom. Full 129-subnet backfill = ~9 min; done once
on first-ever run. Every cron thereafter, `top_up_stale` fetches only
subnets whose most-recent stored bar is >20h old — since daily bars only
update once per day, that amortises to ~32 subnets/cron ≈ ~2 min per cron
(over 4 crons/day, every subnet is refreshed once).

If GeckoTerminal returns fewer than `min_bars_for_valid` daily bars for a
subnet (typically brand-new pools not yet indexed on GT), the store leaves
that subnet's rows alone — the read path falls back to the flat synthetic
in `chain_fetch._dynamicinfos_to_metrics`, preserving the "never fabricate
trend from a snapshot" invariant.

Read contract
-------------
`get_bars(netuid, limit=120)` returns `(closes, timestamps_iso)` in the same
shape as `geckoterminal_fetch.fetch_daily_closes` and
`taostats_fetch.fetch_pool_history` — a drop-in replacement for the flat
synthetic history in `chain_fetch._dynamicinfos_to_metrics`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("subnet_price_history")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Path resolution: env var -> volume default -> script-adjacent fallback. Mirrors
# the STATE_FILE / GINI_CACHE_PATH / SCORE_LOG_PATH pattern already used in
# run_scoring.py so operators set one variable per file per volume.
DB_PATH = Path(os.environ.get(
    "SUBNET_HISTORY_DB_PATH",
    str(Path(__file__).parent / "subnet_price_history.db"),
))

# Bar retention target — enough for 72-day EMA + comfortable Markov window.
# GT's free API caps at 1000 daily candles per response; we ask for 120 which
# covers the scoring engine's needs with margin and keeps payloads small.
BARS_TO_FETCH = int(os.environ.get("SUBNET_HISTORY_BARS_TO_FETCH", "120"))

# A subnet's rows are "stale" if the newest bar is older than this. 20h is
# tight enough that once-per-day GT refresh always fires, loose enough that
# we don't waste calls on a subnet that was just refreshed 4 cron cycles ago.
STALE_HOURS = float(os.environ.get("SUBNET_HISTORY_STALE_HOURS", "20"))

# Minimum bar count returned by GT for a subnet before we accept it as a real
# series. Below this the pool is too new / too sparse — we leave the store
# untouched for that subnet and the read path falls back to synthetic.
MIN_BARS_FOR_VALID = int(os.environ.get("SUBNET_HISTORY_MIN_BARS", "9"))


def _diag(msg: str) -> None:
    """Stderr-with-tag diagnostic (mirrors chain_fetch._diag).

    The cron service's bittensor logger hijacks the root logger, so [price_hist]
    prefix in Railway logs is the only reliable way to prove this module ran.
    """
    print(f"[price_hist] {msg}", file=sys.stderr, flush=True)


# Once-per-process schema-ready flag. `init_db` is idempotent and cheap
# (CREATE IF NOT EXISTS), but re-running it on every read/write would still
# open a spare connection each time. Guard behind a module-level bool so
# read_bars / get_bars / record_bars can guarantee the table exists without
# the caller having to remember to init.
_SCHEMA_READY = False


def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    init_db()
    _SCHEMA_READY = True


# ─────────────────────────────────────────────────────────────────────────────
# Schema and connection
# ─────────────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_bars (
    netuid INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (netuid, ts)
);
CREATE INDEX IF NOT EXISTS idx_price_bars_netuid ON price_bars (netuid);
CREATE INDEX IF NOT EXISTS idx_price_bars_ts ON price_bars (ts);
"""


@contextmanager
def _connect(path: Optional[Path] = None):
    """Context-managed connection. Ensures dir exists, WAL mode for cron+listener
    concurrency safety, and closes cleanly on any exception."""
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30.0)
    try:
        # WAL lets the listener read while the cron writes. Idempotent — safe
        # to call every connection.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Optional[Path] = None) -> None:
    """Create the table + indexes if they don't exist. Safe to call every cron."""
    with _connect(path) as conn:
        conn.executescript(_SCHEMA)


# ─────────────────────────────────────────────────────────────────────────────
# Write path
# ─────────────────────────────────────────────────────────────────────────────

def record_bars(netuid: int, bars: Iterable[tuple[int, float]]) -> int:
    """Upsert one subnet's bars into the store.

    `bars` is an iterable of (unix_seconds_utc, close_price_tao) tuples in any
    order. Duplicate (netuid, ts) rows overwrite the existing close — safe to
    re-fetch a partial day and let today's later fetch replace the earlier one.

    Returns number of rows written.
    """
    rows = [(int(netuid), int(ts), float(close))
            for ts, close in bars
            if close is not None and close > 0]
    if not rows:
        return 0
    _ensure_schema()
    with _connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO price_bars (netuid, ts, close) VALUES (?, ?, ?)",
            rows,
        )
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# Read path — the scoring engine's contract
# ─────────────────────────────────────────────────────────────────────────────

def _iso(ts_epoch: int) -> str:
    """Epoch seconds → ISO8601 UTC. Matches the scoring engine's timestamp
    format (same shape geckoterminal_fetch._to_iso produces)."""
    return datetime.fromtimestamp(int(ts_epoch), tz=timezone.utc).isoformat()


def get_bars(
    netuid: int,
    limit: int = BARS_TO_FETCH,
) -> tuple[list[float], list[str]]:
    """Return (closes, iso_timestamps) for a subnet, oldest-first.

    Empty if the subnet has no stored bars. Matches the shape of
    `geckoterminal_fetch.fetch_daily_closes` so this drops straight into
    `apply_history_overrides` / `_dynamicinfos_to_metrics`.
    """
    _ensure_schema()
    with _connect() as conn:
        cur = conn.execute(
            "SELECT ts, close FROM price_bars WHERE netuid = ? "
            "ORDER BY ts DESC LIMIT ?",
            (int(netuid), int(limit)),
        )
        rows = cur.fetchall()
    if not rows:
        return [], []
    rows.reverse()  # DESC + reverse = ASC oldest-first; faster than ORDER BY ASC + LIMIT
    closes = [float(c) for _, c in rows]
    stamps = [_iso(ts) for ts, _ in rows]
    return closes, stamps


def get_last_bar_ts(netuid: int) -> Optional[int]:
    """Newest bar timestamp for a subnet (epoch seconds), or None if empty.

    Used by `top_up_stale` to decide who to refetch.
    """
    _ensure_schema()
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(ts) FROM price_bars WHERE netuid = ?",
            (int(netuid),),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def stats() -> dict:
    """Return a small summary dict for /status footers and diagnostics."""
    _ensure_schema()
    with _connect() as conn:
        cur = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT netuid), MIN(ts), MAX(ts) FROM price_bars"
        )
        total_rows, subnet_count, min_ts, max_ts = cur.fetchone()
    if total_rows == 0:
        return {"rows": 0, "subnets": 0, "span_days": 0.0}
    span_days = round((max_ts - min_ts) / 86400.0, 2) if min_ts and max_ts else 0.0
    return {
        "rows": int(total_rows),
        "subnets": int(subnet_count or 0),
        "span_days": span_days,
        "newest_ts": int(max_ts) if max_ts else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fetch and refresh — the workhorses
# ─────────────────────────────────────────────────────────────────────────────

def _stale_netuids(candidate_netuids: list[int], max_age_h: float) -> list[int]:
    """Return the subset of candidates whose newest stored bar is older than
    max_age_h (or who have no stored bars at all).

    One query for the batch — avoids 129 SELECT MAX round-trips.
    """
    if not candidate_netuids:
        return []
    _ensure_schema()
    cutoff_ts = int(time.time() - max_age_h * 3600)
    with _connect() as conn:
        # Newest bar per netuid across the candidate set.
        placeholders = ",".join("?" * len(candidate_netuids))
        cur = conn.execute(
            f"SELECT netuid, MAX(ts) FROM price_bars "
            f"WHERE netuid IN ({placeholders}) GROUP BY netuid",
            [int(n) for n in candidate_netuids],
        )
        newest = {int(nid): int(ts) for nid, ts in cur.fetchall() if ts is not None}
    stale: list[int] = []
    for nid in candidate_netuids:
        last = newest.get(int(nid))
        # Never seen (last is None) OR older than cutoff.
        if last is None or last < cutoff_ts:
            stale.append(int(nid))
    return stale


def _fetch_and_store_one(netuid: int) -> int:
    """Fetch one subnet's daily history from GeckoTerminal and store it.

    Returns bar count written (0 if GT had insufficient bars or errored).
    Never raises — GT is best-effort.
    """
    try:
        # Lazy import so this module works standalone (e.g. in a REPL or a
        # migration script) without the full run_scoring dependency graph.
        from geckoterminal_fetch import fetch_daily_closes
    except Exception as e:  # noqa: BLE001
        _diag(f"SN{netuid}: geckoterminal_fetch unavailable ({e}) — skip")
        return 0

    closes, stamps_iso = fetch_daily_closes(netuid, limit=BARS_TO_FETCH)
    if len(closes) < MIN_BARS_FOR_VALID:
        # Too sparse (brand-new pool, GT-less subnet) — leave the store alone
        # so the read path falls through to the flat synthetic fallback.
        return 0
    # Convert ISO timestamps back to epoch seconds for storage (SQLite indexes
    # ints faster than strings and we already have the epoch upstream, but the
    # GT wrapper returns ISO — so parse here to preserve the wrapper's shape).
    bars: list[tuple[int, float]] = []
    for c, s in zip(closes, stamps_iso):
        try:
            ts = int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        bars.append((ts, float(c)))
    if not bars:
        return 0
    return record_bars(netuid, bars)


def top_up_stale(
    candidate_netuids: list[int],
    max_age_h: float = STALE_HOURS,
    max_to_fetch: Optional[int] = None,
) -> dict:
    """Refresh subnets whose newest bar is older than max_age_h.

    Called every cron. Amortises the GT budget across 4 crons/day so any
    single cron only spends ~2 min fetching.

    `max_to_fetch` optionally caps the batch size per cron — a safety valve
    in case the store ends up broadly stale (e.g. after a multi-day outage).
    None = no cap; refresh everything that qualifies.

    Returns {"stale_count": N, "written": M, "elapsed_s": T} for the cron log.
    """
    init_db()
    start = time.time()
    stale = _stale_netuids(candidate_netuids, max_age_h)
    if not stale:
        _diag(f"top-up: all {len(candidate_netuids)} subnets fresh (<{max_age_h}h)")
        return {"stale_count": 0, "written": 0, "elapsed_s": 0.0}
    if max_to_fetch is not None and len(stale) > max_to_fetch:
        _diag(
            f"top-up: {len(stale)} stale but capped to {max_to_fetch} this cron"
        )
        stale = stale[:max_to_fetch]

    _diag(f"top-up: fetching {len(stale)} subnets (of {len(candidate_netuids)} candidates)")
    total_written = 0
    ok_count = 0
    empty_count = 0
    for nid in stale:
        written = _fetch_and_store_one(nid)
        if written > 0:
            total_written += written
            ok_count += 1
        else:
            empty_count += 1
    elapsed = round(time.time() - start, 1)
    _diag(
        f"top-up: done — {ok_count} refreshed ({total_written} rows), "
        f"{empty_count} sparse/missing, {elapsed}s"
    )
    return {
        "stale_count": len(stale),
        "written": total_written,
        "empty": empty_count,
        "elapsed_s": elapsed,
    }


def backfill_full(candidate_netuids: list[int]) -> dict:
    """One-off full-history backfill for every candidate subnet.

    Auto-triggered by `ensure_ready()` when the store is empty. Can also be
    invoked explicitly via `python subnet_price_history.py --backfill`.

    Runs ~9 min for 129 subnets at 4s GT spacing. Never raises — always
    returns a summary dict.
    """
    init_db()
    start = time.time()
    _diag(f"backfill: starting full history for {len(candidate_netuids)} subnets")
    total_written = 0
    ok = 0
    empty = 0
    for nid in candidate_netuids:
        written = _fetch_and_store_one(nid)
        if written > 0:
            total_written += written
            ok += 1
        else:
            empty += 1
    elapsed = round(time.time() - start, 1)
    _diag(
        f"backfill: done — {ok} subnets, {total_written} rows, "
        f"{empty} sparse/missing, {elapsed}s"
    )
    return {
        "subnets": len(candidate_netuids),
        "written": total_written,
        "ok": ok,
        "empty": empty,
        "elapsed_s": elapsed,
    }


def ensure_ready(candidate_netuids: list[int]) -> dict:
    """Idempotent readiness gate called at the top of each cron.

    Cheap fast path: if the store already has rows, just return stats — a
    later `top_up_stale` call will handle refresh. Slow path (empty store):
    run the full backfill first, then return stats.

    Safe to call on every cron; only the first-ever run pays the backfill
    cost.
    """
    init_db()
    s = stats()
    if s["rows"] == 0:
        _diag("first-ever run — store is empty, running full backfill")
        backfill_full(candidate_netuids)
        s = stats()
        _diag(
            f"backfill complete — store now has {s['rows']} rows across "
            f"{s['subnets']} subnets ({s['span_days']}d span)"
        )
    else:
        _diag(
            f"store ready — {s['rows']} rows, {s['subnets']} subnets, "
            f"{s['span_days']}d span"
        )
    return s


# ─────────────────────────────────────────────────────────────────────────────
# CLI — for manual backfill / inspection
# ─────────────────────────────────────────────────────────────────────────────

def _cli() -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="subnet_price_history")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="Print store stats and exit")
    sub.add_parser("init", help="Create the DB and schema if missing")

    p_backfill = sub.add_parser(
        "backfill",
        help="One-off full backfill across a set of subnets (comma-separated)",
    )
    p_backfill.add_argument("--netuids", required=True,
                            help="Comma-separated netuid list (e.g. 1,2,3,4,...)")

    p_show = sub.add_parser("show", help="Show stored bars for one netuid")
    p_show.add_argument("netuid", type=int)
    p_show.add_argument("--limit", type=int, default=20)

    p_topup = sub.add_parser(
        "topup",
        help="Refresh stale subnets from a comma-separated candidate list",
    )
    p_topup.add_argument("--netuids", required=True,
                         help="Comma-separated netuid list")
    p_topup.add_argument("--max-age-h", type=float, default=STALE_HOURS)

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "init":
        init_db()
        print(f"initialised at {DB_PATH}")
        return 0

    if args.cmd == "stats":
        print(stats())
        return 0

    if args.cmd == "show":
        closes, stamps = get_bars(args.netuid, limit=args.limit)
        if not closes:
            print(f"SN{args.netuid}: no bars stored")
            return 0
        print(f"SN{args.netuid}: {len(closes)} bars (most recent first)")
        for c, s in zip(reversed(closes), reversed(stamps)):
            print(f"  {s}  {c:.8f}τ")
        return 0

    ids = [int(x.strip()) for x in args.netuids.split(",") if x.strip()]

    if args.cmd == "backfill":
        result = backfill_full(ids)
        print(result)
        return 0

    if args.cmd == "topup":
        result = top_up_stale(ids, max_age_h=args.max_age_h)
        print(result)
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(_cli())
