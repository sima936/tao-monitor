# LIVE_STATE_20 — TAO Monitor

**Session:** 2026-06-15 (afternoon) · **Repo:** sima936/tao-monitor `main` · **Cron:** spectacular-adaptation (`0 11,23 * * *`, 11:00/23:00 UTC) · **Confirmed live in the 15:42 report.**

---

## Net of this session

Killed the macro cash dial (a relic), fixed a stop-latch bug that was silently re-floating cut names, added a per-cycle stop-state log, and staked the previously-invisible free TAO into SN0. Three commits to `main` + one on-chain action.

| # | Commit | File | Effect |
|---|--------|------|--------|
| 1 | `chore(scoring): log TP/CL peak+latch state every cycle (LS19 #1 confirm)` | run_scoring.py | Additive `logger.info` every cycle → SA Deployments log shows `peaks=N latched=M` + per-name price/peak/FIRED. Confirms `peak_price`/`stop_fired` persist. |
| 2 | `fix(scoring): persist force_exit for standing stop breaches — latch must de-dup the alert, not the exit` | run_scoring.py | `force_exit` now seeded from `fired_out` (all standing breaches), not just new `stop_events`. Fixes MANTIS re-floating to its CV floor one cycle after a hard_stop. |
| 3 | `fix(alloc): remove macro cash dial — SN0 is residual, not a target` | subnet_allocation.py | `deploy_bands` → single `(-9.99, 1.00)`; `unknown_macro_fraction` 0.25→1.00. Always fully deployed; SN0 holds only cut-overflow. |

**On-chain:** staked **3.685τ free TAO → SN0 root via Datura** (block #8413223, fee 0.0019τ, 7.35% APY). Free-TAO gap closed: `account_tao` now **34.84τ ≈ wallet total**.

---

## Key findings / decisions

1. **The 50%-SN0-in-Sideways was the Axis-1 Markov macro dial (Roan/@RohOnChain lineage), NOT Siam.** Decision: **SN0 is a residual parking spot, not a fixed target** — dial removed. Do not reintroduce a macro cash %; it's a relic of the pre-Siam Markov hedge-fund framing.
2. **Stop-latch bug (fixed).** The de-dup latch was suppressing the *force_exit*, not just the *alert*, so a hard-stopped name (MANTIS) silently returned to its conviction floor the next cycle — breaking "exited, not silently held". Now durable: a standing breach keeps forcing the exit until executed/recovered, while alert + outcome-log stay de-duped (still one event row per breach — correct for the Hermes feed).
3. **Datura = SN0 validator** (7.35% APY, 59.5K τ, 6.7K stakers; runs TaoMarketCap). Root rewards in TAO, no unbonding (liquid). **The "Kraken hotkey" config note is STALE** — actual root delegate is Datura.
4. **15:42 verification:** dial gone (`deploy 100% · SN0 0%`), MANTIS shows `CUT TO SN0 (hard_stop)` ✓, SN0 Root 3.69τ visible ✓.

---

## Top of queue — next session

1. **NEW — SN0-as-residual still triggers ADD (decision needed).** With `deploy=100%` always + only 2 healthy A-tier names, the green book sizes Minos/iota to 33% each and tries to pull parked SN0 cash into them → 15:42 flagged **ADD to Minos *and* take-profit simultaneously** (contradiction). If parked cash should *stay* parked, need a **per-name target cap or a deploy ceiling** so SN0 holds genuine residual without nagging ADD. **Do not act on ADD signals meanwhile.**
2. **LS19 #1 — confirm the logging.** Commit #1 is in; still need eyes on the **SA Deployments log** to see `peaks=N`/`FIRED` (SA console empty between runs). Pending.
3. **LS19 #2 — direct-chain wallet read** (staked + free). Partially mooted for the staked portion (free TAO now in SN0), but *future* free TAO stays invisible and the flaky taostats stake endpoint is still in the path. Keep.
4. **parse_stake_balances overwrite bug.** Keys by netuid with `=` not `+=` (run_scoring.py ~L318) → root split across 2 hotkeys would under-count. Harmless now (single Datura delegate). Fix to `+=` when next touching wallet read.
5. **LS19 #3 — Hermes re-point** to `/data/outcome_log.csv`. MANTIS HARD_STOP is **event #1**; needs ~2–4 wks of events before calibration.

## Execution to-do (Simon — manual/advisory)

- **MANTIS (SN123)** → unstake to SN0 (hard_stop, ~2.23τ). Finish the cut.
- **Minos (SN107)** → **partial** take-profit at the +71% high; do **NOT** act on the ADD flag (parked-cash redeploy artifact).
- **Zipcode (SN46)** → bear_regime, 12h/18h → confirms ~23:00; move to SN0 then (or pre-empt).
- Do **not** add to Minos/iota despite the ADD flags.

## Backlog (carried)

- **LS19 #4 — wallet-vs-dashboard:** RESOLVED for now (free TAO staked → `account_tao` ≈ wallet). Structural fix still via #2.
- **LS19 #5/#6** — 6h cron / execution automation: later, gated behind proof + guardrails.
- **OPEN #2** — pool-floor display divergence: dashboard `min_pool_depth` (15000) ≠ engine `MIN_POOL_DEPTH` (5000).
- **OPEN #3** — `health_b` calibration; two health metrics diverge (6/14: Zipcode 59 vs 40); `score_log.csv` accumulating.
- **OPEN #5 mappings** — Hippias=Hippius **SN75 confirmed** (live data); **Lead Power** still unresolved (needs interview audio).
- **Unreviewed repo files** — `patch_scoring_engine.py`, `session_patches.py`, `tao_enhanced_monitor.py`, `debug_api.py`.

## Working-environment notes (carry forward)

- **Project file copies are stale** — pull live from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before editing.
- **bash allowlist:** GitHub raw + api **yes**; `api.taostats.io` / `api.geckoterminal.com` / `api.coingecko.com` **no** — verify data APIs on deploy / dashboard / Railway logs.
- **Deploy** = GitHub web whole-file replace (or surgical block). A push redeploys **all three** services. Commit callee before caller on signature changes.
- **SA is a cron service** — ephemeral container, console empty between runs.
- **Key constants:** `TRAIL_PCT=0.25`, `STOP_PCT=0.30`, `OUTCOME_LOG_PATH=/data/outcome_log.csv`, `MAX_TOKEN_PRICE=0.15`, `MIN_POOL_DEPTH=5000`, `MAX_POOL_DEPTH=500000`, `confirm_hours=18`, `conviction_tags={4,107,46,44,68,123}`. **deploy_bands now single band `(-9.99, 1.00)` — fully deployed, no cash dial.**
- **SN0 validator:** Datura (root, 7.35% APY). Stale "Kraken" note retired.
- **Stops are advisory** — detect/alert/log only; no keys in the stack.
