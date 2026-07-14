"""chain_fetch.py — free, read-only wallet positions via the Subtensor SDK.

PRIMARY source for holdings resolution: reads the coldkey's per-subnet stake
straight off the Bittensor chain (public, read-only, no API key, no taostats
credits).

Returns the SAME shape as run_scoring.parse_stake_balances():
    {netuid: balance_in_TAO}
multi-hotkey summed and spot-valued (alpha * price) so it matches taostats'
`balance_as_tao` and the existing cost-basis / P&L stays consistent.

Safety:
  - Read-only. Uses ONLY the public coldkey ss58 address. Never loads a wallet,
    key, mnemonic, or signer. Cannot move funds.
  - Fails closed: returns None on ANY problem (SDK missing, chain unreachable,
    decode error). Never raises to the caller, never returns a partial or
    guessed number — so the caller drops cleanly to the taostats fallback and
    we are never worse off than before this module existed.

Return contract:
    None  -> chain unavailable; caller should fall back to taostats.
    {}    -> chain reachable, coldkey genuinely holds nothing.
    {..}  -> {netuid: tao_value}.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("chain_fetch")

import sys as _sys
def _diag(msg: str) -> None:
    """Print to stderr so bittensor's logging takeover can't mute it."""
    print(f"[chain_fetch] {msg}", file=_sys.stderr, flush=True)


def _safe_close(sub) -> None:
    """Close the Subtensor connection, swallowing cleanup-only errors.

    async-substrate-interface 2.2.0's SubstrateInterface.close() calls
    .cache_clear() on instance methods, one of which is a functools.partial
    in this build -> AttributeError. That happens AFTER the data is read, so
    a close-time error must never discard a successful result.
    """
    if sub is None:
        return
    try:
        sub.close()
    except Exception as e:  # cleanup only — read already completed
        _diag(f"close() cleanup error ignored ({type(e).__name__}: {e})")

# Simon's coldkey (public address — same default as taostats_fetch.DEFAULT_COLDKEY).
DEFAULT_COLDKEY = "5HR3cMSEnyzQbGCqgeHHQxCosgCBDi6a2tkWiBE3XCwUsmNR"
DEFAULT_NETWORK = "finney"
# Official mainnet endpoint first; SDK retries these on transient drops.
FALLBACK_ENDPOINTS = ["wss://entrypoint-finney.opentensor.ai:443"]

# Reason flag for the most recent chain read, so a caller that only sees the
# None/{}/{..} return value can still branch on WHY a None came back — without
# changing that return contract. Set by every chain-read fn; "ok" on success.
# Single-threaded cron, so plain module state is fine.
#   "ok"               -> last read succeeded
#   "storage_mismatch" -> SDK queried a storage key the runtime dropped
#                         (finney runtime upgrade ahead of the pinned SDK) — bump the pin
#   "unreachable"      -> websocket/connection/timeout — transient chain blip
#   "sdk_missing"      -> bittensor not importable
#   "other"            -> anything else (decode/unexpected)
LAST_FAILURE: str = "ok"

# Per-netuid fingerprint from the last successful all_subnets() read.
# {netuid: {"reg_block": int|None, "owner_coldkey": str|None}}.
# Populated as a side effect of fetch_all_subnet_metrics_via_chain so callers
# can diff fingerprints across runs (detects re-registration into an existing
# slot — the case where set(netuid) is unchanged but the subnet's identity has
# been replaced). network_registered_at is the definitive signal: monotonic
# per-subnet, only changes on re-registration. owner_coldkey is stored as a
# secondary confirmation. Single-threaded cron, plain module state is fine.
LAST_FINGERPRINTS: dict[int, dict] = {}


def get_last_fingerprints() -> dict[int, dict]:
    """Return the fingerprint map from the last chain metrics fetch.

    Empty {} if no successful fetch has run yet. Values are always fresh
    dicts, safe to mutate by the caller.
    """
    return {int(k): dict(v) for k, v in LAST_FINGERPRINTS.items()}


def classify_chain_error(exc: BaseException) -> str:
    """Map a chain-read exception to a coarse reason flag (see LAST_FAILURE)."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if (
        "storagefunctionnotfound" in text
        or "storage function" in text
        or ("metadata" in text and ("decode" in text or "not found" in text))
    ):
        return "storage_mismatch"
    if any(
        k in text
        for k in (
            "connection", "websocket", "timeout", "timed out", "unreachable",
            "refused", "reset", "eof", "broken pipe", "ssl", "handshake",
        )
    ):
        return "unreachable"
    return "other"


def _as_float(x) -> float:
    """Balance objects expose .tao; plain numbers pass through."""
    return float(getattr(x, "tao", x))


def stakes_to_tao_dict(stake_infos, prices) -> dict[int, float]:
    """PURE conversion (unit-testable offline, no chain):

        list[StakeInfo] + {netuid: price}  ->  {netuid: tao_value}

    spot-valued (alpha * price) and multi-hotkey summed. netuid 0 (root) is TAO
    already, so its price is 1.0. A non-zero subnet with no price is skipped
    (can't be valued) rather than guessed.
    """
    out: dict[int, float] = {}
    for si in stake_infos or []:
        nid = int(si.netuid)
        alpha = _as_float(si.stake)
        if nid == 0:
            price = 1.0
        else:
            p = prices.get(nid) if hasattr(prices, "get") else None
            if p is None:
                logger.warning(f"chain_fetch: no price for SN{nid}; skipping (cannot value)")
                continue
            price = _as_float(p)
        out[nid] = out.get(nid, 0.0) + alpha * price
    return out


def _patch_asi_close_bug() -> None:
    """Neutralise an async-substrate-interface 2.2.0 bug.

    Its SubstrateInterface.close() calls ``self.<lru_cached_method>.cache_clear()``.
    On Python 3.13+, accessing an lru_cache-decorated *instance* method via ``self``
    returns a functools.partial, which has no ``cache_clear`` -> AttributeError.
    close() is reached via an internal reconnect/init path during a read, so this
    aborts the whole read. We replace close() with one that closes the websocket
    and clears caches via the CLASS-level wrapper (which always has cache_clear),
    each call guarded. Idempotent; version- and Python-version-agnostic.
    """
    try:
        from async_substrate_interface.sync_substrate import SubstrateInterface
    except Exception as e:
        _diag(f"asi close-patch skipped (import failed: {e})")
        return
    if getattr(SubstrateInterface, "_tao_safe_close", False):
        return
    _cached = (
        "get_runtime_for_version", "get_parent_block_hash", "get_block_runtime_info",
        "get_block_runtime_version_for", "supports_rpc_method", "_get_block_hash",
        "_cached_get_block_number",
    )

    def _safe_close_method(self):
        try:
            self.ws.close()
        except Exception:
            pass
        for _name in _cached:
            try:
                _attr = getattr(type(self), _name, None)  # class wrapper has cache_clear
                _cc = getattr(_attr, "cache_clear", None)
                if callable(_cc):
                    _cc()
            except Exception:
                pass

    SubstrateInterface.close = _safe_close_method
    SubstrateInterface._tao_safe_close = True
    _diag("asi close-patch installed (guards cache_clear AttributeError)")


def get_wallet_stakes_via_chain(
    coldkey: str = DEFAULT_COLDKEY,
    network: str = DEFAULT_NETWORK,
) -> Optional[dict[int, float]]:
    """Read per-subnet stake for `coldkey` off-chain. See module docstring for
    the None / {} / {..} contract."""
    global LAST_FAILURE
    try:
        import bittensor as bt
    except Exception as e:  # SDK not installed (e.g. dep not yet added)
        logger.info(f"chain_fetch: bittensor SDK unavailable ({e}) — caller falls back")
        _diag(f"SDK UNAVAILABLE ({e}) -> falling back to taostats")
        LAST_FAILURE = "sdk_missing"
        return None

    _patch_asi_close_bug()  # must run before any Subtensor/SubstrateInterface use

    # Read OUTSIDE a context manager so a close-time cleanup bug can't discard
    # the result. We close explicitly via _safe_close after the data is in hand.
    sub = None
    try:
        sub = bt.Subtensor(network=network, fallback_endpoints=FALLBACK_ENDPOINTS)
        stake_infos = sub.get_stake_info_for_coldkey(coldkey)
        prices = sub.get_subnet_prices()
        balances = stakes_to_tao_dict(stake_infos, prices)
    except Exception as e:
        LAST_FAILURE = classify_chain_error(e)
        logger.warning(f"chain_fetch: chain read failed ({e}) — caller falls back to taostats")
        _diag(f"CHAIN READ FAILED ({type(e).__name__}: {e}) [{LAST_FAILURE}] -> falling back to taostats")
        import traceback as _tb; _diag("TRACE: " + " | ".join(_tb.format_exc().strip().splitlines()[-3:]))
        _safe_close(sub)
        return None
    # Read succeeded — clean up defensively (cleanup errors must not fail it).
    LAST_FAILURE = "ok"
    _safe_close(sub)
    logger.info(
        f"chain_fetch: read {len(balances)} positions for "
        f"{coldkey[:6]}…{coldkey[-4:]} via chain RPC (free, read-only)"
    )
    _diag(f"OK — {len(balances)} positions via chain RPC (free): "
          f"{sorted(balances)} | total {sum(balances.values()):.3f}\u03c4")
    return balances



def get_free_balance_via_chain(
    coldkey: str = DEFAULT_COLDKEY,
    network: str = DEFAULT_NETWORK,
):
    """Free (unstaked) TAO balance for the coldkey, read off-chain. Returns a
    float, or None on any failure so the caller falls back to taostats."""
    try:
        import bittensor as bt
    except Exception as e:
        _diag(f"free-balance: SDK unavailable ({e}) -> falling back")
        return None
    _patch_asi_close_bug()
    sub = None
    try:
        sub = bt.Subtensor(network=network, fallback_endpoints=FALLBACK_ENDPOINTS)
        free = _as_float(sub.get_balance(coldkey))
    except Exception as e:
        _diag(f"free-balance: CHAIN READ FAILED ({type(e).__name__}: {e}) -> falling back")
        _safe_close(sub)
        return None
    _safe_close(sub)
    _diag(f"free-balance OK — {free:.3f}\u03c4 via chain RPC (free)")
    return free


def _dynamicinfos_to_metrics(subnets):
    """list[DynamicInfo] -> list[SubnetMetrics] (the engine's type).

    Maps chain fields to the same shape taostats fetch_all_subnet_metrics returns:
      price -> token_price, tao_in -> pool_depth, subnet_volume -> volume_24h,
      genie_score = 0.5 (concentration is off), plus a REAL daily price history
      pulled from `subnet_price_history` (v2 layer) when available.

    History source, in order:
      1) subnet_price_history store — persistent SQLite of daily GT closes.
         This is the v2 real-history layer; populated by top_up_stale() /
         backfill_full() run from run_scoring.py earlier in the cycle. Gives
         every subnet honest 24h/7d pct-changes and a real p9_data_maturity,
         which drives Opportunities/Scanner/scoring off actual movement.
      2) Flat synthetic fallback — kept for subnets the store doesn't cover
         (brand-new pools, GT-less subnets, or the first ever cron before
         backfill completes). Deliberately flat: deriving a per-subnet trend
         from a single snapshot once invented bearish regimes in a pullback
         and flipped held names to EXIT — a "sell everything" digest. The
         invariant "never fabricate trend from a snapshot" stands.

    Downstream never sees the fallback vs real distinction — the shape is
    identical. The scoring engine's p9_data_maturity is what surfaces the
    difference (higher score for longer real history, ~50 constant for the
    9-bar synthetic).
    """
    from taostats_fetch import SubnetMetrics, _synthetic_history
    import datetime as _dt

    # Import the store lazily so this module remains usable in environments
    # where subnet_price_history is absent (e.g. legacy container image before
    # the v2 rollout). Missing store => everyone gets the flat synthetic,
    # exactly matching pre-v2 behaviour.
    try:
        from subnet_price_history import get_bars as _get_store_bars
    except Exception:
        _get_store_bars = None  # type: ignore[assignment]

    now = _dt.datetime.now(_dt.timezone.utc)
    real_count = 0
    synth_count = 0
    out = []
    for di in subnets or []:
        nid = int(di.netuid)
        price = _as_float(getattr(di, "price", 0.0))
        depth = _as_float(getattr(di, "tao_in", 0.0))
        vol = _as_float(getattr(di, "subnet_volume", 0.0))
        name = getattr(di, "subnet_name", None)
        if isinstance(name, (bytes, bytearray)):
            name = bytes(name).decode("utf-8", "ignore")
        name = (str(name).strip() if name else "") or f"SN{nid}"
        # moving_price: on-chain EMA the dereg algorithm ranks against.
        # None on taostats path (not exposed there); populated here so the
        # dereg watchlist detector can rank without a second chain call.
        mp = getattr(di, "moving_price", None)
        try:
            mp = _as_float(mp) if mp is not None else None
        except Exception:
            mp = None

        # 1) Try the real-history store first.
        hist: list[float] = []
        ts: list[str] = []
        if _get_store_bars is not None:
            try:
                _closes, _stamps = _get_store_bars(nid, limit=120)
                # A single stored bar isn't a series — the scoring engine's
                # p9 / Markov / EMA all need meaningful depth. Below 7 bars we
                # treat the store as "not ready" for this subnet and fall
                # through to the flat synthetic below (same policy as GT's
                # min_bars in fetch_history_for_netuids).
                if len(_closes) >= 7:
                    # Append today's LIVE chain price as the freshest bar so
                    # the series ends at the current moment, not yesterday's
                    # close. Only add it if the stored newest bar is at least
                    # 12h old, otherwise treat the live price as an update to
                    # today's still-forming bar (skip append to avoid an
                    # artificial duplicate).
                    try:
                        _last_dt = _dt.datetime.fromisoformat(
                            _stamps[-1].replace("Z", "+00:00")
                        )
                        _age_h = (now - _last_dt).total_seconds() / 3600.0
                    except Exception:
                        _age_h = 0.0
                    if _age_h >= 12.0 and price > 0:
                        _closes = list(_closes) + [price]
                        _stamps = list(_stamps) + [now.isoformat()]
                    hist = _closes
                    ts = _stamps
                    real_count += 1
            except Exception:
                # Store read failed for this subnet — fall through to synthetic.
                pass

        # 2) Flat synthetic fallback (empty/sparse store, or store unavailable).
        if not hist:
            hist = _synthetic_history(price, 0.0, 0.0)
            ts = [(now - _dt.timedelta(days=(len(hist) - 1 - i))).isoformat()
                  for i in range(len(hist))]
            synth_count += 1

        out.append(SubnetMetrics(
            subnet_id=nid, name=name, token_price=price, pool_depth=depth,
            genie_score=0.5, price_history=hist, timestamps=ts,
            volume_24h=vol, volume_7d=0.0,
            moving_price=mp,
        ))
    _diag(
        f"history assembly — {real_count} real (store) · {synth_count} synthetic"
    )
    return out


def fetch_all_subnet_metrics_via_chain(network: str = DEFAULT_NETWORK):
    """Free subnet metrics (price, pool depth, volume) from ONE all_subnets()
    chain call — same SubnetMetrics list shape as taostats fetch_all_subnet_metrics.
    Returns None on any failure (or empty) so the caller falls back to taostats.

    Side effect: populates the module-level LAST_FINGERPRINTS map with
    {netuid: {reg_block, owner_coldkey}} extracted from the same DynamicInfo
    list, so the run_scoring new-subnet detector can spot re-registrations
    (same netuid, new reg_block) without a second chain call.
    """
    global LAST_FINGERPRINTS
    try:
        import bittensor as bt
    except Exception as e:
        _diag(f"metrics: SDK unavailable ({e}) -> falling back")
        return None
    _patch_asi_close_bug()
    sub = None
    try:
        sub = bt.Subtensor(network=network, fallback_endpoints=FALLBACK_ENDPOINTS)
        subnets = sub.all_subnets()
        metrics = _dynamicinfos_to_metrics(subnets)
        # Extract fingerprints from the same DynamicInfo list. Defensive on
        # every attribute — an SDK version without these fields must not
        # break the metrics path.
        fingerprints: dict[int, dict] = {}
        for di in subnets or []:
            try:
                nid = int(di.netuid)
            except Exception:
                continue
            reg_block = None
            try:
                _rb = getattr(di, "network_registered_at", None)
                reg_block = int(_rb) if _rb is not None else None
            except Exception:
                reg_block = None
            owner_ck = None
            try:
                _ck = getattr(di, "owner_coldkey", None)
                owner_ck = str(_ck) if _ck else None
            except Exception:
                owner_ck = None
            fingerprints[nid] = {"reg_block": reg_block, "owner_coldkey": owner_ck}
        LAST_FINGERPRINTS = fingerprints
    except Exception as e:
        _diag(f"metrics: CHAIN READ FAILED ({type(e).__name__}: {e}) -> falling back")
        import traceback as _tb
        _diag("TRACE: " + " | ".join(_tb.format_exc().strip().splitlines()[-3:]))
        _safe_close(sub)
        return None
    _safe_close(sub)
    if not metrics:
        _diag("metrics: chain returned 0 subnets -> falling back")
        return None
    _diag(f"metrics OK — {len(metrics)} subnets via chain RPC (free)")
    return metrics


def get_subnet_burn_cost_via_chain(network: str = DEFAULT_NETWORK) -> float | None:
    """Network-wide subnet-creation lock cost in TAO. One free chain call.

    This is the cost to REGISTER A NEW SUBNET (not a miner slot). It decays
    smoothly between registrations and doubles on each new one. When it's
    dropping through a floor, someone will register — which triggers the
    dereg cascade on the lowest-moving-price incumbent slot.

    Returns None on any failure so the caller degrades cleanly (dereg watchlist
    still works without burn-cost context — it's a nice-to-have field, not a
    gate). Defensive against SDK version drift: the method may be spelled
    get_subnet_burn_cost / burn_cost / lock_cost depending on version.
    """
    try:
        import bittensor as bt
    except Exception as e:
        _diag(f"burn_cost: SDK unavailable ({e})")
        return None
    _patch_asi_close_bug()
    sub = None
    try:
        sub = bt.Subtensor(network=network, fallback_endpoints=FALLBACK_ENDPOINTS)
        val = None
        for method in ("get_subnet_burn_cost", "burn_cost", "get_lock_cost", "lock_cost"):
            fn = getattr(sub, method, None)
            if callable(fn):
                try:
                    val = fn()
                    break
                except Exception:
                    continue
        if val is None:
            _diag("burn_cost: no known SDK method returned a value")
            _safe_close(sub)
            return None
        # Value may be a Balance object (with .tao) or a raw rao int.
        if hasattr(val, "tao"):
            out = float(val.tao)
        elif isinstance(val, (int, float)):
            out = float(val) / 1e9 if val > 1e6 else float(val)  # rao → tao heuristic
        else:
            out = _as_float(val)
    except Exception as e:
        _diag(f"burn_cost: CHAIN READ FAILED ({type(e).__name__}: {e})")
        _safe_close(sub)
        return None
    _safe_close(sub)
    _diag(f"burn_cost OK — {out:.2f}τ")
    return out


if __name__ == "__main__":
    import json, traceback
    _diag("=== SELF-TEST START ===")
    try:
        res = get_wallet_stakes_via_chain()
    except Exception as exc:  # should not happen — fn fails closed
        _diag(f"UNEXPECTED RAISE: {exc}")
        traceback.print_exc()
        raise SystemExit(2)
    if res is None:
        _diag("RESULT: None (chain unavailable / SDK missing) -> taostats fallback would run")
        raise SystemExit(1)
    if not res:
        _diag("RESULT: {} (chain reachable but ZERO positions parsed for this coldkey)")
        raise SystemExit(1)
    _diag(f"RESULT: {len(res)} positions, total {sum(res.values()):.3f}\u03c4 — chain read WORKS")
    print(json.dumps(res, indent=2))
