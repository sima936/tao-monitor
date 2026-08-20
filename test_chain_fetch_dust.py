"""Tests for the dust filter in chain_fetch.stakes_to_tao_dict.

Same pattern as test_stake_balances.py — reproduces the function verbatim
because importing chain_fetch pulls the bittensor SDK. Any drift between
this test copy and the real function shows up as an obvious diff in review.

Run: python3 test_chain_fetch_dust.py
"""
from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

logger = logging.getLogger("test_chain_fetch_dust")
logger.addHandler(logging.NullHandler())

DEFAULT_MIN = 0.01


def _diag(msg):  # no-op in tests
    pass


def _as_float(x):
    """Mirror of the real _as_float — handles Balance objects, floats, ints."""
    if hasattr(x, "tao"):
        return float(x.tao)
    if hasattr(x, "rao"):
        return float(x.rao) / 1e9
    return float(x)


# ─── Verbatim copy of stakes_to_tao_dict (keep in sync with chain_fetch.py) ──
def stakes_to_tao_dict(stake_infos, prices, min_balance=None):
    if min_balance is None:
        min_balance = DEFAULT_MIN
    out = {}
    for si in stake_infos or []:
        nid = int(si.netuid)
        alpha = _as_float(si.stake)
        if nid == 0:
            price = 1.0
        else:
            p = prices.get(nid) if hasattr(prices, "get") else None
            if p is None:
                continue
            price = _as_float(p)
        out[nid] = out.get(nid, 0.0) + alpha * price
    if min_balance > 0:
        dropped = {k: round(v, 6) for k, v in out.items() if v < min_balance}
        out = {k: v for k, v in out.items() if v >= min_balance}
        if dropped:
            logger.info(f"dropped dust: {dropped}")
    return out


# ─── Helpers ────────────────────────────────────────────────────────────────
def stake(netuid, alpha):
    """Build a StakeInfo-like object with .netuid and .stake attributes."""
    return SimpleNamespace(netuid=netuid, stake=float(alpha))


# ─── Tests ──────────────────────────────────────────────────────────────────
def test_empty_input():
    assert stakes_to_tao_dict([], {}) == {}
    assert stakes_to_tao_dict(None, {}) == {}


def test_root_is_priced_at_one():
    # netuid 0 is TAO already — no price lookup needed, 1.0 hardcoded.
    out = stakes_to_tao_dict([stake(0, 19.07)], {})
    assert 0 in out
    assert abs(out[0] - 19.07) < 1e-9


def test_alpha_price_multiplication():
    # SN4 30 alpha × 0.055τ/α = 1.65τ.
    out = stakes_to_tao_dict([stake(4, 30.0)], {4: 0.055})
    assert 4 in out
    assert abs(out[4] - 1.65) < 1e-6


def test_sn21_ghost_dust_dropped():
    # The actual case: SN21 30 alpha × 0.00033τ/α = 0.0099τ — below threshold.
    stakes = [
        stake(0, 19.07),
        stake(4, 60.24),
        stake(90, 30.91),
        stake(21, 30.0),   # <0.01 alpha in reality, but scale for the math
    ]
    prices = {4: 0.0552, 90: 0.0347, 21: 0.00033}
    out = stakes_to_tao_dict(stakes, prices)
    assert 21 not in out, f"SN21 dust should be dropped, got {out}"
    assert set(out.keys()) == {0, 4, 90}


def test_boundary_at_threshold_passes():
    # Exactly 0.01τ passes the >= check.
    out = stakes_to_tao_dict([stake(4, 1.0)], {4: 0.01})
    assert 4 in out


def test_just_below_threshold_drops():
    out = stakes_to_tao_dict([stake(4, 1.0)], {4: 0.0099})
    assert out == {}


def test_multi_hotkey_aggregation_before_filter():
    # Two entries on same netuid, each below threshold, sum ABOVE.
    stakes = [stake(4, 0.5), stake(4, 0.5)]  # 0.5 × 0.02 = 0.01 each, sum 0.02
    out = stakes_to_tao_dict(stakes, {4: 0.02})
    assert 4 in out, "aggregation should happen before filter"
    assert abs(out[4] - 0.02) < 1e-9


def test_multi_hotkey_still_dust_dropped():
    # Two entries on same netuid, both micro, summing STILL below threshold.
    stakes = [stake(21, 1.0), stake(21, 1.0)]  # 1 × 0.003 = 0.003 each, sum 0.006
    out = stakes_to_tao_dict(stakes, {21: 0.003})
    assert out == {}


def test_missing_price_skipped_not_fatal():
    # A non-zero subnet with no price is skipped, not guessed. Other positions
    # still pass through.
    stakes = [stake(4, 10.0), stake(999, 10.0)]
    prices = {4: 0.05}  # no price for 999
    out = stakes_to_tao_dict(stakes, prices)
    assert 4 in out and 999 not in out


def test_min_balance_zero_disables_filter():
    # min_balance=0 restores pre-fix behaviour — any nonzero value passes.
    stakes = [stake(4, 30.0), stake(21, 30.0)]
    prices = {4: 0.05, 21: 0.0001}   # SN21 = 0.003τ, would normally drop
    out = stakes_to_tao_dict(stakes, prices, min_balance=0)
    assert 21 in out


def test_min_balance_custom_threshold():
    # Higher threshold — only bigger positions pass.
    stakes = [stake(0, 19.07), stake(4, 30.0), stake(90, 20.0)]
    prices = {4: 0.05, 90: 0.03}   # 4 → 1.5τ, 90 → 0.6τ
    out = stakes_to_tao_dict(stakes, prices, min_balance=1.0)
    assert 0 in out    # 19.07τ passes
    assert 4 in out    # 1.5τ passes
    assert 90 not in out  # 0.6τ dropped


def test_root_never_treated_as_dust_at_realistic_size():
    # Root stakes at 19τ trivially pass — regression guard on the netuid=0 path.
    out = stakes_to_tao_dict([stake(0, 19.07)], {})
    assert 0 in out
    assert out[0] > 10.0


# ─── Runner ─────────────────────────────────────────────────────────────────
def _run_all():
    tests = [
        test_empty_input,
        test_root_is_priced_at_one,
        test_alpha_price_multiplication,
        test_sn21_ghost_dust_dropped,
        test_boundary_at_threshold_passes,
        test_just_below_threshold_drops,
        test_multi_hotkey_aggregation_before_filter,
        test_multi_hotkey_still_dust_dropped,
        test_missing_price_skipped_not_fatal,
        test_min_balance_zero_disables_filter,
        test_min_balance_custom_threshold,
        test_root_never_treated_as_dust_at_realistic_size,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'PASS' if failed == 0 else f'FAIL — {failed} failed'}")
    return failed


if __name__ == "__main__":
    sys.exit(_run_all())
