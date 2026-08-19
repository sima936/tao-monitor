"""Tests for the dust filter in parse_stake_balances.

The real function lives in run_scoring.py which pulls the bittensor/taostats
stack on import. We can't import that in isolation, so this file reproduces
the function body verbatim and tests the reproduced copy. Any drift between
this test copy and the real function shows up as an obvious diff during code
review — kept small on purpose.

Run: python3 test_stake_balances.py
"""
from __future__ import annotations

import logging
import sys

# Silence the logger — the real function calls logger.info(...) on drops,
# and we don't want that noise in test output.
logger = logging.getLogger("test_stake_balances")
logger.addHandler(logging.NullHandler())


# ─── Verbatim copy of the real function (keep in sync with run_scoring.py) ──
DEFAULT_MIN = 0.01


def parse_stake_balances(
    stakes: list[dict],
    min_balance: float | None = None,
) -> dict[int, float]:
    if min_balance is None:
        min_balance = DEFAULT_MIN
    out: dict[int, float] = {}
    for entry in stakes or []:
        nid = entry.get("netuid", entry.get("subnet_id"))
        if nid is None:
            continue
        try:
            bal = float(entry.get("balance_as_tao"))
        except (TypeError, ValueError):
            continue
        out[int(nid)] = out.get(int(nid), 0.0) + bal / 1e9
    if min_balance > 0:
        dropped = {k: round(v, 6) for k, v in out.items() if v < min_balance}
        out = {k: v for k, v in out.items() if v >= min_balance}
        if dropped:
            logger.info(f"dropped dust: {dropped}")
    return out


# ─── Helper: build a taostats-shaped stake entry ────────────────────────────
def _stake(netuid, tao):
    """Build one taostats-shaped entry. TAO input converts to rao internally."""
    return {"netuid": netuid, "balance_as_tao": str(int(tao * 1e9))}


# ─── Tests ──────────────────────────────────────────────────────────────────
def test_empty_input_returns_empty():
    assert parse_stake_balances([]) == {}
    assert parse_stake_balances(None) == {}


def test_single_position_above_threshold_passes():
    stakes = [_stake(4, 3.38)]
    out = parse_stake_balances(stakes)
    assert out == {4: 3.38}


def test_sn21_ghost_dust_gets_dropped():
    # The exact case that triggered this fix — a tiny AdTAO residue reads
    # as a "held position" from taostats. Default threshold should drop it.
    stakes = [
        _stake(4, 3.38),         # Targon — real
        _stake(90, 0.92),        # KubeTEE — real
        _stake(21, 0.000042),    # AdTAO — dust residue, should drop
    ]
    out = parse_stake_balances(stakes)
    assert 21 not in out, f"dust should be dropped, but got: {out}"
    assert set(out.keys()) == {4, 90}


def test_exact_threshold_passes():
    # >= 0.01τ passes. Boundary check.
    stakes = [_stake(4, 0.01)]
    out = parse_stake_balances(stakes)
    assert 4 in out
    assert abs(out[4] - 0.01) < 1e-9


def test_just_below_threshold_drops():
    stakes = [_stake(4, 0.0099)]
    out = parse_stake_balances(stakes)
    assert out == {}


def test_multi_hotkey_aggregation_before_filter():
    # Two entries on the same netuid, each below threshold, but SUMMING above.
    # Must NOT be dropped — filter runs AFTER aggregation.
    stakes = [
        _stake(4, 0.006),
        _stake(4, 0.006),
    ]
    out = parse_stake_balances(stakes)
    assert 4 in out, "aggregation should sum before filter fires"
    assert abs(out[4] - 0.012) < 1e-9


def test_multi_hotkey_still_dust_gets_dropped():
    # Two entries on same netuid, both micro, summing STILL below threshold.
    stakes = [
        _stake(21, 0.001),
        _stake(21, 0.002),
    ]
    out = parse_stake_balances(stakes)
    assert out == {}


def test_min_balance_zero_disables_filter():
    # Env override MIN_HOLDING_BALANCE_TAO=0 should restore pre-fix behaviour
    # (any nonzero stake counts).
    stakes = [
        _stake(4, 3.38),
        _stake(21, 0.000042),
    ]
    out = parse_stake_balances(stakes, min_balance=0)
    assert 21 in out, "min_balance=0 must not filter — got: {out}"


def test_min_balance_custom_threshold():
    # Higher custom threshold — passes only bigger positions.
    stakes = [
        _stake(4, 3.38),
        _stake(90, 0.5),
        _stake(75, 0.05),
    ]
    out = parse_stake_balances(stakes, min_balance=1.0)
    assert out == {4: 3.38}


def test_missing_netuid_field_skipped():
    stakes = [{"balance_as_tao": "1000000000"}]  # 1τ but no netuid
    out = parse_stake_balances(stakes)
    assert out == {}


def test_subnet_id_alias_accepted():
    # Function accepts "subnet_id" as alias for "netuid" — real taostats
    # payload uses this in some endpoints.
    stakes = [{"subnet_id": 4, "balance_as_tao": str(int(3.38 * 1e9))}]
    out = parse_stake_balances(stakes)
    assert out == {4: 3.38}


def test_bad_balance_string_skipped_not_fatal():
    stakes = [
        {"netuid": 4, "balance_as_tao": "not-a-number"},
        _stake(90, 0.92),
    ]
    out = parse_stake_balances(stakes)
    assert 4 not in out
    assert 90 in out


# ─── Runner ─────────────────────────────────────────────────────────────────
def _run_all():
    tests = [
        test_empty_input_returns_empty,
        test_single_position_above_threshold_passes,
        test_sn21_ghost_dust_gets_dropped,
        test_exact_threshold_passes,
        test_just_below_threshold_drops,
        test_multi_hotkey_aggregation_before_filter,
        test_multi_hotkey_still_dust_gets_dropped,
        test_min_balance_zero_disables_filter,
        test_min_balance_custom_threshold,
        test_missing_netuid_field_skipped,
        test_subnet_id_alias_accepted,
        test_bad_balance_string_skipped_not_fatal,
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
