TAO Monitor — Strategy Spec: Take-Profit / Cut-Loss Build
Status: approved for advisory build (2026-06-14). Automation deferred until Hermes-calibrated and proven.
Owner split: Claude builds advisory machine + outcome logging · Simon risk-assesses advisory + cuts NIOME · Hermes calibrates thresholds before automation.
---
Objective
Compound the TAO stack (more TAO than we started with) by holding alpha that beats TAO and sitting in cash (root/SN0) when nothing does. The engine is asymmetry: keep every loss small so no single position digs a hole; let winners run / harvest them so a few cover many small losses. "Take profits, cut losses" is the operational form of that.
The current system is a conviction/regime hybrid — good analysis, but it force-exits only on confirmed bear regime (18h gate) + manual execution, with no hard stop. That let NIOME ride to −41%. This spec converts it into a coherent take-profit/cut-loss machine.
---
The five components
1. Trailing stop — core rule (does double duty)
Each position tracks its peak value since entry; if it falls more than `TRAIL_PCT` off that peak → exit, regardless of regime. Losers hit it near entry (small loss); winners hit it only after running (keep most of the move). Same rule cuts losses and takes profits.
Lives in: `subnet_allocation.py` (pre-allocation override) + per-netuid `peak_value` persisted in state alongside `cut_since`.
Must: bypass the 18h `cut_since` confirmation — a stop is a same-cycle hard exit.
2. Hard stop from entry (backstop)
If a position is down more than `STOP_PCT` vs cost basis → exit, even if the trail hasn't triggered (gap-downs). Cost-basis is already computed on the cron, so this is available.
3. Conviction floors capped by the stop
Conviction tags keep buying patience through a soft patch only. Both stops override conviction floors — a conviction name past `STOP_PCT` still exits. Fixes the MANTIS-style "floored at −30%" trap. On trigger, emit "conviction name hit its stop — thesis check required" rather than silently holding.
4. Cadence → 6h
`0 11,23 * * *` → `0 */6 * * *` (4 real-data reads/day). Faster checking ≠ more trading — with trailing stops we only act when one fires. The 18h regime gate stays for slow regime trims; the stops handle fast exits. Apply only after the stop logic exists.
5. Execution — staged, key-risk honest
Auto-execution needs wallet signing keys in the stack (today everything is read-only/safe). Stage it:
(a) Instant stop alert (build now): dedicated 🚨 Telegram ping the moment a stop fires, separate from the digest, with the exact unstake. Zero key risk; kills the lag that let NIOME bleed.
(b) Auto-execution (later, only if proven): dedicated hot coldkey, limited funds, size/rate caps, kill switch, dry-run first. Not before Hermes calibration + a proven advisory track record.
---
Hermes-tunable vs fixed (the overfitting guard)
Fixed structure (Claude builds, not for Hermes to fit): the existence of trailing + hard stops, the conviction-cap override, the alert mechanism, the log schema.
Hermes-tunable (narrow surface, calibrate later):
`TRAIL_PCT` — trailing-stop distance off peak
`STOP_PCT` — hard stop off cost basis
scale-out ladder levels (if adopted: e.g. trim 25% at +25%, +50%)
optionally `confirm_hours` for the regime gate
Protocol — consistent with prior sessions, do NOT shortcut:
Log first. Nothing to optimise until forward-outcome data exists (see schema below).
Perturbation-stability before any optimum. Jitter each threshold ±X, re-run, measure book/exit churn. If exits barely move, precision is second-order — don't over-tune.
Forward-predictive scoring after ~2–4 weeks (information coefficient on logged events). Reweight by IC × IC-stability, not intuition.
Then let Hermes calibrate the narrow params only — never a joint fit of everything. ~60 days of live data before trusting it. Optionally run 2–3 pre-registered threshold configs as shadow books and let forward data adjudicate.
---
Outcome-log schema (build emits this from day one)
Generalises the existing `score_log.csv`. One row per stop/TP/exit event (plus the per-cycle per-subnet snapshot already logged):
```
event_ts, netuid, name, event_type {TRAIL_STOP|HARD_STOP|REGIME_EXIT|TP_TRIM|ENTRY},
entry_cost_tao, peak_value_tao, exit_value_tao, pnl_pct,
regime_at_event, health_at_event, trail_pct_used, stop_pct_used,
fwd_return_1d, fwd_return_7d, fwd_return_14d   # backfilled later
```
This is the dataset Hermes consumes via its `goal.yaml` / `strategy.yaml`. Without it, the advisory period is wasted as far as optimisation goes — so logging is part of step 1, not an afterthought.
---
Build order
Trailing + hard stop calc in `run_scoring`, persist `peak_value`, emit the instant 🚨 stop alert (advisory), and write the outcome-log rows. ← highest leverage, lowest risk, no keys. Fixes the NIOME-lag immediately and starts Hermes's dataset.
Wire stops into allocation targets (override regime gate + conviction floor on exits).
Conviction-floor cap (component 3).
6h cron.
Accumulate forward data → perturbation-stability + IC validation.
Hermes calibrates `TRAIL_PCT` / `STOP_PCT` / scale-out on the accumulated data.
(Only then) execution automation, with guardrails.
---
Decisions Simon owns
Placeholder thresholds to start (pre-Hermes) — risk call. Too tight = whipsaw on volatile alpha; too loose = NIOME.
Whether to keep the macro gate or let stops + momentum run regardless of TAO regime.
How far down the automation path to go (a genuine security decision, not just a feature).
Whether to adopt the scale-out ladder (component 1 optional) or run pure trailing stop.
---
Service / file touch-map (for the build)
`run_scoring.py` (spectacular-adaptation) — stop calc, peak tracking, outcome log, alert trigger.
`subnet_allocation.py` — stop override in `compute_target_allocation`, conviction cap.
`tao_bot_listener.py` (alluring-smile) — 🚨 instant stop alert path.
state file (SA volume) — add per-netuid `peak_value`.
cron schedule (SA settings) — 6h, step 4.
(pull live from `raw.githubusercontent.com/sima936/tao-monitor/main` before editing — project copies are stale.)
