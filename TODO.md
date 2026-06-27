TAO Monitor — TODO
Running backlog. Add items as they come up; move to Done when shipped.
Convention: `[ ]` open · `[x]` done · date-stamp on add and on close.
Priority order within each section. LIVE_STATE handoffs are routine and separate from this file.
Soon (do next)
Cleanup (cosmetic, low risk)
[ ] Remove Fear & Greed tile from `gordie.html` — taostats-only, now permanently empty ("—"). LS31 already decided to drop it. (added 2026-06-27)
[ ] Strip residual `[probe]` stderr prints in `run_scoring.py` — 4 left after the overlay removal (Pools snapshot / PUSHED / push-EXCEPTION). Pure noise, no taostats. (added 2026-06-27)
[ ] Infinity8 stale checkout — `~/tao-monitor` is at `2bb1fc8`, missing `chain_fetch.py`/`snapshot_history.py`, bittensor 10.2.0. Confirm it runs nothing scheduled (the real cron is the Railway `spectacular-adaptation` service). If inert, leave/decommission; if it has any cron, it'll keep hitting the old wall and confuse future debugging. (added 2026-06-27)
Data / features
[ ] Cost basis → chain (delegation-event history) — the durable fix for the blank P&L tile and the missing orange entry/cost line on the allocation chart. Both are the last taostats dependency; reconstructing entry/cost from on-chain `add_stake` events (valued at block) makes them free + permanent. Until then they return on the monthly taostats credit reset (check dash.taostats.io billing for the date). Real work, not a quick edit. (carried LS30/LS31)
[ ] `/brief <netuid>` Telegram command — spec done (`STRATEGY_telegram_brief.md`), not built. Reuses the chain connection. (carried LS30/LS31)
[ ] Shortlist screen — cheap structural-gate filter → research shortlist. Labelled filter, not picks. Spec + build pending. (carried LS30)
[ ] `FUNDAMENTAL_METRICS.md` §5 reconcile to `fundamentals.json` (SN9=IOTA/Macrocosmos, SN44 real customer, etc.). Also commit `FUNDAMENTAL_METRICS.md` if not yet in repo. (carried LS30)
Strategy (deferred — `STRATEGY_take_profit_cut_loss.md`)
[ ] Take-profit / cut-loss machine — build order: forward-outcome logging (day one) → perturbation-stability → ~2–4wk IC validation → Hermes calibrates `TRAIL_PCT`/`STOP_PCT`/scale-out → (only then) execution automation with guardrails. Do not shortcut the log-first / no-joint-fit protocol.
[ ] `TRAIL_PCT=0.25` / `confirm_hours=18` rework — still unprincipled placeholders (smoothed peak + confirmation; bear-exit on fundamental verdict). Stops now fail safe (skip when blind) but the logic needs grounding. (carried LS30)
Minor
[ ] Consolidate chain connections — 3 per cron (stakes / metrics / free balance) → 1. (carried LS30)
Done
[x] Chain-upgrade guard — distinct `StorageFunctionNotFound` alert (2026-06-27). `chain_fetch.py` records a `LAST_FAILURE` reason flag (`classify_chain_error`: storage_mismatch / unreachable / sdk_missing / other / ok) without changing the None/{}/{..} contract. `run_scoring.py` branches the wallet-read fallback on it: storage_mismatch → 🔧 (precedence over 🟠), credit wall → 🟠, blip → 🟡 — and also fires 🔧 (fallback wording) when taostats rescues the cycle, so a runtime upgrade is flagged before credits drain. Classifier + branch precedence unit-tested offline (chain unreachable from sandbox); deployed, two clean digests confirm no happy-path regression.
[x] Digest clock → local time (2026-06-27). `run_scoring.py` was slicing `result.timestamp[11:16]` raw UTC → digest showed 13:29 while Telegram showed 14:29 (BST). Added `_local_hhmm()` (Europe/London, BST/GMT aware, UTC-labelled fallback if tzdata missing); pinned `tzdata` in `requirements.txt`.
[x] Bump bittensor 10.4.1 → 10.5.0 — finney runtime upgrade removed `Swap.AlphaSqrtPrice`; pinned SDK queried a dead storage key → chain read failed → taostats fallback → credit-walled. Verified 10.5.0 reads finney clean, deployed via `requirements.txt` floor bump. (2026-06-27)
[x] Remove dead `fetch_pool_overlay` taostats call — store-only momentum; cron now makes zero taostats calls (cost-basis aside). (2026-06-27)
