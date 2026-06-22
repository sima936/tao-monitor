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

    try:
        with bt.Subtensor(network=network, fallback_endpoints=FALLBACK_ENDPOINTS) as sub:
            stake_infos = sub.get_stake_info_for_coldkey(coldkey)
            prices = sub.get_subnet_prices()
            balances = stakes_to_tao_dict(stake_infos, prices)
        logger.info(
            f"chain_fetch: read {len(balances)} positions for "
            f"{coldkey[:6]}…{coldkey[-4:]} via chain RPC (free, read-only)"
        )
        _diag(f"OK — {len(balances)} positions via chain RPC (free): "
              f"{sorted(balances)} | total {sum(balances.values()):.3f}\u03c4")
        return balances
    except Exception as e:
        logger.warning(f"chain_fetch: chain read failed ({e}) — caller falls back to taostats")
        _diag(f"CHAIN READ FAILED ({type(e).__name__}: {e}) -> falling back to taostats")
        return None


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
