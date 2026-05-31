"""
TAO Monitor — Price History Cache (SQLite)
==========================================
Stores per-subnet price snapshots on Infinity8 for:
  1. Markov regime detection (needs 60+ bars minimum)
  2. 72 EMA trend calculation
  3. 7-day momentum scoring

Designed for Infinity8 SSH — SQLite has no server dependency,
zero config, persists across cron runs.

Schema:
  subnet_prices(subnet_id, timestamp, price, pool_depth, volume_24h)

Usage:
    from price_cache import PriceCache

    cache = PriceCache("/home/user/tao_monitor/price_history.db")
    cache.upsert(subnet_id=4, price=0.0538, pool_depth=130000.0)
    history = cache.get_history(subnet_id=4, limit=200)
    # returns list of (timestamp, price) newest-first

Integration with run_scoring.py:
    After fetching pool data, call cache.upsert() for each subnet.
    Before scoring, call cache.enrich_metrics() to replace short
    price_history in SubnetMetrics with full cached history.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("price_cache")

# Default DB path — override in production
DEFAULT_DB_PATH = Path.home() / "tao_monitor" / "price_history.db"

# How many bars to return for scoring (72 EMA needs 72+, Markov needs 60+)
DEFAULT_HISTORY_LIMIT = 200


class PriceCache:
    """SQLite-backed price history cache for all subnets."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subnet_prices (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    subnet_id    INTEGER NOT NULL,
                    timestamp    TEXT    NOT NULL,
                    price        REAL    NOT NULL,
                    pool_depth   REAL    DEFAULT 0.0,
                    volume_24h   REAL    DEFAULT 0.0,
                    UNIQUE(subnet_id, timestamp)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_subnet_ts
                ON subnet_prices(subnet_id, timestamp DESC)
            """)
            # Metadata table for tracking last fetch per subnet
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fetch_meta (
                    subnet_id      INTEGER PRIMARY KEY,
                    last_fetch_ts  TEXT NOT NULL,
                    bar_count      INTEGER DEFAULT 0
                )
            """)
        logger.info(f"Price cache initialised: {self.db_path}")

    def upsert(
        self,
        subnet_id: int,
        price: float,
        pool_depth: float = 0.0,
        volume_24h: float = 0.0,
        timestamp: str | None = None,
    ) -> bool:
        """Insert or ignore a price snapshot.

        Uses current UTC time if timestamp not provided.
        UNIQUE constraint on (subnet_id, timestamp) prevents duplicates.
        Uses minute-level precision to avoid duplicate cron inserts.
        """
        if timestamp is None:
            # Round to nearest minute for idempotency across retries
            now = datetime.now(timezone.utc)
            timestamp = now.strftime("%Y-%m-%dT%H:%M:00Z")

        if price <= 0:
            return False

        with self._conn() as conn:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO subnet_prices
                        (subnet_id, timestamp, price, pool_depth, volume_24h)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (subnet_id, timestamp, price, pool_depth, volume_24h),
                )
                # Update metadata
                conn.execute(
                    """
                    INSERT INTO fetch_meta (subnet_id, last_fetch_ts, bar_count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(subnet_id) DO UPDATE SET
                        last_fetch_ts = excluded.last_fetch_ts,
                        bar_count = (
                            SELECT COUNT(*) FROM subnet_prices WHERE subnet_id = ?
                        )
                    """,
                    (subnet_id, timestamp, subnet_id),
                )
                return True
            except Exception as e:
                logger.warning(f"upsert failed for SN{subnet_id}: {e}")
                return False

    def upsert_batch(
        self,
        subnet_id: int,
        prices: list[float],
        timestamps: list[str],
        pool_depths: list[float] | None = None,
    ) -> int:
        """Bulk insert historical prices. Returns number inserted."""
        if not prices or len(prices) != len(timestamps):
            return 0

        if pool_depths is None:
            pool_depths = [0.0] * len(prices)

        rows = [
            (subnet_id, ts, p, d, 0.0)
            for ts, p, d in zip(timestamps, prices, pool_depths)
            if p > 0
        ]

        with self._conn() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO subnet_prices
                    (subnet_id, timestamp, price, pool_depth, volume_24h)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
            inserted = conn.execute(
                "SELECT changes()"
            ).fetchone()[0]

        logger.info(f"SN{subnet_id}: bulk inserted {inserted}/{len(rows)} rows")
        return inserted

    def get_history(
        self,
        subnet_id: int,
        limit: int = DEFAULT_HISTORY_LIMIT,
        oldest_first: bool = True,
    ) -> list[dict]:
        """Fetch price history for a subnet.

        Returns list of dicts with keys: timestamp, price, pool_depth
        Sorted oldest-first by default (required for Markov + EMA).
        """
        order = "ASC" if oldest_first else "DESC"
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT timestamp, price, pool_depth, volume_24h
                FROM subnet_prices
                WHERE subnet_id = ?
                ORDER BY timestamp {order}
                LIMIT ?
                """,
                (subnet_id, limit),
            ).fetchall()

        return [dict(r) for r in rows]

    def get_prices_only(
        self,
        subnet_id: int,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> tuple[list[float], list[str]]:
        """Returns (prices, timestamps) oldest-first — for SubnetMetrics."""
        history = self.get_history(subnet_id, limit=limit, oldest_first=True)
        prices = [r["price"] for r in history]
        timestamps = [r["timestamp"] for r in history]
        return prices, timestamps

    def bar_count(self, subnet_id: int) -> int:
        """How many price bars are cached for this subnet."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM subnet_prices WHERE subnet_id = ?",
                (subnet_id,),
            ).fetchone()
        return row[0] if row else 0

    def all_subnet_counts(self) -> dict[int, int]:
        """Returns {subnet_id: bar_count} for all subnets in cache."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT subnet_id, COUNT(*) as cnt FROM subnet_prices GROUP BY subnet_id"
            ).fetchall()
        return {r["subnet_id"]: r["cnt"] for r in rows}

    def enrich_metrics(self, metrics_list, min_bars: int = 9) -> None:
        """In-place: replace short price_history in SubnetMetrics with cached history.

        Modifies the list in place. Call this after fetching pool data,
        before running the scoring cycle.

        Subnets with fewer than min_bars cached are left with API history
        (or empty, which triggers FAIL_NO_DATA pre-filter).
        """
        enriched = 0
        for m in metrics_list:
            cached_prices, cached_ts = self.get_prices_only(m.subnet_id)
            if len(cached_prices) >= min_bars:
                m.price_history = cached_prices
                m.timestamps = cached_ts
                enriched += 1

        logger.info(f"Enriched {enriched}/{len(metrics_list)} subnets with cached history")

    def prune_old(self, keep_bars: int = 500) -> int:
        """Delete oldest rows beyond keep_bars per subnet. Returns rows deleted."""
        with self._conn() as conn:
            # Get subnet IDs with more rows than keep_bars
            over_limit = conn.execute(
                """
                SELECT subnet_id, COUNT(*) as cnt
                FROM subnet_prices
                GROUP BY subnet_id
                HAVING cnt > ?
                """,
                (keep_bars,),
            ).fetchall()

            deleted = 0
            for row in over_limit:
                sid = row["subnet_id"]
                # Keep the newest keep_bars rows
                conn.execute(
                    """
                    DELETE FROM subnet_prices
                    WHERE subnet_id = ?
                    AND id NOT IN (
                        SELECT id FROM subnet_prices
                        WHERE subnet_id = ?
                        ORDER BY timestamp DESC
                        LIMIT ?
                    )
                    """,
                    (sid, sid, keep_bars),
                )
                deleted += conn.execute("SELECT changes()").fetchone()[0]

        if deleted:
            logger.info(f"Pruned {deleted} old price rows")
        return deleted

    def status(self) -> str:
        """Human-readable cache status summary."""
        counts = self.all_subnet_counts()
        if not counts:
            return "Cache empty — no data yet"

        total_rows = sum(counts.values())
        subnets_with_data = len(counts)
        ready_for_markov = sum(1 for c in counts.values() if c >= 60)
        ready_for_ema = sum(1 for c in counts.values() if c >= 72)

        lines = [
            f"Price cache: {self.db_path}",
            f"  Total rows:          {total_rows:,}",
            f"  Subnets tracked:     {subnets_with_data}",
            f"  Ready for Markov:    {ready_for_markov} (≥60 bars)",
            f"  Ready for 72 EMA:    {ready_for_ema} (≥72 bars)",
        ]

        # Show holdings specifically
        holdings = [0, 4, 51, 62, 64, 68, 75]
        lines.append(f"\n  Holdings bar counts:")
        for sid in holdings:
            c = counts.get(sid, 0)
            flag = "✓" if c >= 60 else ("~" if c >= 9 else "✗")
            lines.append(f"    SN{sid:>3d}: {c:>4d} bars  {flag}")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Integration helper — drop into run_scoring.py
# ─────────────────────────────────────────────────────────────────────────────

def update_cache_from_metrics(cache: PriceCache, metrics_list) -> None:
    """Persist current prices from a freshly-fetched metrics list.

    Call this every 30-min cron cycle BEFORE enriching metrics.
    This grows the cache one bar per cycle (~48 bars/day).
    """
    for m in metrics_list:
        if m.token_price > 0:
            cache.upsert(
                subnet_id=m.subnet_id,
                price=m.token_price,
                pool_depth=m.pool_depth,
                volume_24h=m.volume_24h,
            )


# ─────────────────────────────────────────────────────────────────────────────
# CLI — run directly for status / backfill
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import os
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="TAO Monitor price cache tool")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite DB path")
    subparsers = parser.add_subparsers(dest="cmd")

    # Status
    subparsers.add_parser("status", help="Show cache status")

    # Backfill from Taostats history endpoint
    bp = subparsers.add_parser("backfill", help="Backfill history from Taostats API")
    bp.add_argument("--api-key", default=os.environ.get("TAOSTATS_API_KEY"))
    bp.add_argument("--netuids", default="4,51,62,64,68,75",
                    help="Comma-separated subnet IDs (default: Simon's holdings)")
    bp.add_argument("--limit", type=int, default=200,
                    help="Bars to fetch per subnet (default: 200)")

    # Prune
    subparsers.add_parser("prune", help="Prune old rows (keep 500/subnet)")

    args = parser.parse_args()
    cache = PriceCache(args.db)

    if args.cmd == "status" or args.cmd is None:
        print(cache.status())

    elif args.cmd == "backfill":
        if not args.api_key:
            print("ERROR: --api-key required or set TAOSTATS_API_KEY")
            sys.exit(1)

        import time
        import requests

        netuids = [int(x.strip()) for x in args.netuids.split(",")]
        print(f"Backfilling {len(netuids)} subnets from Taostats pool/history...")

        for netuid in netuids:
            print(f"\n  SN{netuid}: fetching {args.limit} bars...", end=" ", flush=True)
            time.sleep(13)  # rate limit
            try:
                url = f"https://api.taostats.io/api/dtao/pool/history/v1"
                resp = requests.get(
                    url,
                    headers={"Authorization": args.api_key, "Accept": "application/json"},
                    params={"netuid": netuid, "limit": args.limit},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                entries = data.get("data", [])

                if not entries:
                    print(f"no data returned")
                    continue

                # Inspect first entry to find the right field names
                first = entries[0]
                price_field = next(
                    (f for f in ["price", "alpha_price", "token_price"] if f in first), None
                )
                ts_field = next(
                    (f for f in ["timestamp", "time", "created_at", "block_timestamp"] if f in first), None
                )

                if not price_field or not ts_field:
                    print(f"unknown fields. Keys: {list(first.keys())}")
                    continue

                prices = []
                timestamps = []
                for e in entries:
                    p = float(e.get(price_field, 0) or 0)
                    t = str(e.get(ts_field, ""))
                    if p > 0 and t:
                        prices.append(p)
                        timestamps.append(t)

                # Sort oldest first
                if timestamps and timestamps[0] > timestamps[-1]:
                    prices.reverse()
                    timestamps.reverse()

                inserted = cache.upsert_batch(netuid, prices, timestamps)
                print(f"inserted {inserted}/{len(prices)} rows "
                      f"(price_field={price_field!r}, ts_field={ts_field!r})")

            except Exception as e:
                print(f"FAILED: {e}")

        print(f"\n{cache.status()}")

    elif args.cmd == "prune":
        deleted = cache.prune_old(keep_bars=500)
        print(f"Pruned {deleted} rows")
        print(cache.status())
