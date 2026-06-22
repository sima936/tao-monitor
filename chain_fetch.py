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
    try:
        import bittensor as bt
    except Exception as e:  # SDK not installed (e.g. dep not yet added)
        logger.info(f"chain_fetch: bittensor SDK unavailable ({e}) — caller falls back")
        _diag(f"SDK UNAVAILABLE ({e}) -> falling back to taostats")
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
        logger.warning(f"chain_fetch: chain read failed ({e}) — caller falls back to taostats")
        _diag(f"CHAIN READ FAILED ({type(e).__name__}: {e}) -> falling back to taostats")
        import traceback as _tb; _diag("TRACE: " + " | ".join(_tb.format_exc().strip().splitlines()[-3:]))
        _safe_close(sub)
        return None
    # Read succeeded — clean up defensively (cleanup errors must not fail it).
    _safe_close(sub)
    logger.info(
        f"chain_fetch: read {len(balances)} positions for "
        f"{coldkey[:6]}…{coldkey[-4:]} via chain RPC (free, read-only)"
    )
    _diag(f"OK — {len(balances)} positions via chain RPC (free): "
          f"{sorted(balances)} | total {sum(balances.values()):.3f}\u03c4")
    return balances


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
