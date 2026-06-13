"""Smoke test for the OPEN #6 time-confirmation gate in subnet_allocation.py.

Duck-types SubnetScore + TaoMacroState (the allocator only reads attributes).
No numpy/pandas needed — subnet_allocation imports math + stdlib only.
"""
from types import SimpleNamespace as NS
from subnet_allocation import compute_target_allocation, AllocationPolicy

HOUR = 3600.0
POL = AllocationPolicy()  # confirm_hours=18, gates={bear_regime, health_below_floor}


def sn(sid, name, health, regime, pool=150.0):
    return NS(subnet_id=sid, name=name, health_score=health,
              markov_regime=regime, pool_depth=pool)


def macro(signal, regime):
    return NS(available=True, signal=signal, regime=regime)


def run(scored, held, signal, regime, now_ts, cut_since=None):
    cw = {s.subnet_id: 0.10 for s in scored if s.subnet_id in held}
    return compute_target_allocation(
        scored, macro(signal, regime),
        account_tao=None, current_weight_by_id=cw,
        cut_since=cut_since, now_ts=now_ts,
    )


def ids_in_cut(plan):
    return {c["subnet_id"]: c["reason"] for c in plan.cut}


def pos_by_id(plan):
    return {p.subnet_id: p for p in plan.positions}


PASS, FAIL = "✅", "❌"
def check(label, cond):
    print(f"  {PASS if cond else FAIL} {label}")
    assert cond, label


print("\n[1] Held BEAR name at t=0 → PENDING (toehold), not cut")
A = sn(10, "Alpha", 60, "Bear")
p = run([A], held={10}, signal=-0.2, regime="Bear", now_ts=0.0)
pos = pos_by_id(p)
check("10 is a position (held at toehold)", 10 in pos)
check("10 flagged pending_exit", 10 in pos and pos[10].pending_exit)
check("10 NOT in cut list", 10 not in ids_in_cut(p))
check("cut_since records 10", 10 in p.cut_since and p.cut_since[10]["reason"] == "bear_regime")

print("\n[2] Same name at t=+6h (since=0) → STILL PENDING (6h < 18h)")
p = run([A], held={10}, signal=-0.2, regime="Bear", now_ts=6*HOUR,
        cut_since={10: {"since_ts": 0.0, "reason": "bear_regime"}})
check("10 still a pending position", 10 in pos_by_id(p) and pos_by_id(p)[10].pending_exit)
check("10 still not cut", 10 not in ids_in_cut(p))
check("since_ts preserved at 0", p.cut_since[10]["since_ts"] == 0.0)

print("\n[3] Same name at t=+18h (since=0) → CONFIRMED EXIT")
p = run([A], held={10}, signal=-0.2, regime="Bear", now_ts=18*HOUR,
        cut_since={10: {"since_ts": 0.0, "reason": "bear_regime"}})
check("10 IS in cut, reason bear_regime", ids_in_cut(p).get(10) == "bear_regime")
check("10 NOT a position anymore", 10 not in pos_by_id(p))
check("streak dropped (not carried)", 10 not in p.cut_since)

print("\n[4] Name recovers at t=+6h (Bull, healthy) → streak RESETS")
A_ok = sn(10, "Alpha", 65, "Bull")
p = run([A_ok], held={10}, signal=0.5, regime="Bull", now_ts=6*HOUR,
        cut_since={10: {"since_ts": 0.0, "reason": "bear_regime"}})
check("10 is a healthy position", 10 in pos_by_id(p) and not pos_by_id(p)[10].pending_exit)
check("10 not cut", 10 not in ids_in_cut(p))
check("cut_since cleared for 10", 10 not in p.cut_since)

print("\n[5] Held HEALTH-FLOOR name (untagged, h30) → pending → confirm")
B = sn(11, "Beta", 30, "Sideways")
p0 = run([B], held={11}, signal=-0.2, regime="Bear", now_ts=0.0)
check("t=0 pending (health_below_floor)", 11 in pos_by_id(p0) and pos_by_id(p0)[11].pending_exit
      and p0.cut_since[11]["reason"] == "health_below_floor")
p1 = run([B], held={11}, signal=-0.2, regime="Bear", now_ts=18*HOUR,
         cut_since={11: {"since_ts": 0.0, "reason": "health_below_floor"}})
check("t=+18h confirmed exit", ids_in_cut(p1).get(11) == "health_below_floor")

print("\n[6] TAGGED name on health floor (sid=4, h30, Sideways) → CV hold, NO streak")
C = sn(4, "Targon", 30, "Sideways")  # 4 ∈ conviction_tags
p = run([C], held={4}, signal=-0.2, regime="Bear", now_ts=0.0)
check("4 is a CV position", 4 in pos_by_id(p) and pos_by_id(p)[4].tier == "CV")
check("4 NOT pending (permanent thesis hold, not a window)", not pos_by_id(p)[4].pending_exit)
check("4 NOT in cut_since (no confirmation clock)", 4 not in p.cut_since)

print("\n[6b] TAGGED name in a real BEAR regime (sid=4) → goes through the gate")
C_bear = sn(4, "Targon", 60, "Bear")
p = run([C_bear], held={4}, signal=-0.2, regime="Bear", now_ts=0.0)
check("4 pending under bear (tag does NOT exempt bear)", 4 in pos_by_id(p) and pos_by_id(p)[4].pending_exit)
p = run([C_bear], held={4}, signal=-0.2, regime="Bear", now_ts=18*HOUR,
        cut_since={4: {"since_ts": 0.0, "reason": "bear_regime"}})
check("4 exits once bear confirmed", ids_in_cut(p).get(4) == "bear_regime")

print("\n[7] now_ts=None (e.g. /status path) → INERT, immediate cut (backward-compat)")
p = run([sn(12, "Gamma", 60, "Bear")], held={12}, signal=-0.2, regime="Bear", now_ts=None)
check("12 cut immediately, no pending", ids_in_cut(p).get(12) == "bear_regime")
check("no cut_since written", p.cut_since == {})

print("\n[8] UN-HELD bear name (Bull macro so not suppressed) → cut now, never a toehold")
p = run([sn(13, "Delta", 60, "Bear")], held=set(), signal=0.5, regime="Bull", now_ts=0.0)
check("13 cut (un-held names are never 'pended' into a position)", ids_in_cut(p).get(13) == "bear_regime")
check("13 not a position", 13 not in pos_by_id(p))
check("13 not in cut_since", 13 not in p.cut_since)

print("\nAll assertions passed.\n")
