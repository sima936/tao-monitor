# LIVE_STATE_19 — TAO Monitor

**Session:** 2026-06-15 · **Repo:** sima936/tao-monitor `main` · **Cron:** spectacular-adaptation (`0 11,23 * * *`, 11:00/23:00 UTC) · **Clean post-deploy run initiated at wrap — verify on next look.**

---

## Net of this session

Built and deployed the **take-profit / cut-loss stop layer** (steps 1 + 2 of `STRATEGY_take_profit_cut_loss.md`), then hardened the wallet-read failure mode that surfaced during deploy. Four commits to `main` (GitHub web; each redeploys all three services):

| # | Commit | File | Effect |
|---|--------|------|--------|
| 1 | `Create tp_cl_stops.py` | tp_cl_stops.py (new, 191 ln) | Stop module — peak tracking, trailing + hard stop, outcome-log writer, 🚨 alert. Inert until imported. |
| 2 | `Add force_exit stop override to allocator` | subnet_allocation.py (516 ln) | `force_exit` param — same-cycle full exit, overrides conviction floor + 18h gate. Backward-compatible (optional param). |
| 3 | `Update run_scoring.py — wire TP/CL stop layer into cron` | run_scoring.py (1169 ln) | Stop eval BEFORE allocation; `force_exit` threaded in; `peak_price`/`stop_fired` persisted; 🚨 alert independent of digest. |
| 4 | `fix(scoring): wallet-read soft-fail` | run_scoring.py (1194 ln) | Failed/empty wallet read → hold last state + calm note, instead of stale-holdings fallback. |

**Env vars set on SA:** `TRAIL_PCT=0.25`, `STOP_PCT=0.30`, `OUTCOME_LOG_PATH=/data/outcome_log.csv` (volume-backed, matches `SCORE_LOG_PATH`/`STATE_FILE` prefix).

**Deploy order used (no crash window):** allocator (param) → env vars → run_scoring (caller). Stand-alone soft-fail commit last.

---

## The stop layer — what it does

Advisory only. No signing keys, no auto-unstake. Detects → 🚨 pings → logs; forces the exit in the *target book* but you execute on-chain manually.

- **Peak tracking on `token_price`** (= peak value *per unit*, invariant to adds/trims — not raw TAO position value, which a trim would false-trigger).
- **Trailing stop:** `price < peak × (1 − TRAIL_PCT)` → exit. Harvests winners, cuts losers near entry. Same rule, both jobs.
- **Hard stop:** `pnl < −STOP_PCT` vs cost basis → exit (gap-down backstop). Uses the cron's existing P&L.
- Both **bypass the 18h `cut_since` gate** (same-cycle) and the **conviction floor** (fixes the MANTIS "floored at −30%" trap). Conviction name hit by a stop emits a "thesis check required" note, not a silent hold.
- **De-dup latch** (`stop_fired`): one alert per standing breach, clears on recovery/exit. State pruned to current holdings.
- **Outcome log** (`/data/outcome_log.csv`): one row per `TRAIL_STOP|HARD_STOP|REGIME_EXIT|TP_TRIM|ENTRY` event; `fwd_return_1d/7d/14d` backfilled later. **This is the Hermes feed.**
- Gated on `cost_basis` (cron path only — needs P&L/balances). Tested locally: 5-cycle stop test + allocator-override test both pass.

**Two limitations banked:** (a) trailing stop is **not retroactive** — peaks seed at deploy-time price, so profit-protection builds over a cycle or two; **hard stop is live from cycle one**. (b) iota's +25% take-profit was a manual call (no peak history yet).

---

## Key findings this session

1. **Orphaned Hermes feed.** `outcome_tracker.py` is **404 on main** — the May `trades.jsonl`/`snapshots.jsonl` pipeline (wired into legacy `tao_gordie.py`, which is not the live cron) is dead. **`/data/outcome_log.csv` (written by `run_scoring.py`) replaces it as the Hermes feed.**
2. **The degraded 03:05 run** (NIOME resurrected, no τ, no P&L) had a single root cause: the on-chain wallet read returned **empty** → fell back to stale `CURRENT_HOLDINGS = [0,4,9,44,46,55,68,107,123]` (still lists 55/NIOME, which Simon unstaked) and `account_tao=None`. Fixed by commit #4 — a failed/empty read now skips the cycle, never computes on a phantom book.
3. **SA console is empty by design** — it's a cron service; the container only exists during a run. SA volume files (`scoring_state.json`, `outcome_log.csv`) aren't readable from the console between runs, and the volume is SA-only (persistent services can't see it). To inspect: run logs, dashboard payload, or a small read path.
4. **Wallet ≠ dashboard total** is structural, not a bug in the stops: `account_tao` sums **staked** TAO only (alpha + SN0 root), excluding **free/unstaked** TAO. Stops are per-position so unaffected; only the headline total understates. Fix = direct-chain wallet read (below).

---

## Service ↔ file map (unchanged)

| Railway service | Runs | State |
|---|---|---|
| **spectacular-adaptation** | `run_scoring.py` (cron) | `/data` volume: `scoring_state.json`, `score_log.csv`, **`outcome_log.csv`** (new), `gini_cache.json` |
| **tao-monitor** | `serve.py` (dashboard) | Online; own volume |
| **alluring-smile** | `tao_bot_listener.py` (bot) | Online; own volume |

---

## Top of queue — next session

1. **Verify the clean post-deploy run.** Confirm: real book (NIOME gone), τ/drift/P&L restored, no crash. If taostats blipped, expect the calm "wallet read unavailable" note (correct behaviour now). Confirm `peak_price`/`stop_fired` landed in `scoring_state.json` — SA console can't show it, so add a one-line `logger.info` in the stop block (prints to the run's Deployments log) if direct confirmation is wanted.
2. **Direct-chain wallet read (the big one).** Query the coldkey for **staked + free** balance via Bittensor SDK / raw RPC. Two wins: (a) drops the flaky taostats stake endpoint that caused today's failure; (b) captures free unstaked TAO so dashboard total = wallet total. SDK is heavy (~500MB) → route via Infinity8 or a light raw-RPC call rather than installing on the Railway cron. **This is the fix for the wallet-vs-dashboard discrepancy + the cash-sink modelling question.**
3. **Hermes re-point.** Add a `risk_management:` block (`trail_pct`, `stop_pct`, `scale_out_levels`, optionally `confirm_hours`) to `strategy.yaml`; point Hermes's config from the dead Infinity8 `trades.jsonl` to `/data/outcome_log.csv`. Needs ~2–4 weeks of stop events first. Discipline carries over: one var/cycle, Cornelius Mon / Hermes Thu, `read_only → live`.
4. **Threshold calibration.** `TRAIL_PCT`/`STOP_PCT` are placeholders (0.25/0.30, Simon's risk call). Hermes-tunable later: log → perturbation-stability → forward-IC (~2–4 wks) → narrow calibration → ~60 days before trusting. Never a joint fit.
5. **Strategy step 4 — 6h cron** (`0 */6 * * *`): only *after* stops are proven.
6. **Strategy step 7 — execution automation** (the Gordie-style auto-rotate end goal): last, behind the guardrail list — dedicated hot coldkey, limited funds, size/rate caps, kill switch, dry-run. Earns the keys via a proven advisory track record; a genuine security decision Simon owns.

---

## Backlog (carried + new)

- **NEW — `CURRENT_HOLDINGS` now effectively dead** as a fallback (the skip path supersedes it). Decide: remove, or keep as a documented manual-override-only constant.
- **NEW — confirm the wallet-read failure mode** from the 03:05 run logs: was it `get_wallet_stakes` specifically or the cost-basis fetch?
- **NEW — two health metrics diverge** (saw it live 6/14: Zipcode 59 vs 40 across the two report sections). Ties to OPEN #3 `health_b` calibration; matters because health drives sizing.
- **OPEN #2** — pool-floor display divergence: dashboard `min_pool_depth` (15000) ≠ engine `MIN_POOL_DEPTH` (5000) — align.
- **OPEN #3** — `health_b` calibration; `score_log.csv` accumulating on `/data`.
- **OPEN #4** — Gini 429 (parked; 48h cache + SDK→RPC→taostats fallback absorbs it).
- **OPEN #5 mappings** — Hippias≈Hippius (confirm SN#); **Lead Power** unresolved (needs interview audio).
- **/api/score persistence** — redeploys blank the dashboard score panels until next cron POST; persist to `/data`.
- **price_cache.py audit** — `status()` still references a stale holdings list; confirm if wired into the live cron.
- **cash-sink modelling** — root-stake vs free unstaked TAO; now folded into next-session item #2 (direct-chain read).
- **Unreviewed repo files** — `patch_scoring_engine.py`, `session_patches.py`, `tao_enhanced_monitor.py`, `debug_api.py`.

---

## Working-environment notes (carry forward)

- **Project file copies are stale** — always pull live from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before editing.
- **bash allowlist:** GitHub raw + api **yes**; `api.taostats.io` / `api.geckoterminal.com` / `api.coingecko.com` **no** — can't test data APIs from the sandbox; verify on deploy / via dashboard proxies / Railway logs.
- **Deploy** = GitHub web whole-file replace (or surgical block). A push redeploys **all three** services. When a commit's caller depends on a callee's new signature, **commit the callee first** (e.g. allocator before run_scoring) to avoid a mid-deploy crash window.
- **SA is a cron service** — ephemeral container, console empty between runs; inspect its `/data` via run logs or a surfaced payload, not the console.
- **Key constants:** `TRAIL_PCT=0.25`, `STOP_PCT=0.30`, `OUTCOME_LOG_PATH=/data/outcome_log.csv`, `MAX_TOKEN_PRICE=0.15`, `MIN_POOL_DEPTH=5000`, `MAX_POOL_DEPTH=500000`, `confirm_hours=18`, `conviction_tags={4,107,46,44,68,123}`.
- **Stops are advisory** — detect/alert/log only; no keys in the stack. Execution automation is gated behind proof + guardrails.
