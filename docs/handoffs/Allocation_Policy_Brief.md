# TAO Monitor — Allocation Policy Brief (for next session)

**Created:** 2026-06-10 (end of session that shipped LIVE_STATE_9 items #1–#4)
**Purpose:** Lock the allocation decisions made in discussion so the next chat can
write the full design doc + engine layer and kill the relic `TARGETS`.

---

## The problem

`TARGETS` in `gordie.html` is a **dead relic** — it carries Mark Jeffries'
conviction picks (scraped via Lewis Jackson) that Siam Kidd explicitly rejected
as too passive, AND it points at subnets (5/32/75) Simon no longer holds. It
should be **deleted and replaced by a derived allocation process**, not hand-set.

Key insight: the **selection** half is already built (v4 pre-filters + 10-gate
scorer + macro regime). The gap is **allocation** — the bridge from scores →
position sizes. `TARGETS` squats where that bridge belongs.

---

## Decisions locked (Simon's risk posture)

- **Unit sizing:** buy/hold subnets in flat **3τ units**. Rotations move whole
  units. Unit size **scales up as the account grows** (3τ now → larger later).
- **A+ ceiling:** an A+ subnet may hold **3–4 units** now; ceiling **rises with
  account size**.
- **Max open positions:** **≤ 10 subnets** at once (the binding anti-thinness
  constraint — preferred over a min-unit-per-position rule).
- **Pool-depth cap:** max units per subnet capped so stake stays a small % of
  pool depth (illiquidity guard; matters more as units scale). Desired units =
  conviction bucket; **allowed units = min(conviction, pool_cap)**.
- **Anti-churn:** units move only on **bucket-boundary crossings**, not every
  cron wobble (avoid round-trip dTAO slippage). Pairs with Phase-3 cooldowns.
- **SN0 / Root = the unit sink** (de-risk destination / "cash"), not a holding.

## Two-axis model (the design to spec)

- **Axis 1 — gross exposure ("hedge dial"):** the **continuous macro Markov
  signal** sets how many units are deployed vs parked in SN0. Strong bull → ~all
  units live; mild → partial; deep bear (e.g. −0.89 today) → 1–2 units live, rest
  in SN0. Replaces the current binary Bull/Bear switch. *This is the
  "Markov-apportioned hedge amount" Simon asked about.* Can start as a stepped
  dial, refine later.
- **Axis 2 — cross-section:** among deployed units, allocate by v4 score-bucket
  (A+/A/B/exit) × pool-cap, ≤10 positions. This is the unit plan above.
- **Target = derived unit count** per subnet. Dashboard **Drift = actual units −
  target units** (whole units → directly actionable). Replaces manual `TARGETS`.

## Why this fits the constraints (validated)

- At 3τ, a buy is ~0.04% of even the thinnest current pool (MANTIS 6,944τ) —
  **slippage negligible at current size**; pool-cap only bites as units scale.
- Discrete units = robust to noisy subnet transition matrices (no false
  precision), simple to automate, easy kill-switch/audit.
- Makes the shipped trim/Drift loop legible: e.g. Minos at 2.5 units (7.43τ/3),
  take-profit at +49% in Bear macro → "trim ~1 unit"; NIOME 1.5 units, bleeding
  bear → "cut to 0, units → SN0".

---

## What the next chat should deliver

1. Full allocation **design doc**: the two-axis model fully specified — macro→
   exposure-dial function, score→bucket thresholds, pool-cap formula, anti-churn
   rule, rebalance cadence, max-10 enforcement.
2. Engine layer spec: `compute_target_units(eligible_scored, macro, pool_caps,
   policy)` → replaces manual `TARGETS`, feeds dashboard Drift, becomes the input
   to the Phase-3 rebalancer. Slots in where `entry_score` is already
   macro-scaled.
3. Then implementation + wire into `gordie.html` Drift and the Telegram report.

## Note: current portfolio is itself partly relic

A real process in today's Bear macro would **cut**, not just reweight: NIOME,
Zipcode, MANTIS are bleeding bears already in REVIEW/EXIT. Expect the process to
reshape holdings, not only assign weights. Current holdings in units (τ/3):
Minos 2.5, iota 2.4, NIOME 1.5, Score 1.2, NOVA 1.1, Zipcode 1.0, Targon 0.9,
MANTIS 0.7, Root ~0 (dust/sink).

---

## Carried-over open items (from LIVE_STATE_10, still live)

1. TARGETS weights → **superseded by this allocation process** (don't hand-set;
   build the derived target-units instead).
2. Raise `--candidates` to 20–25 (safe post-#3 retry/backoff).
3. `TP_MIN_PROFIT_PCT` tuning (0.0 → ~0.10–0.15) if marginal trims clutter.
4. De-dup the stake-balance fetch (`main()` + `compute_holdings_pnl` both call
   `get_wallet_stakes`).
5. `_ema` SMA-seed only if a short-history holding shows `EMA:` pinned near −90%
   (none observed).
6. Optional: dashboard client-side `FILTERS` (min-pool 15,000τ) diverge from the
   engine pre-filters — cosmetic "7 need attention" noise.
