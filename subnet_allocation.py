"""
Subnet Allocation Engine — TAO Monitor Phase 2 (allocation layer)
==================================================================
The bridge from v4 *scores* to *position sizes*. Replaces the hand-set
`TARGETS` relic in gordie.html with a derived target allocation, and feeds
the dashboard Drift + the Telegram rebalance plan.

Design = "be Siam" (locked with Simon, 2026-06-11):
  - Hold only green; cut the red fast.
  - Size the survivors by conviction (tiers), not equal weight.
  - Express everything in PERCENTAGES of the account (no 3τ unit machinery).
  - Exits are fast; rebalances among greens are slow (drift deadband).

Two axes
--------
  Axis 1 — gross exposure ("the dial"):  macro.signal  →  % of account deployed
           vs parked in SN0. Replaces the blunt entry_score×0.3 Bear switch.
  Axis 2 — cross-section:  among deployed capital, cut reds → tier the greens by
           health_score → conviction-weight → cap by pool & max-positions.

The dial only sets *how much* green you hold; selection sets *how many* names.
A deep bear shrinks the whole green book toward SN0 — it never forces you to
concentrate into a single subnet (breadth comes from how many names are green).

Reads only these attributes off each scored object (duck-typed against v4
SubnetScore): subnet_id, name, health_score, markov_regime, pool_depth.
Works on the /status fast path with no balance fetch (targets need no holdings);
pass current_weight_by_id (from get_wallet_stakes, cost-basis path) to also get
Drift + actions.

Author: built for sima936/tao-monitor. Framework lineage: Siam Kidd (DSV) +
Roan (@RohOnChain) Markov macro. Not financial advice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional, Iterable, TYPE_CHECKING

if TYPE_CHECKING:  # avoid a hard import cycle; runtime is pure duck-typing
    from subnet_scoring_engine import SubnetScore, TaoMacroState


# ─────────────────────────────────────────────────────────────────────────────
# Tiers
# ─────────────────────────────────────────────────────────────────────────────

class Tier(Enum):
    APLUS      = "A+"
    A          = "A"
    B          = "B"
    CONVICTION = "CV"   # tagged real-utility vertical, rescued from the marginal
                        # health-floor cut; held at a sub-B toehold (see policy).
    EXIT       = "exit"


# Relative conviction weights per tier (Axis 2). Only the RATIOS matter — they
# are normalised across survivors, so {A+:4, A:2, B:1} means an A+ gets 4× a B.
TIER_WEIGHT = {Tier.APLUS: 4.0, Tier.A: 2.0, Tier.B: 1.0, Tier.CONVICTION: 0.5, Tier.EXIT: 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# Policy
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AllocationPolicy:
    # ── Axis 1: the dial. A clamped LINEAR RAMP maps the macro Markov signal to
    #    the fraction of the WHOLE (staked) account deployed; the rest parks SN0:
    #        signal >= deploy_signal_hi → deploy_ceiling   (full risk-on)
    #        signal <= deploy_signal_lo → deploy_floor     (de-risked, NOT flat)
    #        in between                 → linear interpolation
    #    Hybrid by design: smooth in the middle (no band-edge whipsaw), hard
    #    floor + ceiling at the extremes. Floor > 0 — a soft/negative signal
    #    parks cash in SN0 but never force-flattens the book; hard exits are the
    #    stops'/Bear-regime's job, not the dial's.
    #    PLACEHOLDER numbers — Simon's risk call, Hermes-calibratable later
    #    (narrow surface: four scalars). Worked example: signal -0.18 → ~59%.
    deploy_signal_hi:  float = 0.00    # at/above this signal → full deploy
    deploy_signal_lo:  float = -0.30   # at/below this signal → floor
    deploy_ceiling:    float = 1.00    # max fraction deployed
    deploy_floor:      float = 0.30    # min fraction deployed (rest → SN0)
    unknown_macro_fraction: float = 1.00   # macro unavailable → still fully deployed

    # ── Axis 2: tiering off health_score.
    health_aplus: float = 70.0
    health_a:     float = 55.0
    health_b:     float = 45.0             # was 40 — cut marginal sideways harder ("be Siam")
    cut_on_bear_regime: bool = True        # markov Bear → EXIT regardless of health
    new_entries_only_in_bull: bool = True  # Sideways/Bear/Unknown macro → no NEW (un-held) names
                                           # in the book; rotate in only on Bull. Discovery lives
                                           # on the Opportunities tab, not the allocation plan.

    # ── Conviction guard. Named real-utility verticals are exempt from the
    #    MARGINAL health-floor cut (NOT the Bear-regime cut): instead of →SN0
    #    they drop to the sub-B CONVICTION tier and keep a small toehold, sized
    #    through the same Axis-1 dial as everything else. Tag on thesis, not
    #    price — a name's weakness today is why the floor helps, not a reason to
    #    drop the tag. A tagged name that flips to a real Markov Bear still exits.
    #    {4 Targon, 107 Minos, 46 Zipcode, 44 Score, 68 NOVA, 123 MANTIS}.
    #    NIOME (55) deliberately excluded — unmapped, keeps the set from
    #    collapsing into "just the current book".
    conviction_tags: frozenset = frozenset({4, 107, 46, 44, 68, 123})

    # ── Caps / guards.
    max_weight_per_name: float = 0.40      # no single name above 40% of the ACCOUNT (any tier).
                                           # Of-account (not of-deployed) keeps the cap orthogonal
                                           # to the Axis-1 dial: prevents ~100%-one-name in a bull
                                           # without shaving a lone green in a low-deploy bear.
    aplus_max_weight: float = 0.40         # optional tighter A+-specific cap (min() with the above)
    max_positions:    int   = 10           # ≤10 green names at once
    pool_fraction_cap: float = 0.01        # position ≤ 1% of pool depth (needs account_tao;
                                           # inert at current size — bites only as you scale)
    per_name_full_deploy: float = 0.25     # breadth ceiling: each surviving name backs ≤25% of
                                           # account, so a thin book (few survivors) deploys less
                                           # and parks the rest in SN0 instead of over-concentrating.
                                           # Inert once survivors × this ≥ 1.0.

    # ── Anti-churn. Drift below this (fraction of account) → HOLD, don't fiddle.
    drift_deadband: float = 0.03           # 3% of account
    # Exits (target 0, currently held) ALWAYS act — never deadbanded.

    # ── N-cycle / time confirmation (OPEN #6). A HELD name that would EXIT on a
    #    gated reason isn't cut on the first flag: it's held at a CV-style toehold
    #    (de-risked, not zeroed) until the cut-worthy state has persisted for
    #    `confirm_hours`, then it exits. Time-based (not raw cron count) so the
    #    filter is cadence-independent — flip the cron to 6h/8h without retuning
    #    or weakening it (intraday crons read the same daily Markov bar, so raw
    #    cycle-count confirmation would be diluted; wall-clock isn't).
    #    Gate engages ONLY for held names (can't "pend an exit" on an un-held
    #    name) and only when now_ts is supplied (inert on the holdings-less
    #    /status path → behaves exactly as before).
    confirm_hours: float = 18.0
    confirm_gates: frozenset = frozenset({"bear_regime", "health_below_floor"})


# ─────────────────────────────────────────────────────────────────────────────
# Outputs
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TargetPosition:
    subnet_id:      int
    name:           str
    tier:           str
    health_score:   float
    markov_regime:  str
    target_weight:  float                  # fraction of whole account
    current_weight: Optional[float] = None
    drift:          Optional[float] = None # current - target (fraction of account)
    action:         str = "hold"           # enter / add / hold / trim / exit
    capped_by:      Optional[str] = None   # "pool" / "aplus" / None
    pending_exit:   bool = False           # cut-worthy but inside the confirm window:
                                           # held at a toehold, not yet zeroed (OPEN #6)
    reason:         str = ""
    genie_score:    Optional[float] = None # real concentration if fetched; 0.5
                                           # placeholder / None = NOT fetched this run


@dataclass
class AllocationPlan:
    macro_signal:      float
    macro_regime:      str
    deployed_fraction: float               # Axis 1 result
    sn0_target_weight: float               # parked / "cash"
    positions:         list                # list[TargetPosition], target desc
    cut:               list                # subnets pushed to SN0 (dicts)
    notes:             list = field(default_factory=list)
    cut_since:         dict = field(default_factory=dict)  # {sid: {"since_ts","reason"}}
                                           # carried back into prev_state by run() — the
                                           # persistent confirmation streak (OPEN #6).

    def to_dict(self) -> dict:
        return {
            "macro_signal": round(self.macro_signal, 4),
            "macro_regime": self.macro_regime,
            "deployed_fraction": round(self.deployed_fraction, 4),
            "sn0_target_weight": round(self.sn0_target_weight, 4),
            "positions": [asdict(p) for p in self.positions],
            "cut": self.cut,
            "notes": self.notes,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Axis 1 — the dial
# ─────────────────────────────────────────────────────────────────────────────

def deployed_fraction(signal: float, policy: AllocationPolicy) -> float:
    """Map the macro Markov signal (−1..+1) to the fraction of the account
    deployed (vs parked in SN0) via a clamped linear ramp:

        signal >= deploy_signal_hi → deploy_ceiling
        signal <= deploy_signal_lo → deploy_floor
        between                    → linear interpolation

    Hybrid: smooth in the middle (no band-edge whipsaw), hard floor + ceiling
    at the extremes. Floor > 0 so a soft/negative signal parks cash without
    flattening the book — hard exits are the stops'/Bear-regime's job.
    """
    hi, lo = policy.deploy_signal_hi, policy.deploy_signal_lo
    ceil, floor = policy.deploy_ceiling, policy.deploy_floor
    if hi <= lo:                       # misconfigured ramp → fail safe to floor
        return floor
    if signal >= hi:
        return ceil
    if signal <= lo:
        return floor
    t = (signal - lo) / (hi - lo)      # 0..1
    return floor + t * (ceil - floor)


# ─────────────────────────────────────────────────────────────────────────────
# Axis 2 — tiering
# ─────────────────────────────────────────────────────────────────────────────

def classify_tier(health: float, regime: str, policy: AllocationPolicy) -> Tier:
    """Cut the worst, tier the rest. Bear regime is an immediate cut."""
    if policy.cut_on_bear_regime and regime == "Bear":
        return Tier.EXIT
    if health >= policy.health_aplus:
        return Tier.APLUS
    if health >= policy.health_a:
        return Tier.A
    if health >= policy.health_b:
        return Tier.B
    return Tier.EXIT


# ─────────────────────────────────────────────────────────────────────────────
# The allocator
# ─────────────────────────────────────────────────────────────────────────────

def compute_target_allocation(
    eligible_scored: Iterable,                          # v4 SubnetScore objects (post pre-filter)
    macro: "TaoMacroState",
    policy: Optional[AllocationPolicy] = None,
    *,
    account_tao: Optional[float] = None,                # enables the pool cap (else skipped)
    current_weight_by_id: Optional[dict] = None,        # actual fraction-of-account per subnet → Drift
    sn0_id: int = 0,
    cut_since: Optional[dict] = None,                   # {sid:{"since_ts","reason"}} from prev_state
    now_ts: Optional[float] = None,                     # wall-clock epoch; enables time-confirmation
    force_exit: Optional[dict] = None,                  # {sid: "trail_stop"/"hard_stop"} — STEP 2 stop override
) -> AllocationPlan:
    """Derive the target allocation.

    1. Axis 1: macro.signal → deployed fraction f (rest → SN0).
    2. Axis 2: classify each scored subnet; EXIT = cut to SN0.
    3. Keep the top `max_positions` survivors by health.
    4. Conviction-weight survivors → normalise → × f → fraction of account.
    5. Apply A+ cap and (if account_tao given) pool cap; overflow → SN0.
    6. If current weights supplied: compute Drift + per-name action
       (exits act immediately; rebalances respect the deadband).
    """
    policy = policy or AllocationPolicy()
    notes: list[str] = []
    force_exit = {int(k): str(v) for k, v in (force_exit or {}).items()}
    forced_conviction_exits: list[tuple] = []

    macro_available = getattr(macro, "available", True)
    signal = float(getattr(macro, "signal", 0.0) or 0.0)
    regime = getattr(getattr(macro, "regime", None), "value", None) or str(getattr(macro, "regime", "Unknown"))

    # ── Axis 1 ────────────────────────────────────────────────────────────────
    if macro_available:
        f = deployed_fraction(signal, policy)
        notes.append(
            f"Dial: signal {signal:+.3f} → {f:.0%} deployed, {1.0 - f:.0%} parked SN0 "
            f"(ramp {policy.deploy_floor:.0%}@{policy.deploy_signal_lo:+.2f} → "
            f"{policy.deploy_ceiling:.0%}@{policy.deploy_signal_hi:+.2f})."
        )
    else:
        f = policy.unknown_macro_fraction
        notes.append("Macro unavailable — deployed fraction set conservatively; treat new entries as deferred.")

    # ── Axis 2: classify ──────────────────────────────────────────────────────
    # Capital preservation: outside a Bull macro, don't introduce NEW (un-held)
    # names into the book — hold/cut what you have, rotate in only on Bull. Needs
    # holdings (current_weight_by_id); on the holdings-less /status path this is a
    # no-op (run() already feeds holdings-only there).
    held_ids_known = current_weight_by_id is not None
    held_set = {sid for sid, w in (current_weight_by_id or {}).items() if w and w > 0}
    allow_new_entries = (regime == "Bull") or (not policy.new_entries_only_in_bull)
    suppressed_new = 0
    conviction_floored = 0
    flagged_not_entered: list = []   # un-held names the engine reads as chasers/exit-zones:
                                     # kept VISIBLE in a note, NOT sized into an ENTER.

    # Time-confirmation state (OPEN #6). Inert when now_ts is absent (e.g. the
    # holdings-less /status path) → exits act immediately, exactly as before.
    now = float(now_ts) if now_ts is not None else 0.0
    gate_active = now_ts is not None       # epoch 0.0 is a valid ts; don't use now>0
    prior_cut_since = {int(k): v for k, v in (cut_since or {}).items()}
    new_cut_since: dict[int, dict] = {}
    pending_meta: dict[int, tuple] = {}   # sid -> (reason, elapsed_hours)
    pending_count = 0

    survivors: list = []
    cut: list[dict] = []
    for s in eligible_scored:
        sid0 = int(getattr(s, "subnet_id"))
        if sid0 == sn0_id:
            continue  # SN0 is the sink, never a holding
        # ── STEP 2: a fired trailing/hard stop is a same-cycle hard exit. It
        #    overrides the conviction floor AND the 18h confirm gate — a full
        #    exit (→SN0), not a toehold, not pending. Stops are only ever set
        #    for held names, so this never creates/zeroes an un-held subnet. ──
        if sid0 in force_exit:
            _h = round(float(getattr(s, "health_score", 0.0)), 1)
            _r = str(getattr(s, "markov_regime", "Unknown"))
            cut.append({"subnet_id": sid0, "name": getattr(s, "name", ""),
                        "health_score": _h, "markov_regime": _r,
                        "reason": force_exit[sid0]})
            if sid0 in policy.conviction_tags:
                forced_conviction_exits.append((sid0, getattr(s, "name", ""), force_exit[sid0]))
            continue
        if held_ids_known and not allow_new_entries and sid0 not in held_set:
            suppressed_new += 1
            continue  # non-Bull macro → no new exposure; leave discovery to Opportunities
        # ── A NEW (un-held) name the engine flags as a chaser / exit-zone is NOT an
        #    entry: it's at/near its high (take_profit_flags: AT_RECENT_HIGH /
        #    TAKE_PROFIT*) or explicitly CHASING. The scoring engine already drops
        #    these from its buy list; the allocator must too, or it sizes an ENTER
        #    into a name it simultaneously says to take profit on (SN77/SN14/SN83).
        #    Don't size it — keep it VISIBLE in a flagged note so nothing is hidden;
        #    the entry call is the operator's, not an auto-ENTER. Held names are
        #    exempt (a holding at its high is a TRIM decision, handled below).
        if held_ids_known and sid0 not in held_set:
            _tp = getattr(s, "take_profit_flags", None) or []
            _ef = getattr(s, "entry_flags", None) or []
            if _tp or any("CHASING" in str(fl).upper() for fl in _ef):
                _why = "at/near high — take-profit zone" if _tp else "chasing — wait for pullback"
                flagged_not_entered.append((sid0, getattr(s, "name", ""), _why))
                continue
        health = float(getattr(s, "health_score", 0.0))
        s_regime = str(getattr(s, "markov_regime", "Unknown"))
        tier = classify_tier(health, s_regime, policy)

        if tier != Tier.EXIT:
            survivors.append((s, tier, health, s_regime))
            continue  # healthy → not cut-worthy → any prior streak auto-resets
                      # (simply by not being carried into new_cut_since)

        # ── Cut-worthy. Classify the reason. ──────────────────────────────────
        is_bear_cut = policy.cut_on_bear_regime and s_regime == "Bear"
        reason = "bear_regime" if is_bear_cut else "health_below_floor"

        # Conviction guard (unchanged in spirit): a tagged vertical cut ONLY on
        # the marginal health floor (not a Bear regime) keeps a CV toehold instead
        # of →SN0. That is a permanent thesis hold, NOT a confirmation window, so
        # it does not touch cut_since. A tagged name in a real Bear regime falls
        # through to the time-gate below — and then exits once confirmed.
        if (sid0 in policy.conviction_tags) and not is_bear_cut:
            survivors.append((s, Tier.CONVICTION, health, s_regime))
            conviction_floored += 1
            continue

        # ── Time-confirmation gate (HELD names only; never creates a position) ─
        held = held_ids_known and sid0 in held_set
        if held and gate_active and reason in policy.confirm_gates:
            since = prior_cut_since.get(sid0, {}).get("since_ts", now)
            elapsed_h = (now - float(since)) / 3600.0
            if elapsed_h < policy.confirm_hours:
                # PENDING: de-risk to a toehold now, confirm before zeroing.
                new_cut_since[sid0] = {"since_ts": float(since), "reason": reason}
                pending_meta[sid0] = (reason, elapsed_h)
                pending_count += 1
                survivors.append((s, Tier.CONVICTION, health, s_regime))
                continue
            # else: persisted ≥ confirm_hours → confirmed → fall through to cut.
            # Streak not carried forward (the name is leaving the book).

        cut.append({
            "subnet_id": sid0,
            "name": getattr(s, "name", ""),
            "health_score": round(health, 1),
            "markov_regime": s_regime,
            "reason": reason,
        })

    # ── Max positions: keep the healthiest N; spill the rest to the cut list ──
    survivors.sort(key=lambda t: t[2], reverse=True)
    if len(survivors) > policy.max_positions:
        for s, tier, health, s_regime in survivors[policy.max_positions:]:
            cut.append({
                "subnet_id": int(getattr(s, "subnet_id")),
                "name": getattr(s, "name", ""),
                "health_score": round(health, 1),
                "markov_regime": s_regime,
                "reason": f"beyond_max_positions_{policy.max_positions}",
            })
        survivors = survivors[:policy.max_positions]

    # ── Breadth-aware deploy ceiling: each surviving name backs at most
    #    per_name_full_deploy of the account, so a thin book parks the remainder
    #    in SN0 (residual) instead of over-concentrating. Only ever LOWERS f;
    #    inert once survivors are plentiful (ceiling ≥ current f). ──
    if survivors:
        breadth_ceiling = len(survivors) * policy.per_name_full_deploy
        if breadth_ceiling < f:
            notes.append(
                f"Breadth ceiling — only {len(survivors)} name(s) cleared the floor → "
                f"deploy capped {f:.0%}→{breadth_ceiling:.0%} (≤{policy.per_name_full_deploy:.0%} "
                f"each); the rest parks in SN0 rather than over-concentrating."
            )
            f = breadth_ceiling

    # ── Conviction weights → normalised fraction of DEPLOYED capital ──────────
    positions: list[TargetPosition] = []
    if survivors:
        raw = {int(getattr(s, "subnet_id")): TIER_WEIGHT[tier] for s, tier, _, _ in survivors}
        total_raw = sum(raw.values()) or 1.0
        # fraction of account = (conviction share) × deployed fraction
        weights = {sid: (w / total_raw) * f for sid, w in raw.items()}

        # ── Per-name cap (every tier, as a share of the ACCOUNT) ─────────────
        # Of-account (not of-deployed) keeps the cap orthogonal to the dial: it
        # prevents ~100%-one-name in a bull, but never shaves a lone green in a
        # low-deploy bear (which would make deploy% and SN0% disagree). A+ keeps
        # an optionally-tighter cap via min().
        capped_flags: dict[int, str] = {}
        per_name_cap_abs = policy.max_weight_per_name
        aplus_cap_abs = policy.aplus_max_weight
        for s, tier, _, _ in survivors:
            sid = int(getattr(s, "subnet_id"))
            cap_abs = min(per_name_cap_abs, aplus_cap_abs) if tier == Tier.APLUS else per_name_cap_abs
            if weights[sid] > cap_abs:
                capped_flags[sid] = "aplus" if tier == Tier.APLUS else "name"
                weights[sid] = cap_abs

        # ── Pool cap (needs account size; inert at current scale) ────────────
        if account_tao and account_tao > 0:
            for s, tier, _, _ in survivors:
                sid = int(getattr(s, "subnet_id"))
                pool = float(getattr(s, "pool_depth", 0.0) or 0.0)
                if pool > 0:
                    pool_cap_w = (policy.pool_fraction_cap * pool) / account_tao
                    if weights[sid] > pool_cap_w:
                        capped_flags[sid] = "pool"
                        weights[sid] = pool_cap_w
        else:
            notes.append("Pool cap skipped (no account_tao supplied) — non-binding at current size anyway.")

        # Any weight removed by caps simply stays in SN0 (conservative; can be
        # redistributed to uncapped greens in a later refinement).
        for s, tier, health, s_regime in survivors:
            sid = int(getattr(s, "subnet_id"))
            tw = weights[sid]
            cur = None if current_weight_by_id is None else float(current_weight_by_id.get(sid, 0.0))
            is_pending = sid in pending_meta
            # A name pending bear-exit is on its way out — never size it UP. Cap
            # its target at current so the toehold can only HOLD or TRIM toward
            # exit, never ADD. The freed weight falls to SN0 via the residual.
            if is_pending and cur is not None:
                tw = min(tw, cur)
            drift = None if cur is None else (cur - tw)
            action, reason = _decide_action(cur, tw, drift, policy)
            if is_pending:
                p_reason, p_elapsed = pending_meta[sid]
                reason = (f"pending exit ({p_reason}) "
                          f"{p_elapsed:.0f}h/{policy.confirm_hours:.0f}h")
            positions.append(TargetPosition(
                subnet_id=sid,
                name=getattr(s, "name", ""),
                tier=tier.value,
                health_score=round(health, 1),
                markov_regime=s_regime,
                target_weight=round(tw, 4),
                current_weight=None if cur is None else round(cur, 4),
                drift=None if drift is None else round(drift, 4),
                action=action,
                capped_by=capped_flags.get(sid),
                pending_exit=is_pending,
                reason=reason,
                genie_score=getattr(s, "genie_score_raw", None),
            ))

    positions.sort(key=lambda p: p.target_weight, reverse=True)

    # ── Exits for currently-held names that got cut ───────────────────────────
    if current_weight_by_id is not None:
        held_ids = {sid for sid, w in current_weight_by_id.items() if w and w > 0 and sid != sn0_id}
        cut_ids = {c["subnet_id"] for c in cut}
        target_ids = {p.subnet_id for p in positions}
        for c in cut:
            if c["subnet_id"] in held_ids:
                c["action"] = "EXIT"
                c["current_weight"] = round(float(current_weight_by_id[c["subnet_id"]]), 4)
        # held but neither a target nor in cut (shouldn't happen, but be safe)
        for sid in held_ids - target_ids - cut_ids:
            cut.append({"subnet_id": sid, "name": "", "reason": "not_eligible",
                        "action": "EXIT", "current_weight": round(float(current_weight_by_id[sid]), 4)})

    deployed_total = sum(p.target_weight for p in positions)
    sn0_target = max(0.0, 1.0 - deployed_total)

    if regime == "Bear":
        notes.append("Macro Bear — ENTER/ADD actions are advisory; defer new exposure, prioritise the EXITs.")
    if suppressed_new:
        notes.append(f"{regime} macro — new entries suppressed ({suppressed_new} healthy non-held names held back); book limited to current holdings. Rotate in on Bull (see Opportunities).")
    if flagged_not_entered:
        _items = ", ".join(f"SN{sid} {nm} ({why})" for sid, nm, why in flagged_not_entered[:6])
        _more = "" if len(flagged_not_entered) <= 6 else f" +{len(flagged_not_entered) - 6} more"
        notes.append(
            f"⚠️ {len(flagged_not_entered)} new name(s) FLAGGED, not auto-entered (your call): "
            f"{_items}{_more}. Engine reads these as at-high / chasing — shown so you can act "
            f"manually, but the allocator won't size an ENTER into a take-profit zone."
        )
    if conviction_floored:
        notes.append(f"{conviction_floored} conviction-tagged vertical(s) floored at CV tier through the health cut — thesis-held at a toehold; would still exit on a Bear-regime flip.")
    if forced_conviction_exits:
        _names = ", ".join(f"SN{sid} {nm}" for sid, nm, _ in forced_conviction_exits)
        notes.append(f"⚠️ Conviction name(s) hit a stop — thesis check required: {_names}. "
                     f"Stop overrides the conviction floor; exited, not silently held.")
    if pending_count:
        notes.append(f"{pending_count} held name(s) cut-worthy but inside the {policy.confirm_hours:.0f}h confirmation window — de-risked to a toehold (TRIM), full EXIT only once the state persists. Single-cycle blips won't fire a cut.")

    return AllocationPlan(
        macro_signal=signal,
        macro_regime=regime,
        deployed_fraction=f,
        sn0_target_weight=sn0_target,
        positions=positions,
        cut=cut,
        notes=notes,
        cut_since=new_cut_since,
    )


def _decide_action(cur: Optional[float], target: float, drift: Optional[float],
                   policy: AllocationPolicy) -> tuple[str, str]:
    """Translate the target/current gap into an instruction.

    Exits act immediately (cut fast). Rebalances among greens respect the
    deadband (don't churn). Enters/adds are honest targets; the caller flags
    them as advisory under a Bear macro.
    """
    if cur is None:
        return "target", "no holdings supplied"
    if target <= 0:
        return ("exit", "cut to SN0") if cur > 0 else ("hold", "not held / not targeted")
    if cur <= 0:
        return ("enter", "new green target") if target > policy.drift_deadband else ("hold", "target below deadband")
    if drift is None:
        return "hold", ""
    if abs(drift) <= policy.drift_deadband:
        return "hold", f"within {policy.drift_deadband:.0%} deadband"
    return ("trim", "above target") if drift > 0 else ("add", "below target")


# ─────────────────────────────────────────────────────────────────────────────
# Telegram formatter — slots beside format_telegram_alert()
# ─────────────────────────────────────────────────────────────────────────────

def format_allocation_plan(plan: AllocationPlan, account_tao: Optional[float] = None) -> str:
    def tao(w: float) -> str:
        return f" ({w*account_tao:.1f}τ)" if account_tao else ""

    lines = [
        "🧭 ALLOCATION PLAN",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Macro {plan.macro_regime} (signal {plan.macro_signal:+.2f}) "
        f"→ deploy {plan.deployed_fraction:.0%} · SN0 {plan.sn0_target_weight:.0%}{tao(plan.sn0_target_weight)}",
        "",
    ]
    if plan.positions:
        lines.append("🟢 TARGET BOOK (green, conviction-sized):")
        unchecked_entries = []
        for p in plan.positions:
            d = "" if p.drift is None else f" · drift {p.drift:+.0%}"
            act = "" if p.action in ("hold", "target") else f" → {p.action.upper()}"
            cap = f" [{p.capped_by} cap]" if p.capped_by else ""
            pend = f" ⏳{p.reason}" if p.pending_exit else ""
            # Concentration guard on NEW exposure only: an enter/add on a name
            # whose real Gini wasn't fetched (0.5 placeholder / None) has NOT
            # cleared the concentration gate on its true value, so a concentrated
            # name can read as a clean ENTER. Holds/trims don't add exposure → no flag.
            conc = ""
            if p.action in ("enter", "add") and (
                p.genie_score is None or abs(p.genie_score - 0.5) < 1e-9
            ):
                conc = " ⚠️Gini unchecked"
                unchecked_entries.append(p.subnet_id)
            lines.append(
                f"  {p.tier:>2} SN{p.subnet_id} {p.name} — {p.target_weight:.0%}{tao(p.target_weight)}"
                f" (h{p.health_score:.0f}/{p.markov_regime}){cap}{d}{act}{pend}{conc}"
            )
        if unchecked_entries:
            names = ", ".join(f"SN{s}" for s in unchecked_entries)
            lines.append(
                f"  ⚠️ ENTER/ADD on concentration-UNCHECKED names ({names}): real "
                "Gini not fetched this run — verify before deploying; the gate "
                "hasn't seen their true concentration."
            )
        lines.append("")
    exits = [c for c in plan.cut if c.get("action") == "EXIT"]
    if exits:
        lines.append("🔴 CUT TO SN0 (fast — held + failing):")
        for c in exits:
            cw = c.get("current_weight")
            cwf = f" {cw:.0%} →0" if cw is not None else ""
            lines.append(f"  SN{c['subnet_id']} {c.get('name','')} —{cwf} ({c['reason']})")
        lines.append("")
    if plan.notes:
        lines.extend(f"• {n}" for n in plan.notes)
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# Lean action-only Telegram digest — the 7-rung ladder. Sourced ENTIRELY from
# `plan` (the same object an execution agent consumes). Evidence (health, EMA,
# Gini, pullback research, filter counts) lives on the dashboard; the 🚨 stop
# ping stays a separate message. Self-contained — no new module-level names.
# Forward-compatible `pending_entry` slot lights up when the entry-gate build
# lands; until then it renders nothing.
# ─────────────────────────────────────────────────────────────────────────────
def format_actionable_digest(plan, free_tao=None, account_tao=None, ts=None) -> str:
    DOT = {"Bull": "🟢", "Bear": "🔴", "Sideways": "⚪"}

    from subnet_scoring_engine import macro_stance  # canonical stance (single source)

    def clean(reason):
        return (reason or "").replace("_regime", "").replace("_", " ").strip()

    def tao(w):
        return f"{w*account_tao:.1f}τ" if account_tao else f"{w:.0%}"

    def move(p):
        if p.current_weight is None or account_tao is None:
            return f"→ {p.target_weight:.0%} ({tao(p.target_weight)})"
        d = abs(p.current_weight - p.target_weight) * account_tao
        verb = "buy" if getattr(p, "pending_entry", False) else \
            {"trim": "sell", "add": "buy", "enter": "buy"}.get(p.action, "move")
        return f"{verb} ~{d:.1f}τ (→ {p.target_weight:.0%})"

    pos = list(plan.positions)
    L = [
        "📊 TAO MONITOR",
        f"{DOT.get(plan.macro_regime, '❔')} {plan.macro_regime} · "
        f"signal {plan.macro_signal:+.2f} · {macro_stance(plan.macro_regime, plan.macro_signal)}",
        f"Deploy {plan.deployed_fraction:.0%} · cash SN0 "
        f"{plan.sn0_target_weight:.0%} ({tao(plan.sn0_target_weight)})",
        "",
    ]
    rungs = []
    for p in pos:  # 🟢⏳ PENDING-BUY (forward-compat)
        if getattr(p, "pending_entry", False):
            rungs.append(f"🟢⏳ ENTERING SN{p.subnet_id} {p.name} — confirming ({move(p)})")
    for p in pos:  # 🟢 ENTER
        if p.action == "enter" and not getattr(p, "pending_entry", False):
            rungs.append(f"🟢 ENTER SN{p.subnet_id} {p.name}  {move(p)}  ({clean(p.reason)})")
    for p in pos:  # 🟢 ADD
        if p.action == "add":
            rungs.append(f"🟢 ADD   SN{p.subnet_id} {p.name}  {move(p)}")
    holds = [p for p in pos if p.action == "hold" and not p.pending_exit
             and not getattr(p, "pending_entry", False)]
    if holds:  # ⚪ HOLD — bare regime dot per name (watching, at target)
        names = " · ".join(f"{DOT.get(p.markov_regime, '❔')} {p.name}" for p in holds)
        rungs.append(f"⚪ HOLD  {names}")
    for p in pos:  # 🟠 TRIM
        if p.action == "trim":
            rungs.append(f"🟠 TRIM  SN{p.subnet_id} {p.name}  {move(p)}")
    for p in pos:  # 🔴⏳ PENDING-SELL — straight: regime + gate, no prose
        if p.pending_exit:
            last = (p.reason or "").split()[-1] if (p.reason or "").strip() else ""
            gate = last if "/" in last else ""
            desc = ", ".join(x for x in ((p.markov_regime or "").lower(), gate) if x)
            tail = f" — {desc}" if desc else ""
            rungs.append(f"🔴⏳ EXITING SN{p.subnet_id} {p.name}{tail}")
    for c in [c for c in plan.cut if c.get("action") == "EXIT"]:  # 🔴 SELL
        cw = c.get("current_weight")
        amt = f" unstake {cw*account_tao:.1f}τ ·" if (cw is not None and account_tao) else ""
        rungs.append(f"🔴 SELL  SN{c['subnet_id']} {c.get('name','')} —{amt} ({clean(c.get('reason',''))})")

    L.extend(rungs if rungs else ["(no actions — book at target)"])
    L.append("")
    if free_tao is not None and free_tao > 0.005:
        if plan.deployed_fraction >= 0.999:
            L.append(f"🟢 Rotate free {free_tao:.2f}τ → greens (dial full risk-on)")
        else:
            L.append(f"🅿️ Park free {free_tao:.2f}τ → SN0 (dial {plan.deployed_fraction:.0%}, soft)")
    acct = f" · acct ~{account_tao:.1f}τ" if account_tao else ""
    L.append(f"⏰ {ts or '—'}{acct} · details → dashboard")
    return "\n".join(L)
