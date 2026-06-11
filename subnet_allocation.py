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
    APLUS = "A+"
    A     = "A"
    B     = "B"
    EXIT  = "exit"


# Relative conviction weights per tier (Axis 2). Only the RATIOS matter — they
# are normalised across survivors, so {A+:4, A:2, B:1} means an A+ gets 4× a B.
TIER_WEIGHT = {Tier.APLUS: 4.0, Tier.A: 2.0, Tier.B: 1.0, Tier.EXIT: 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# Policy
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AllocationPolicy:
    # ── Axis 1: the dial. (signal_floor, deployed_fraction), highest floor first.
    #    deployed_fraction is the % of the WHOLE account that is live (rest → SN0).
    deploy_bands: tuple = (
        ( 0.40, 1.00),   # strong bull  → fully deployed
        ( 0.10, 0.80),   # bull
        (-0.10, 0.50),   # sideways
        (-0.40, 0.25),   # mild bear
        (-9.99, 0.15),   # deep bear (catch-all) → small green book, rest SN0
    )
    unknown_macro_fraction: float = 0.25   # macro unavailable → conservative

    # ── Axis 2: tiering off health_score.
    health_aplus: float = 70.0
    health_a:     float = 55.0
    health_b:     float = 40.0
    cut_on_bear_regime: bool = True        # markov Bear → EXIT regardless of health

    # ── Caps / guards.
    aplus_max_weight: float = 0.40         # no single A+ above 40% of DEPLOYED
    max_positions:    int   = 10           # ≤10 green names at once
    pool_fraction_cap: float = 0.01        # position ≤ 1% of pool depth (needs account_tao;
                                           # inert at current size — bites only as you scale)

    # ── Anti-churn. Drift below this (fraction of account) → HOLD, don't fiddle.
    drift_deadband: float = 0.03           # 3% of account
    # Exits (target 0, currently held) ALWAYS act — never deadbanded.


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
    reason:         str = ""


@dataclass
class AllocationPlan:
    macro_signal:      float
    macro_regime:      str
    deployed_fraction: float               # Axis 1 result
    sn0_target_weight: float               # parked / "cash"
    positions:         list                # list[TargetPosition], target desc
    cut:               list                # subnets pushed to SN0 (dicts)
    notes:             list = field(default_factory=list)

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
    """Map the continuous macro Markov signal (−1..+1) to the fraction of the
    account that is deployed (vs parked in SN0). Stepped for legibility + to
    avoid whipsaw at band edges; refine to a smooth curve later if desired."""
    for floor, frac in policy.deploy_bands:
        if signal >= floor:
            return frac
    return policy.deploy_bands[-1][1]


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

    macro_available = getattr(macro, "available", True)
    signal = float(getattr(macro, "signal", 0.0) or 0.0)
    regime = getattr(getattr(macro, "regime", None), "value", None) or str(getattr(macro, "regime", "Unknown"))

    # ── Axis 1 ────────────────────────────────────────────────────────────────
    if macro_available:
        f = deployed_fraction(signal, policy)
    else:
        f = policy.unknown_macro_fraction
        notes.append("Macro unavailable — deployed fraction set conservatively; treat new entries as deferred.")

    # ── Axis 2: classify ──────────────────────────────────────────────────────
    survivors: list = []
    cut: list[dict] = []
    for s in eligible_scored:
        if int(getattr(s, "subnet_id")) == sn0_id:
            continue  # SN0 is the sink, never a holding
        health = float(getattr(s, "health_score", 0.0))
        s_regime = str(getattr(s, "markov_regime", "Unknown"))
        tier = classify_tier(health, s_regime, policy)
        if tier == Tier.EXIT:
            cut.append({
                "subnet_id": int(getattr(s, "subnet_id")),
                "name": getattr(s, "name", ""),
                "health_score": round(health, 1),
                "markov_regime": s_regime,
                "reason": "bear_regime" if (policy.cut_on_bear_regime and s_regime == "Bear")
                          else "health_below_floor",
            })
        else:
            survivors.append((s, tier, health, s_regime))

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

    # ── Conviction weights → normalised fraction of DEPLOYED capital ──────────
    positions: list[TargetPosition] = []
    if survivors:
        raw = {int(getattr(s, "subnet_id")): TIER_WEIGHT[tier] for s, tier, _, _ in survivors}
        total_raw = sum(raw.values()) or 1.0
        # fraction of account = (conviction share) × deployed fraction
        weights = {sid: (w / total_raw) * f for sid, w in raw.items()}

        # ── A+ cap (per single A+ name, as a share of DEPLOYED) ──────────────
        capped_flags: dict[int, str] = {}
        aplus_cap_abs = policy.aplus_max_weight * f
        for s, tier, _, _ in survivors:
            sid = int(getattr(s, "subnet_id"))
            if tier == Tier.APLUS and weights[sid] > aplus_cap_abs:
                capped_flags[sid] = "aplus"
                weights[sid] = aplus_cap_abs

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
            drift = None if cur is None else (cur - tw)
            action, reason = _decide_action(cur, tw, drift, policy)
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
                reason=reason,
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

    return AllocationPlan(
        macro_signal=signal,
        macro_regime=regime,
        deployed_fraction=f,
        sn0_target_weight=sn0_target,
        positions=positions,
        cut=cut,
        notes=notes,
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
        for p in plan.positions:
            d = "" if p.drift is None else f" · drift {p.drift:+.0%}"
            act = "" if p.action in ("hold", "target") else f" → {p.action.upper()}"
            cap = f" [{p.capped_by} cap]" if p.capped_by else ""
            lines.append(
                f"  {p.tier:>2} SN{p.subnet_id} {p.name} — {p.target_weight:.0%}{tao(p.target_weight)}"
                f" (h{p.health_score:.0f}/{p.markov_regime}){cap}{d}{act}"
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
