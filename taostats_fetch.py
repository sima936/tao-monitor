"""
TAO Monitor — Data Fetch Layer
================================
Pulls real subnet metrics from the Taostats API and maps them
to SubnetMetrics objects for the scoring engine.

Primary endpoint: GET /api/dtao/pool/latest/v1
  - Returns price, liquidity, volume, 7-day price history, sentiment
  - One call per subnet, or omit netuid for all subnets

Secondary endpoint: GET /api/dtao/metagraph/latest/v1?netuid=N
  - Returns all neurons with stake amounts
  - Used to compute wallet concentration (Genie-equivalent)

Rate limit: 5 calls/min on free tier.
Strategy: fetch all pools in one call (no netuid param), then
selectively fetch metagraph for subnets that pass initial filters.

Usage:
    from taostats_fetch import TaostatsClient, fetch_all_subnet_metrics

    client = TaostatsClient(api_key="tao-xxxxx:yyyyyy")
    metrics = fetch_all_subnet_metrics(client)
    # metrics is a list of SubnetMetrics ready for run_scoring_cycle()

Dependencies: requests (add to requirements.txt)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

# Import the scoring engine's data structure
try:
    from subnet_scoring_engine import SubnetMetrics
except ImportError:
    # Fallback if running standalone — define minimal SubnetMetrics
    from dataclasses import field as _field

    @dataclass
    class SubnetMetrics:  # type: ignore[no-redef]
        subnet_id: int
        name: str
        token_price: float
        pool_depth: float
        genie_score: float
        price_history: list[float]
        timestamps: list[str]
        volume_24h: float = 0.0
        volume_7d: float = 0.0


logger = logging.getLogger("taostats_fetch")

class TaostatsCreditsExhausted(RuntimeError):
    """Raised on a 429 whose body is "Insufficient credits" (quota at zero).

    Distinct from a transient rate-limit blip: a retry will NEVER recover it,
    so callers should alert specifically and hold, not loop on backoff."""

# ─────────────────────────────────────────────────────────────────────────────
# API Client
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL = "https://api.taostats.io"

# Endpoints
POOL_LATEST = "/api/dtao/pool/latest/v1"
POOL_HISTORY = "/api/dtao/pool/history/v1"
METAGRAPH_LATEST = "/api/metagraph/latest/v1"
SUBNET_INFO = "/api/dtao/subnet/latest/v1"
STAKE_BALANCE = "/api/dtao/stake_balance/latest/v1"
DELEGATION = "/api/delegation/v1"   # staking/delegation events (buys & sells)
ACCOUNT_LATEST = "/api/account/latest/v1"   # coldkey balance: free / staked / total

# Default wallet — Simon's coldkey
DEFAULT_COLDKEY = "5HR3cMSEnyzQbGCqgeHHQxCosgCBDi6a2tkWiBE3XCwUsmNR"


class TaostatsClient:
    """Thin wrapper around the Taostats API with rate limiting."""

    def __init__(self, api_key: str, rate_limit_delay: float = 12.5,
                 max_retries: int = 1, backoff_base: float = 3.0,
                 connect_timeout: float = 8.0, read_timeout: float = 25.0):
        """
        api_key: Your taostats API key (format: tao-xxxxx:yyyyyy)
        rate_limit_delay: Seconds between calls (12.5s = ~5/min for free tier)
        max_retries: Extra attempts after the first on TRANSIENT failures only
            (read/connect timeout, 429, 5xx). Default 1 (→ 2 attempts total) so
            a single blip recovers without endangering the 60s /status budget.
            Retries fire only on failure — the happy path is unchanged.
        backoff_base: Base seconds for exponential backoff between retries.
        connect_timeout / read_timeout: split (connect, read) timeouts. The read
            timeout is the one that bites on Taostats; 25s leaves headroom for a
            retry under the /status budget.
        """
        self.api_key = api_key
        self.rate_limit_delay = rate_limit_delay
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = (connect_timeout, read_timeout)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": api_key,
            "Accept": "application/json",
        })
        self._last_call_time = 0.0

    def _rate_limit(self):
        """Enforce minimum delay between API calls."""
        elapsed = time.time() - self._last_call_time
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)
        self._last_call_time = time.time()

    def _backoff(self, attempt: int, retry_after=None, rate_limited: bool = False) -> float:
        """Seconds to wait before the next attempt.

        Honours a server Retry-After when present. For 429s the floor is the
        rate-limit delay (retrying faster would just 429 again); other transient
        errors use the short exponential base.
        """
        if retry_after:
            try:
                return max(self.backoff_base, float(retry_after))
            except (TypeError, ValueError):
                pass
        base = self.rate_limit_delay if rate_limited else self.backoff_base
        return base * (2 ** (attempt - 1))

    def get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """GET with rate limiting and transient-failure retry.

        Retries ONLY on read/connect timeouts, HTTP 429, and 5xx — these are
        the blips that abort an otherwise-fine cron run. 4xx (bad params, auth)
        fail fast, no retry. Rate limiting is enforced once up front; retry
        spacing comes from the backoff, so we don't double-pay the 12.5s gate.
        """
        self._rate_limit()
        url = f"{BASE_URL}{endpoint}"
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 2):  # 1 + max_retries attempts
            # --- network layer: timeouts / connection errors are transient ---
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                last_exc = e
                if attempt <= self.max_retries:
                    wait = self._backoff(attempt)
                    logger.warning(
                        f"{type(e).__name__} on {url} "
                        f"(attempt {attempt}/{self.max_retries + 1}); retry in {wait:.0f}s"
                    )
                    time.sleep(wait)
                    continue
                logger.error(f"Request failed for {url} after {attempt} attempts: {e}")
                raise

            # --- HTTP layer: 429 + 5xx are transient, everything else is final ---
            status = resp.status_code

            # Quota exhaustion arrives as a 429 but is NOT transient — retrying
            # only burns the /status budget and still fails. Detect it and raise
            # a distinct error so the caller can alert specifically.
            if status == 429:
                try:
                    _msg = str(resp.json().get("message", ""))
                except Exception:
                    _msg = resp.text or ""
                if "insufficient credits" in _msg.lower() or "remaining: 0" in _msg.lower():
                    logger.error(f"Taostats credits exhausted for {url}: {_msg}")
                    raise TaostatsCreditsExhausted(_msg or "Insufficient taostats credits")

            if status == 429 or 500 <= status < 600:
                last_exc = requests.exceptions.HTTPError(f"HTTP {status} for {url}")
                if attempt <= self.max_retries:
                    wait = self._backoff(
                        attempt,
                        retry_after=resp.headers.get("Retry-After"),
                        rate_limited=(status == 429),
                    )
                    logger.warning(
                        f"HTTP {status} on {url} "
                        f"(attempt {attempt}/{self.max_retries + 1}); retry in {wait:.0f}s"
                    )
                    time.sleep(wait)
                    continue
                logger.error(f"HTTP {status} for {url} after {attempt} attempts")
                resp.raise_for_status()

            # 2xx success, or a deterministic 4xx → fail fast.
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                logger.error(f"HTTP error {status} for {url}: {e}")
                raise
            return resp.json()

        # Loop exhausted (defensive — the raises above normally return/raise first).
        if last_exc:
            raise last_exc
        raise requests.exceptions.RequestException(f"Exhausted retries for {url}")

    def get_all_pools(self) -> list[dict]:
        """Fetch pool data for ALL subnets in one call."""
        data = self.get(POOL_LATEST)
        return data.get("data", [])

    def get_pool(self, netuid: int) -> Optional[dict]:
        """Fetch pool data for a single subnet."""
        data = self.get(POOL_LATEST, params={"netuid": netuid})
        pools = data.get("data", [])
        return pools[0] if pools else None

    def get_pool_history(self, netuid: int, limit: int = 200) -> list[dict]:
        """Fetch historical pool snapshots for a subnet.

        Used to build extended price history beyond the 7-day window.
        """
        data = self.get(POOL_HISTORY, params={"netuid": netuid, "limit": limit})
        return data.get("data", [])

    def get_metagraph(self, netuid: int) -> list[dict]:
        """Fetch metagraph (all neurons) for a subnet.

        Used to compute wallet concentration (Genie equivalent).
        """
        data = self.get(METAGRAPH_LATEST, params={"netuid": netuid})
        return data.get("data", [])

    def get_wallet_stakes(self, coldkey: str = DEFAULT_COLDKEY) -> list[dict]:
        """Fetch all staked positions for a wallet from chain.

        Same endpoint the Gordie dashboard uses. Returns list of stake entries.
        """
        data = self.get(STAKE_BALANCE, params={"coldkey": coldkey, "limit": 100})
        return data.get("data", [])

    def get_free_balance_tao(self, coldkey: str = DEFAULT_COLDKEY) -> Optional[float]:
        """Free (unstaked / transferable) TAO for a coldkey, in TAO.

        GET /api/account/latest/v1?address={coldkey} → data[0].balance_free (rao).
        This is the wallet balance that get_wallet_stakes (stake positions only)
        does NOT see — the TAO sitting idle after an unstake/take-profit.

        Pure enrichment: returns None on ANY failure (network, schema, empty) so
        a missing free read can never break a scoring cycle. The caller treats
        None as "free unknown this run" and reports staked-only, as before.
        """
        try:
            data = self.get(ACCOUNT_LATEST, params={"address": coldkey, "limit": 1})
            rows = data.get("data", [])
            if not rows:
                logger.warning("Free-balance read: empty account response — skipping.")
                return None
            free_rao = rows[0].get("balance_free")
            if free_rao is None:
                logger.warning("Free-balance read: no balance_free field — skipping.")
                return None
            return float(free_rao) / 1e9
        except Exception as e:  # noqa: BLE001 — enrichment must never be fatal
            logger.warning(f"Free-balance read failed (non-fatal): {e}")
            return None

    def get_account_balances(self, coldkey: str = DEFAULT_COLDKEY) -> Optional[dict]:
        """Coldkey balances in TAO from a SINGLE account snapshot:
        {"free":.., "staked":.., "root":.., "total":..} (rao ÷ 1e9), or None.

        Why this exists alongside get_free_balance_tao: account-total must NOT be
        computed as (staked positions sum) + (balance_free), because those come
        from two different endpoints that desync during a stake move — right
        after parking free→SN0 the positions read updates but balance_free lags,
        so the same TAO is counted twice (the 43.2τ phantom). balance_total is
        INVARIANT through a park (TAO just shifts free→staked within one total),
        so it's the safe basis for the account total and for deriving free as a
        residual against the live staked sum.

        Pure enrichment: returns None on ANY failure so a missing read can never
        break a cycle.
        """
        try:
            data = self.get(ACCOUNT_LATEST, params={"address": coldkey, "limit": 1})
            rows = data.get("data", [])
            if not rows:
                logger.warning("Account-balance read: empty response — skipping.")
                return None
            r = rows[0]

            def _tao(key: str) -> Optional[float]:
                v = r.get(key)
                return float(v) / 1e9 if v is not None else None

            return {
                "free": _tao("balance_free"),
                "staked": _tao("balance_staked"),
                "root": _tao("balance_staked_root"),
                "total": _tao("balance_total"),
            }
        except Exception as e:  # noqa: BLE001 — enrichment must never be fatal
            logger.warning(f"Account-balance read failed (non-fatal): {e}")
            return None

    def get_delegation_events(
        self,
        nominator: str = DEFAULT_COLDKEY,
        page: int = 1,
        limit: int = 200,
        order: str = "timestamp_asc",
    ) -> dict:
        """One page of staking/delegation events (buys & sells) for a coldkey.

        Endpoint: GET /api/delegation/v1?nominator={coldkey}
        Each event: action DELEGATE|UNDELEGATE, amount (TAO leg, rao),
        alpha (rao), alpha_price_in_tao, netuid, is_transfer.
        Returns the full payload {pagination:{...}, data:[...]}.
        """
        return self.get(DELEGATION, params={
            "nominator": nominator,
            "page": page,
            "limit": limit,
            "order": order,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Dynamic holdings from chain
# ─────────────────────────────────────────────────────────────────────────────

def fetch_wallet_holdings(
    api_key: str,
    coldkey: str = DEFAULT_COLDKEY,
) -> list[int]:
    """Fetch the list of subnet IDs the wallet is currently staked in.

    Calls the same Taostats endpoint as the Gordie dashboard.
    Returns sorted list of subnet IDs. Falls back to empty list on error.
    """
    try:
        client = TaostatsClient(api_key=api_key, rate_limit_delay=0.5)
        stakes = client.get_wallet_stakes(coldkey)
        subnet_ids = set()
        for entry in stakes:
            netuid = entry.get("netuid")
            if netuid is None:
                netuid = entry.get("subnet_id")
            if netuid is not None:
                subnet_ids.add(int(netuid))
        result = sorted(subnet_ids)
        logger.info(f"Wallet holdings from chain: {result} ({len(result)} subnets)")
        return result
    except Exception as e:
        logger.error(f"Failed to fetch wallet holdings: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Cost basis from on-chain stake events (auto — replaces manual entry)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_cost_basis(
    api_key: str,
    coldkey: str = DEFAULT_COLDKEY,
    max_pages: int = 25,
    rate_limit_delay: float = 12.5,
) -> dict:
    """Compute per-subnet cost basis from the coldkey's full stake-event history.

    Method (TAO-denominated, no accounting-method ambiguity):
        net_invested[netuid] = Σ TAO in (DELEGATE) − Σ TAO out (UNDELEGATE)

    P&L is then (current balance_as_tao − net_invested), computed downstream.
    This auto-handles staking rewards correctly: emission alpha is not a
    DELEGATE event, so it shows up as extra value at zero cost.

    Pages /api/delegation/v1 oldest→newest until exhausted (or max_pages hit),
    bucketing by netuid. One coldkey's whole history, so it's a few calls, not
    one-per-subnet. Returns a dashboard-ready dict:

        {
          "positions": { "<netuid>": {
              "tao_invested": float,   # net TAO in (may be <=0 = house money)
              "tao_in": float, "tao_out": float,
              "n_events": int, "transfers": int
          }, ... },
          "_source": "taostats_delegation",
          "_computed": iso8601,
          "_coldkey": coldkey,
          "_pages": int,
          "_capped": bool,           # True if max_pages hit before history end
          "_total_events": int
        }

    `amount` is the TAO leg in rao (÷1e9). Fees (sub-milliTAO) are ignored.
    Transfer events (is_transfer truthy) are counted but also tallied separately
    so the dashboard can flag positions whose basis may be incomplete.
    """
    client = TaostatsClient(api_key=api_key, rate_limit_delay=rate_limit_delay)

    buckets: dict[int, dict] = {}
    pages = 0
    capped = False
    total_events = 0

    page = 1
    while page <= max_pages:
        try:
            payload = client.get_delegation_events(coldkey, page=page, limit=200)
        except Exception as e:
            logger.warning(f"Cost-basis: delegation page {page} failed: {e}")
            break

        pages += 1
        events = payload.get("data", []) or []
        for ev in events:
            total_events += 1
            netuid = ev.get("netuid")
            if netuid is None:
                continue
            netuid = int(netuid)
            b = buckets.setdefault(
                netuid,
                {"tao_in": 0.0, "tao_out": 0.0, "n_events": 0, "transfers": 0},
            )
            try:
                tao = float(ev.get("amount", 0) or 0) / 1e9
            except (TypeError, ValueError):
                tao = 0.0
            action = str(ev.get("action", "")).upper()
            if action == "DELEGATE":
                b["tao_in"] += tao
            elif action == "UNDELEGATE":
                b["tao_out"] += tao
            else:
                # Unknown action — skip the amount but record it happened.
                pass
            b["n_events"] += 1
            if ev.get("is_transfer"):
                b["transfers"] += 1

        pag = payload.get("pagination", {}) or {}
        next_page = pag.get("next_page")
        if not next_page:
            break
        page = int(next_page)
    else:
        # Loop exhausted max_pages without a natural break → history truncated.
        capped = True

    positions = {}
    for netuid, b in buckets.items():
        positions[str(netuid)] = {
            "tao_invested": round(b["tao_in"] - b["tao_out"], 6),
            "tao_in": round(b["tao_in"], 6),
            "tao_out": round(b["tao_out"], 6),
            "n_events": b["n_events"],
            "transfers": b["transfers"],
        }

    logger.info(
        f"Cost basis: {len(positions)} subnets from {total_events} events "
        f"over {pages} page(s){' (CAPPED)' if capped else ''}"
    )

    from datetime import datetime, timezone
    return {
        "positions": positions,
        "_source": "taostats_delegation",
        "_computed": datetime.now(timezone.utc).isoformat(),
        "_coldkey": coldkey,
        "_pages": pages,
        "_capped": capped,
        "_total_events": total_events,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Wallet Concentration (Genie-equivalent)
# ─────────────────────────────────────────────────────────────────────────────

def compute_gini_coefficient(stakes: list[float]) -> float:
    """Compute Gini coefficient from a list of stake amounts.

    0.0 = perfectly equal distribution
    1.0 = one wallet holds everything

    This is the Genie-equivalent metric. Siam's threshold is 0.85.
    """
    if not stakes or len(stakes) < 2:
        return 0.0

    stakes = sorted(stakes)
    n = len(stakes)
    total = sum(stakes)

    if total == 0:
        return 0.0

    # Standard Gini formula
    cumulative = 0.0
    for i, s in enumerate(stakes):
        cumulative += (2 * (i + 1) - n - 1) * s

    return cumulative / (n * total)


def compute_top_holder_concentration(stakes: list[float], top_n: int = 10) -> float:
    """What % of total stake is held by the top N wallets.

    Alternative to Gini — more intuitive for Siam's framework.
    Returns 0-1 (0 = no concentration, 1 = top N hold everything).
    """
    if not stakes:
        return 0.0

    total = sum(stakes)
    if total == 0:
        return 0.0

    sorted_stakes = sorted(stakes, reverse=True)
    top_sum = sum(sorted_stakes[:top_n])

    return top_sum / total


def concentration_from_metagraph(metagraph_data: list[dict]) -> float:
    """Extract stake amounts from metagraph and compute Gini.

    The metagraph endpoint returns neurons with stake info.
    We aggregate by coldkey to get per-wallet totals.
    """
    # Aggregate stakes by coldkey (unique wallet)
    wallet_stakes: dict[str, float] = {}

    for neuron in metagraph_data:
        # The metagraph returns stake per hotkey — group by coldkey
        coldkey = neuron.get("coldkey", {})
        if isinstance(coldkey, dict):
            coldkey_addr = coldkey.get("ss58", "unknown")
        else:
            coldkey_addr = str(coldkey)

        # Stake might be in rao (divide by 1e9) or TAO — check the field
        stake = neuron.get("alpha_stake") or neuron.get("stake", 0)
        if isinstance(stake, str):
            stake = float(stake)

        # Convert from rao to TAO if the value is very large
        if stake > 1_000_000:
            stake = stake / 1e9

        wallet_stakes[coldkey_addr] = wallet_stakes.get(coldkey_addr, 0) + stake

    stakes = list(wallet_stakes.values())
    if not stakes:
        return 0.0

    return compute_gini_coefficient(stakes)


# ─────────────────────────────────────────────────────────────────────────────
# Pool data → SubnetMetrics mapping
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(val, default=0.0) -> float:
    """Safely convert API values to float."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def pool_to_metrics(
    pool: dict,
    genie_score: float = 0.5,  # default if metagraph not fetched yet
) -> SubnetMetrics:
    """Convert a taostats pool/latest response to a SubnetMetrics object.

    Known fields from /api/dtao/pool/latest/v1:
      - netuid: int
      - name: str (subnet name)
      - price: str (alpha price in TAO)
      - total_tao: str (TAO in pool = pool depth)
      - market_cap: str
      - tao_volume_24_hr: str
      - seven_day_prices: list[dict] with {price, timestamp} entries
      - price_change_1_hour, price_change_1_day, price_change_1_week: str
      - fear_and_greed_index: float
      - fear_and_greed_sentiment: str
      - liquidity: str
      - buys_24_hr, sells_24_hr: str
      - highest_price_24_hr, lowest_price_24_hr: str
    """
    netuid = int(pool.get("netuid", 0))
    name = pool.get("name", f"SN{netuid}")

    # Token price in TAO
    token_price = _safe_float(pool.get("price"))

    # Pool depth = TAO in the liquidity pool
    pool_depth = _safe_float(pool.get("total_tao"))
    # If total_tao is in rao, convert
    if pool_depth > 1_000_000:
        pool_depth = pool_depth / 1e9

    # Volume
    volume_24h = _safe_float(pool.get("tao_volume_24_hr"))
    if volume_24h > 1_000_000:
        volume_24h = volume_24h / 1e9

    # Price history from seven_day_prices
    seven_day = pool.get("seven_day_prices", [])
    price_history = []
    timestamps = []

    if isinstance(seven_day, list):
        for entry in seven_day:
            if isinstance(entry, dict):
                p = _safe_float(entry.get("price"))
                t = entry.get("timestamp", "")
                if p > 0:
                    price_history.append(p)
                    timestamps.append(t)
            elif isinstance(entry, (int, float, str)):
                # Some API versions return just a list of prices
                p = _safe_float(entry)
                if p > 0:
                    price_history.append(p)
                    timestamps.append("")

    # Ensure chronological order (oldest first)
    if timestamps and timestamps[0] > timestamps[-1]:
        price_history.reverse()
        timestamps.reverse()

    return SubnetMetrics(
        subnet_id=netuid,
        name=name,
        token_price=token_price,
        pool_depth=pool_depth,
        genie_score=genie_score,
        price_history=price_history,
        timestamps=timestamps,
        volume_24h=volume_24h,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Full fetch pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _synthetic_history(
    current_price: float,
    pct_change_1d: float,
    pct_change_1w: float,
) -> list[float]:
    """Build a 9-bar synthetic price history from pct-change anchors.

    Interpolates linearly between price 7 days ago, 1 day ago, and now.
    Returns oldest-first. Used when both pool/latest and pool/history
    fail to provide enough bars.
    """
    if current_price <= 0:
        return []
    price_1d = current_price / (1 + pct_change_1d / 100) if pct_change_1d else current_price
    price_7d = current_price / (1 + pct_change_1w / 100) if pct_change_1w else current_price
    # 7 evenly-spaced points from 7d→1d, then midpoint and now (9 total)
    segment = [price_7d + (price_1d - price_7d) * i / 6 for i in range(7)]
    segment.append((price_1d + current_price) / 2)
    segment.append(current_price)
    return [round(p, 8) for p in segment]


def fetch_subnet_identities(client: TaostatsClient) -> dict[int, dict]:
    """Fetch on-chain subnet identity for every netuid — one call, structured JSON.

    Uses the /api/subnet/identity/v1 endpoint (docs.taostats.io). Returns
    {netuid: {name, url, github, discord, description, contact}}. Empty dict on
    failure so callers degrade cleanly (alerts still fire, just without the
    enrichment fields). Costs one taostats API call per invocation — cheap
    enough to run once per cron.

    This is the authoritative source for subnet metadata: owners set identity
    on-chain via extrinsic, taostats reflects it within minutes. Makes 🆕 and
    dereg alerts self-describing (name + website + description + github) and
    doubles as an automatic rotation detector for fundamentals.json — SN15
    ORO, SN40 Ralph, SN58 greevils all show up here as their current identity.
    """
    import sys as _sys   # local import — keep the helper self-contained
    try:
        resp = client.get("/api/subnet/identity/v1", params={"limit": 200})
    except Exception as e:
        # Print to stderr as well as logger — bittensor hijacks logger on the
        # cron service, so [identity] prefix in Railway logs is the only way to
        # confirm this ran without crashing the digest.
        print(f"[identity] FETCH FAILED ({type(e).__name__}: {e})",
              file=_sys.stderr, flush=True)
        logger.warning(f"Subnet identity fetch failed: {e}")
        return {}
    out: dict[int, dict] = {}
    for row in (resp.get("data") or []):
        try:
            nid = int(row.get("netuid"))
        except (TypeError, ValueError):
            continue
        # Normalise nulls → empty string; strip whitespace. Keep only the
        # fields we consume in alerts (skip 'additional' — mostly promo text).
        def _s(k: str) -> str:
            v = row.get(k)
            return (str(v).strip() if v is not None else "")
        out[nid] = {
            "name":        _s("subnet_name"),
            "url":         _s("subnet_url"),
            "github":      _s("github_repo"),
            "discord":     _s("discord"),
            "description": _s("description"),
            "contact":     _s("subnet_contact"),
        }
    print(f"[identity] OK — {len(out)} netuids", file=_sys.stderr, flush=True)
    logger.info(f"Subnet identity fetch OK — {len(out)} netuids")
    return out


def fetch_all_subnet_metrics(
    client: TaostatsClient,
    fetch_concentration: bool = True,
    concentration_netuids: Optional[list[int]] = None,
) -> list[SubnetMetrics]:
    """Fetch metrics for all subnets and return SubnetMetrics list.

    Step 1: Single API call to get all pool data
    Step 2: Build synthetic price history for subnets with <9 bars
            (seven_day_prices no longer returned by pool/latest)
    Step 3: Optionally fetch metagraph for concentration scoring
            (expensive — 1 call per subnet, rate limited)

    If fetch_concentration is True but concentration_netuids is None,
    fetches metagraph only for subnets that pass the cheap pre-filters
    (price + pool depth) to minimize API calls.

    Args:
        client: TaostatsClient instance
        fetch_concentration: Whether to compute Genie scores from metagraph
        concentration_netuids: If set, only fetch metagraph for these netuids
    """
    logger.info("Fetching all subnet pools...")
    pools = client.get_all_pools()
    logger.info(f"Got {len(pools)} subnet pools")

    # First pass: convert pools to metrics with default genie
    metrics_map: dict[int, SubnetMetrics] = {}
    raw_pools: dict[int, dict] = {}
    for pool in pools:
        m = pool_to_metrics(pool, genie_score=0.5)  # placeholder
        metrics_map[m.subnet_id] = m
        raw_pools[m.subnet_id] = pool

    # Second pass: seven_day_prices no longer returned by pool/latest —
    # build synthetic history for every subnet missing >=9 bars.
    # Real history storage can be added later; for now synthetic is universal.
    thin = [nid for nid, m in metrics_map.items() if len(m.price_history) < 9]
    if thin:
        logger.info(f"Building synthetic history for {len(thin)} subnets with <9 bars...")
    for netuid in thin:
        pool = raw_pools[netuid]
        synth = _synthetic_history(
            metrics_map[netuid].token_price,
            _safe_float(pool.get("price_change_1_day")),
            _safe_float(pool.get("price_change_1_week")),
        )
        if synth:
            metrics_map[netuid].price_history = synth
            metrics_map[netuid].timestamps = []
        else:
            logger.warning(f"  SN{netuid}: price=0, cannot build synthetic history")

    # Third pass: fetch concentration for relevant subnets
    if fetch_concentration:
        target_netuids = concentration_netuids

        if target_netuids is None:
            # Only fetch metagraph for subnets passing cheap pre-filters
            from subnet_scoring_engine import MAX_TOKEN_PRICE, MIN_POOL_DEPTH, MAX_POOL_DEPTH
            target_netuids = [
                m.subnet_id for m in metrics_map.values()
                if (m.token_price < MAX_TOKEN_PRICE
                    and m.pool_depth > MIN_POOL_DEPTH
                    and m.pool_depth < MAX_POOL_DEPTH
                    and len(m.price_history) >= 9)
            ]

        # Hard cap: 20 subnets max — keeps cycle under 5min at 12.5s/call.
        # Always include current holdings first, then fill with others.
        HOLDINGS = [0, 4, 51, 62, 64, 68, 75]
        MAX_METAGRAPH_FETCHES = 20
        if len(target_netuids) > MAX_METAGRAPH_FETCHES:
            priority = [n for n in target_netuids if n in HOLDINGS]
            others   = [n for n in target_netuids if n not in HOLDINGS]
            target_netuids = (priority + others)[:MAX_METAGRAPH_FETCHES]

        logger.info(
            f"Fetching metagraph for {len(target_netuids)} subnets "
            f"(~{len(target_netuids) * 12.5:.0f}s at rate limit)..."
        )

        for netuid in target_netuids:
            try:
                metagraph = client.get_metagraph(netuid)
                genie = concentration_from_metagraph(metagraph)
                metrics_map[netuid].genie_score = genie
                logger.info(f"  SN{netuid}: Gini={genie:.3f}")
            except Exception as e:
                logger.warning(f"  SN{netuid}: metagraph fetch failed: {e}")
                # Keep default 0.5 — won't be filtered out

    return list(metrics_map.values())


def fetch_extended_history(
    client: TaostatsClient,
    netuid: int,
    limit: int = 200,
) -> tuple[list[float], list[str]]:
    """Fetch extended price history from pool/history endpoint.

    Returns (prices, timestamps) sorted oldest-first.
    Use this to build 72+ bar history for EMA calculation.

    Note: costs 1 API call per subnet. Use selectively.
    """
    history = client.get_pool_history(netuid, limit=limit)

    prices = []
    timestamps = []
    for entry in history:
        p = _safe_float(entry.get("price"))
        t = entry.get("timestamp", "")
        if p > 0:
            prices.append(p)
            timestamps.append(t)

    # Ensure chronological order
    if timestamps and len(timestamps) > 1 and timestamps[0] > timestamps[-1]:
        prices.reverse()
        timestamps.reverse()

    return prices, timestamps


# ─────────────────────────────────────────────────────────────────────────────
# Momentum + sentiment overlay (LS31)
# ─────────────────────────────────────────────────────────────────────────────

def _opt_float(val) -> Optional[float]:
    """Parse a value to float, returning None when the field is genuinely
    absent/null/unparseable — but KEEPING a real 0.0.

    Unlike _safe_float (which defaults missing → 0.0), this preserves the
    omit-vs-zero distinction the dashboard relies on: a missing horizon must
    surface as "—" (honest "no data"), not a fabricated 0.0% that would trip
    the ALL_ZERO / FLAT gates. A literal "0.00" from the API is a real datum
    and is kept.
    """
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def fetch_pool_overlay(client: TaostatsClient) -> dict:
    """ONE bulk pool/latest call → instant per-subnet momentum + network F&G.

    Returns:
        {
            "deltas": {netuid: {"1h": pct, "24h": pct, "7d": pct}},  # only real horizons
            "fear_and_greed": {"index": float, "sentiment": str} | None,
        }

    Design (mirrors snapshot_history.py / chain_fetch.py philosophy):
      - Missing horizons are OMITTED, never zeroed. A real "0.00" IS kept
        (see _opt_float). The caller attaches only present keys so the
        dashboard shows "—" for unknowns, not a fake flat 0.0%.
      - Credit exhaustion propagates as TaostatsCreditsExhausted from
        client.get()'s existing guard — the caller catches it and falls back
        to the snapshot store (store-only mode), never blanks the dashboard.
      - pool/latest still returns price_change_1_hour / _1_day / _1_week and
        fear_and_greed_index; it only dropped the seven_day_prices array. This
        function reads exactly those surviving fields — no extra API cost.

    Horizon mapping (API field → dashboard key):
        price_change_1_hour  → "1h"
        price_change_1_day   → "24h"
        price_change_1_week  → "7d"
    (30d is NOT a taostats field — it comes from the snapshot store. The caller
    merges: taostats wins 1h/24h/7d, store supplies 30d.)

    Cost: exactly ONE call per invocation (~120/month at the 6h cron cadence),
    well under the 5/min free limit. This is FEWER taostats calls than the old
    dashboard, which made this same call on every browser page load.
    """
    pools = client.get_all_pools()  # may raise TaostatsCreditsExhausted — let it
    logger.info(f"Overlay: parsing momentum + F&G from {len(pools)} pools")

    deltas: dict[int, dict[str, float]] = {}
    fng: Optional[dict] = None

    _field_map = (
        ("1h", "price_change_1_hour"),
        ("24h", "price_change_1_day"),
        ("7d", "price_change_1_week"),
    )

    for pool in pools:
        try:
            netuid = int(pool.get("netuid"))
        except (TypeError, ValueError):
            continue

        d: dict[str, float] = {}
        for key, api_field in _field_map:
            v = _opt_float(pool.get(api_field))
            if v is not None:          # omit unknowns, keep a real 0.0
                d[key] = v
        if d:
            deltas[netuid] = d

        # Fear & Greed is network-wide; capture the first pool that carries it.
        if fng is None:
            idx = _opt_float(pool.get("fear_and_greed_index"))
            if idx is not None:
                fng = {
                    "index": idx,
                    "sentiment": pool.get("fear_and_greed_sentiment", ""),
                }

    logger.info(
        f"Overlay: {len(deltas)} subnets with momentum; "
        f"F&G={'present' if fng else 'absent'}"
    )
    return {"deltas": deltas, "fear_and_greed": fng}


# ─────────────────────────────────────────────────────────────────────────────
# Quick test / CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description="Test Taostats API fetch")
    parser.add_argument("--api-key", required=True, help="Taostats API key")
    parser.add_argument("--netuid", type=int, help="Fetch single subnet (default: all)")
    parser.add_argument("--concentration", action="store_true",
                        help="Also fetch metagraph for Gini calculation")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--overlay", action="store_true",
                        help="Test the momentum + Fear&Greed overlay (1 bulk call)")
    args = parser.parse_args()

    client = TaostatsClient(api_key=args.api_key)

    if args.overlay:
        print("\nFetching pool overlay (momentum + Fear & Greed)...")
        ov = fetch_pool_overlay(client)
        if args.json:
            print(json.dumps(ov, indent=2))
        else:
            deltas = ov["deltas"]
            fng = ov["fear_and_greed"]
            print(f"  Momentum on {len(deltas)} subnets")
            for nid in sorted(deltas)[:10]:
                d = deltas[nid]
                cols = "  ".join(f"{k}={d[k]:+.2f}%" for k in ("1h", "24h", "7d") if k in d)
                print(f"    SN{nid:>3d}  {cols}")
            if len(deltas) > 10:
                print(f"    ... and {len(deltas) - 10} more")
            if fng:
                print(f"  Fear & Greed: {fng['index']:.0f} / {fng['sentiment']}")
            else:
                print("  Fear & Greed: — (not returned)")
    elif args.netuid:
        print(f"\nFetching SN{args.netuid}...")
        pool = client.get_pool(args.netuid)
        if pool:
            if args.json:
                print(json.dumps(pool, indent=2))
            else:
                m = pool_to_metrics(pool)
                print(f"  Name: {m.name}")
                print(f"  Price: {m.token_price:.6f} TAO")
                print(f"  Pool depth: {m.pool_depth:.2f} TAO")
                print(f"  Price history: {len(m.price_history)} bars")
                print(f"  Volume 24h: {m.volume_24h:.2f} TAO")

                if args.concentration:
                    print(f"\n  Fetching metagraph for concentration...")
                    metagraph = client.get_metagraph(args.netuid)
                    gini = concentration_from_metagraph(metagraph)
                    print(f"  Gini coefficient: {gini:.4f}")
                    print(f"  {'⚠️ ABOVE 0.85 THRESHOLD' if gini >= 0.85 else '✓ Below threshold'}")
        else:
            print(f"  No data returned for SN{args.netuid}")
    else:
        print("\nFetching all subnet pools...")
        pools = client.get_all_pools()
        print(f"Got {len(pools)} subnets\n")

        for pool in pools[:10]:  # Print first 10
            m = pool_to_metrics(pool)
            print(f"  SN{m.subnet_id:>3d} ({m.name:>20s}) | "
                  f"Price: {m.token_price:.6f} TAO | "
                  f"Pool: {m.pool_depth:>10.2f} TAO | "
                  f"History: {len(m.price_history)} bars")

        if len(pools) > 10:
            print(f"  ... and {len(pools) - 10} more")

        print(f"\nTo run with scoring: pipe into subnet_scoring_engine.py")
