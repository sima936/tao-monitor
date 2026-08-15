"""Tests for the fixed-fractional risk cap layer in subnet_allocation.

Run: python -m pytest test_risk_cap.py -v
  or (no pytest): python test_risk_cap.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from types import SimpleNamespace

# Import under test
from subnet_allocation import (
    AllocationPolicy,
    compute_target_allocation,
    worst_rolling_drawdown_pct,
)


# ─── Stub scoring / macro objects ────────────────────────────────────────────
def make_score(sid, name, health, regime="Bull", pool=1000.0, price=0.05, genie=0.3):
    return SimpleNamespace(
        subnet_id=sid,
        name=name,
        health_score=float(health),
        markov_regime=regime,
        pool_depth=float(pool),
        token_price=float(price),
        genie_score_raw=genie,
        take_profit_flags=[],
        entry_flags=[],
    )


def make_macro(signal=+0.1, regime="Bull"):
    return SimpleNamespace(
        signal=signal,
        regime=SimpleNamespace(value=regime),
        available=True,
    )


# ─── worst_rolling_drawdown_pct ──────────────────────────────────────────────
def test_dd_flat_series_is_zero():
    assert worst_rolling_drawdown_pct([1.0, 1.0, 1.0, 1.0]) == 0.0


def test_dd_empty_series_is_zero():
    assert worst_rolling_drawdown_pct([]) == 0.0
    assert worst_rolling_drawdown_pct([1.0]) == 0.0
    assert worst_rolling_drawdown_pct(None) == 0.0


def test_dd_simple_50pct_drop():
    # Peak-to-trough 100 -> 50 inside a 2-bar window is a clean 50%.
    prices = [100.0, 50.0]
    dd = worst_rolling_drawdown_pct(prices, window=2)
    assert abs(dd - 0.5) < 1e-9, f"expected 0.5, got {dd}"


def test_dd_uses_rolling_window():
    # 100 -> 60 spread across 5 bars. window=3 catches part of it, not all.
    prices = [100.0, 95.0, 85.0, 70.0, 60.0]
    dd_full = worst_rolling_drawdown_pct(prices, window=5)
    dd_win3 = worst_rolling_drawdown_pct(prices, window=3)
    assert abs(dd_full - 0.4) < 1e-9, f"full window: expected 0.4, got {dd_full}"
    # window=3 starting at bar 0: 100 -> 85 = 15%
    #                     bar 1: 95 -> 70 = 26.3%
    #                     bar 2: 85 -> 60 = 29.4% <- worst
    assert abs(dd_win3 - (25.0 / 85.0)) < 1e-9, f"window=3: got {dd_win3}"


def test_dd_recovery_then_new_drop():
    # 100 -> 90 (10%) -> 110 (peak) -> 88 (20%) — worst inside window is 20%.
    prices = [100.0, 90.0, 110.0, 88.0]
    dd = worst_rolling_drawdown_pct(prices, window=4)
    assert abs(dd - 0.2) < 1e-9, f"expected 0.2, got {dd}"


def test_dd_ignores_bad_prices():
    prices = [0.0, None, 100.0, 60.0, 0.0]
    dd = worst_rolling_drawdown_pct(prices, window=3)
    assert abs(dd - 0.4) < 1e-9, f"expected 0.4 (100->60), got {dd}"


# ─── Risk cap integration in compute_target_allocation ──────────────────────
def _run(scored, macro, holdings=None, worst_dd=None, policy=None, account=100.0):
    """Small helper — thin book of survivors, holdings as fraction of account."""
    current = None
    if holdings:
        current = {sid: w for sid, w in holdings.items()}
    return compute_target_allocation(
        scored,
        macro,
        policy=policy,
        account_tao=account,
        current_weight_by_id=current,
        worst_dd_by_id=worst_dd,
    )


def test_shadow_mode_does_not_apply_but_notes():
    # Two A-tier survivors, tier weights 2:2 → each targets 50% × deploy of book.
    scored = [make_score(4, "Targon", 60), make_score(90, "KubeTEE", 60)]
    macro = make_macro(signal=+0.5, regime="Bull")  # full deploy
    worst_dd = {4: 0.20, 90: 0.50}  # SN90 is volatile
    policy = AllocationPolicy(
        risk_cap_enabled=False,
        risk_budget_per_position=0.01,
        max_weight_per_name=1.0,        # disable per-name so risk is the only cap
        aplus_max_weight=1.0,
        per_name_full_deploy=1.0,
        pool_fraction_cap=1.0,
    )
    plan = _run(scored, macro, holdings={4: 0.20, 90: 0.20}, worst_dd=worst_dd, policy=policy)

    # Weights should be UNTOUCHED in shadow mode.
    weights = {p.subnet_id: p.target_weight for p in plan.positions}
    # Both survivors → tier weight equal → each 50% of deploy (~50% since Bull, sig 0.5 clamped hi=0 → 100%).
    assert abs(weights[4] - 0.5) < 1e-4, f"SN4 weight in shadow mode: {weights[4]}"
    assert abs(weights[90] - 0.5) < 1e-4, f"SN90 weight in shadow mode: {weights[90]}"

    # No position should be capped_by="risk" in shadow mode.
    caps = {p.subnet_id: p.capped_by for p in plan.positions}
    assert caps.get(4) != "risk"
    assert caps.get(90) != "risk"

    # A SHADOW note should be present naming both would-cap subnets.
    shadow_notes = [n for n in plan.notes if "Risk cap [SHADOW]" in n]
    assert len(shadow_notes) == 1, f"expected 1 shadow note, got {plan.notes}"
    assert "SN4" in shadow_notes[0] and "SN90" in shadow_notes[0]


def test_live_mode_caps_and_flags():
    # DDs chosen to CLEARLY exceed the fallback (0.30) so the realized-DD branch
    # governs. SN4=40% DD → cap 2.5%; SN90=50% DD → cap 2%.
    scored = [make_score(4, "Targon", 60), make_score(90, "KubeTEE", 60)]
    macro = make_macro(signal=+0.5, regime="Bull")
    worst_dd = {4: 0.40, 90: 0.50}
    policy = AllocationPolicy(
        risk_cap_enabled=True,
        risk_budget_per_position=0.01,
        risk_cap_fallback_worst_loss=0.30,
        max_weight_per_name=1.0,
        aplus_max_weight=1.0,
        per_name_full_deploy=1.0,
        pool_fraction_cap=1.0,
    )
    plan = _run(scored, macro, holdings={4: 0.20, 90: 0.20}, worst_dd=worst_dd, policy=policy)

    weights = {p.subnet_id: p.target_weight for p in plan.positions}
    caps = {p.subnet_id: p.capped_by for p in plan.positions}
    # SN4: cap = 0.01 / 0.40 = 0.025 (2.5% of book)
    assert abs(weights[4] - 0.025) < 1e-9, f"SN4 should cap to 2.5%, got {weights[4]}"
    # SN90: cap = 0.01 / 0.50 = 0.02 (2% of book)
    assert abs(weights[90] - 0.02) < 1e-9, f"SN90 should cap to 2%, got {weights[90]}"
    assert caps[4] == "risk"
    assert caps[90] == "risk"

    live_notes = [n for n in plan.notes if "Risk cap [LIVE]" in n]
    assert len(live_notes) == 1, f"expected 1 live note, got {plan.notes}"
    # SN4 has real history → "hist" source; SN90 too.
    assert "hist" in live_notes[0]


def test_fallback_is_a_floor_not_just_fallback():
    # Explicit assertion of the semantic: subnet with low realized DD still uses
    # the fallback as a floor (because the STOP itself defines a worst-case loss
    # that can't be undercut by "the subnet has been calm lately").
    scored = [make_score(4, "Targon", 60)]
    macro = make_macro(signal=+0.5, regime="Bull")
    policy = AllocationPolicy(
        risk_cap_enabled=True,
        risk_budget_per_position=0.01,
        risk_cap_fallback_worst_loss=0.30,
        max_weight_per_name=1.0, aplus_max_weight=1.0,
        per_name_full_deploy=1.0, pool_fraction_cap=1.0,
    )
    # Realized DD 10% — MUCH lower than the 30% fallback. Fallback should win.
    plan = _run(scored, macro, holdings={4: 0.50}, worst_dd={4: 0.10}, policy=policy)
    weights = {p.subnet_id: p.target_weight for p in plan.positions}
    assert abs(weights[4] - (0.01 / 0.30)) < 1e-4, (
        f"fallback (0.30) should floor the worst-case even when realized DD is lower; "
        f"got {weights[4]}"
    )
    # Source should be labelled "hist" since we DID have history (just low DD),
    # but worst-case number in the note reflects fallback (0.30).
    live = [n for n in plan.notes if "Risk cap [LIVE]" in n][0]
    assert "worst 30%" in live, f"note should show floored worst-case: {live}"


def test_fallback_used_when_history_missing():
    # No worst_dd supplied → fallback (0.30 default) governs the cap.
    scored = [make_score(4, "Targon", 60)]
    macro = make_macro(signal=+0.5, regime="Bull")
    policy = AllocationPolicy(
        risk_cap_enabled=True,
        risk_budget_per_position=0.01,
        risk_cap_fallback_worst_loss=0.30,
        max_weight_per_name=1.0,
        aplus_max_weight=1.0,
        per_name_full_deploy=1.0,
        pool_fraction_cap=1.0,
    )
    plan = _run(scored, macro, holdings={4: 0.50}, worst_dd=None, policy=policy)

    weights = {p.subnet_id: p.target_weight for p in plan.positions}
    # 0.01 / 0.30 = 0.0333...
    assert abs(weights[4] - (0.01 / 0.30)) < 1e-4, f"got {weights[4]}"
    notes = [n for n in plan.notes if "Risk cap [LIVE]" in n]
    assert notes and "fallback" in notes[0]


def test_no_cap_when_generous_risk_budget():
    # With a generous 10% risk budget, a low-DD subnet at moderate target
    # weight is NOT capped: 0.10/0.30 = 33% cap ceiling, target 15% is below.
    scored = [make_score(4, "Targon", 60), make_score(90, "KubeTEE", 60)]
    macro = make_macro(signal=-0.25, regime="Bull")  # deploy floor
    worst_dd = {4: 0.10, 90: 0.10}
    policy = AllocationPolicy(
        risk_cap_enabled=True,
        risk_budget_per_position=0.10,   # very loose
        risk_cap_fallback_worst_loss=0.30,
        max_weight_per_name=1.0, aplus_max_weight=1.0,
        per_name_full_deploy=1.0, pool_fraction_cap=1.0,
        deploy_floor=0.30,
    )
    plan = _run(scored, macro, holdings={4: 0.10, 90: 0.10}, worst_dd=worst_dd, policy=policy)
    caps = {p.subnet_id: p.capped_by for p in plan.positions}
    # ceiling = 0.10 / 0.30 = 0.333; targets are ~0.15 each; no risk cap should fire.
    assert caps.get(4) != "risk", f"SN4 unexpectedly risk-capped: {caps}"
    assert caps.get(90) != "risk", f"SN90 unexpectedly risk-capped: {caps}"
    # And no LIVE note should mention this.
    assert not [n for n in plan.notes if "Risk cap [LIVE]" in n], plan.notes


def test_zero_worst_case_is_safe():
    # If fallback is 0 (nonsense config) the cap should not divide by zero.
    scored = [make_score(4, "Targon", 60)]
    macro = make_macro(signal=+0.5, regime="Bull")
    policy = AllocationPolicy(
        risk_cap_enabled=True,
        risk_budget_per_position=0.01,
        risk_cap_fallback_worst_loss=0.0,   # pathological
        max_weight_per_name=1.0,
        aplus_max_weight=1.0,
        per_name_full_deploy=1.0,
        pool_fraction_cap=1.0,
    )
    plan = _run(scored, macro, holdings={4: 0.50}, worst_dd={4: 0.0}, policy=policy)
    weights = {p.subnet_id: p.target_weight for p in plan.positions}
    assert abs(weights[4] - 1.0) < 1e-4, f"pathological config should skip cap, got {weights[4]}"


def test_backwards_compat_when_kwarg_omitted():
    # Callers that don't pass worst_dd_by_id must still work.
    scored = [make_score(4, "Targon", 60)]
    macro = make_macro(signal=+0.5, regime="Bull")
    policy = AllocationPolicy(risk_cap_enabled=False)
    plan = compute_target_allocation(
        scored, macro, policy=policy,
        account_tao=100.0,
        current_weight_by_id={4: 0.10},
    )
    assert plan.positions, "should produce a position"


# ─── Manual runner (no pytest dep) ───────────────────────────────────────────
def _run_all():
    tests = [
        test_dd_flat_series_is_zero,
        test_dd_empty_series_is_zero,
        test_dd_simple_50pct_drop,
        test_dd_uses_rolling_window,
        test_dd_recovery_then_new_drop,
        test_dd_ignores_bad_prices,
        test_shadow_mode_does_not_apply_but_notes,
        test_live_mode_caps_and_flags,
        test_fallback_is_a_floor_not_just_fallback,
        test_fallback_used_when_history_missing,
        test_no_cap_when_generous_risk_budget,
        test_zero_worst_case_is_safe,
        test_backwards_compat_when_kwarg_omitted,
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
    print(f"\n{'PASS' if failed == 0 else f'FAIL — {failed} test(s) failed'}")
    return failed


if __name__ == "__main__":
    sys.exit(_run_all())
