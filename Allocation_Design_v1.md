# TAO Monitor — Allocation Design (v1, "be Siam")

**Created:** 2026-06-11 · **Supersedes:** `Allocation_Policy_Brief.md`
**Status:** model locked with Simon; engine layer built + verified; wiring is next.
**Module:** `subnet_allocation.py` → `compute_target_allocation()`

---

## Purpose

The bridge from v4 **scores** to **position sizes**. Kills the hand-set `TARGETS`
relic in `gordie.html` and replaces it with a derived target allocation that
feeds the dashboard **Drift** (actual − target) and the Telegram rebalance plan.
Becomes the input to the Phase-3 rebalancer.

Selection (v4 pre-filters + 10-gate scorer + macro) was already built. This is
the missing **allocation** half.

---

## The principle: be Siam

Siam Kidd holds only green, uptrending subnets and cuts the failing ones fast.
The whole model is that one sentence, mechanised:

1. **Hold only green; cut the red fast.**
2. **Size the survivors by conviction**, not equal weight.
3. **Express everything in percentages** of the account — no fixed-unit (3τ)
   machinery; percentages scale with the account for free.
4. **Exits are fast; rebalances are slow.** A failing name is cut next cron with
   no hesitation; weight is only *shuffled between* healthy names when something
   genuinely changes (drift deadband).

Decisions locked: **1A** (cut the worst) + **2B** (conviction-tiered) + **3A**
(percentages). The earlier "3τ unit" framing is dropped.

---

## Two axes

### Axis 1 — gross exposure ("the dial")

The continuous macro Markov signal sets **what % of the account is deployed** vs
parked in SN0. This *replaces the blunt `entry_score *= 0.3` Bear switch* in v4
(it does **not** touch `p2_macro`, which stays a 15% scoring input).

| `macro.signal` | regime | deployed | parked in SN0 |
|---|---|---|---|
| ≥ +0.40 | strong bull | 100% | 0% |
| +0.10 … +0.40 | bull | 80% | 20% |
| −0.10 … +0.10 | sideways | 50% | 50% |
| −0.40 … −0.10 | mild bear | 25% | 75% |
| ≤ −0.40 | deep bear | 15% | 85% |
| (macro unavailable) | — | 25% | 75% |

Stepped for legibility and to avoid whipsaw at band edges; can become a smooth
curve later. **The dial only sets *how much* green you hold — never *how many***
**names.** Breadth comes from selection, so a deep bear shrinks the whole green
book toward SN0 rather than concentrating you into one subnet.

### Axis 2 — cross-section (which greens, how much)

Among deployed capital:

1. **Cut the worst → SN0.** A subnet is cut if `markov_regime == Bear` (immediate)
   **or** `health_score < health_b`.
2. **Tier the survivors** off `health_score` (health = the stability axis;
   `entry_score` is for *timing entries*, not *sizing holds*):

   | tier | health | conviction weight |
   |---|---|---|
   | A+ | ≥ 70 | 4 |
   | A  | ≥ 55 | 2 |
   | B  | ≥ 40 | 1 |
   | exit | < 40 (or Bear) | 0 → SN0 |

3. **Conviction-weight** survivors (ratios normalised across the survivor set),
   then **× deployed fraction** = each name's % of the whole account.
4. **Caps:** no single A+ above `aplus_max_weight` (40%) of deployed; each
   position ≤ `pool_fraction_cap` (1%) of its pool depth (needs `account_tao`;
   **inert at current size** — at 34τ a 1% pool slice is never the binding
   constraint; bites only as the account scales). Capped weight stays in SN0.
5. **≤ `max_positions` (10)** green names; lowest-health survivors beyond 10 are
   cut to SN0.

`target SN0 % = 1 − Σ target position %`.

---

## Anti-churn (exits fast, rebalances slow)

- **Exits** (held name → target 0): act immediately, never deadbanded.
- **Rebalances** among greens: only act when `|drift| > drift_deadband` (3% of
  account). Smaller gaps read `HOLD`.
- **ENTER/ADD under a Bear macro** are advisory — the plan computes them, but the
  note says defer new exposure and prioritise the exits.
- Pairs with the Phase-3 cooldown (don't round-trip the same subnet within N hrs).

---

## Engine contract

```python
compute_target_allocation(
    eligible_scored,          # iterable of v4 SubnetScore (post pre-filter)
    macro,                    # v4 TaoMacroState
    policy=AllocationPolicy(),
    *,
    account_tao=None,         # enables the pool cap; else skipped (inert now)
    current_weight_by_id=None,# {netuid: fraction-of-account} → enables Drift + actions
    sn0_id=0,
) -> AllocationPlan
```

- Reads only `subnet_id, name, health_score, markov_regime, pool_depth` off each
  score (duck-typed — no import cycle with the scoring engine).
- **Targets need no holdings** → safe on the `/status` fast path (no balance
  fetch). Supplying `current_weight_by_id` adds Drift + per-name actions on the
  cost-basis cron path.
- `AllocationPlan.to_dict()` → JSON for the dashboard; `format_allocation_plan()`
  → Telegram block (sits beside `format_telegram_alert`).

`AllocationPolicy` exposes every number above as a field for tuning.

---

## Verified behaviour (live book, 2026-06-11)

Book: `[107 Minos h60 Bull, 9 iota h41, 55 NIOME h25 Bear, 44 Score h40,
68 NOVA h36, 46 Zipcode h40 Bear, 4 Targon h35, 123 MANTIS h39]`, account ≈ 34τ.

**Deep bear (signal −0.89):** deploy 15% · SN0 85% (28.8τ).
- Keep: Minos 8% (2.5τ, TRIM from 22%), iota 4%, Score 4%.
- **Cut to SN0:** NIOME & Zipcode (bear), NOVA, Targon, MANTIS (health). ~46% of
  the account moves to SN0. This is the brief's thesis made real — the process
  *cuts*, it does not reweight.

**Strong bull (+0.55), same names:** deploy 100% · SN0 0%; same three greens
scaled up by conviction (reds still cut).

---

## Integration plan (Phase-2 wiring — next)

1. **`run_scoring.py` → `run()`:** after `run_scoring_cycle`, build
   `eligible_scored` (the `ranked_by_health` survivors), call
   `compute_target_allocation(...)` with `macro`. On the cost-basis cron path,
   pass `current_weight_by_id` (derive from the `get_wallet_stakes` balances
   already fetched — **pairs with LIVE_STATE #5 de-dup**, no extra call).
2. **Dashboard push:** add `plan.to_dict()` under an `allocation` key in the
   score JSON pushed via `/api/ingest-score`.
3. **`gordie.html`:** delete `TARGETS`; render Drift = `current_weight −
   target_weight` per holding from the new `allocation` block.
4. **Telegram:** append `format_allocation_plan(plan)` to the cron digest
   (keep `/status` lean — plan only on the cost-basis path).

---

## Open calibration knobs (defaults are starting points)

1. **Tier health cuts (70/55/40):** with the current weak book, iota (41) & Score
   (40) scrape into B. Raise `health_b` to ~45 to cut marginal sideways names too
   (more aggressively Siam).
2. **Single-name cap:** the A+ cap only bites A+ names; in a thin book an A name
   can reach ~50% (Minos, bull case). Add a general per-name cap if undesired.
   Self-resolves as more names go green.
3. **Allocation sizes eligible names only** — new greens come from the scorer's
   candidate/entry side and flow in as `eligible_scored`. Confirm `run()` feeds
   the candidate survivors in, not just current holdings, in bull macros.
4. **Dial bands & deadband:** 5 steps + 3% deadband are conservative; tune once a
   few weeks of live target-vs-actual data exist.
5. **Capped-weight handling:** currently leaks to SN0; could redistribute to
   uncapped greens. Moot until pool/A+ caps bind.
