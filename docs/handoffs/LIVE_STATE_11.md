# TAO Monitor — LIVE_STATE / Session Handoff

**Updated:** 2026-06-11 · **Repo:** sima936/tao-monitor (default branch `main`)
**Railway project:** bountiful-celebration / production
**This session:** allocation layer designed, built, verified, committed. **Not yet wired.**

---

## Architecture (current)

Unchanged from LIVE_STATE_10. Three Railway services from `main`:

- **tao-monitor** / `serve.py` — always-on dashboard (`gordie.html`), Basic Auth. In-memory bridges: `POST /api/ingest-score`→`GET /api/score`, `POST /api/ingest-cost-basis`→`GET /api/cost-basis`. URL: https://tao-monitor-production.up.railway.app
- **alluring-smile** / `tao_bot_listener.py` — always-on Telegram bot; `/status`,`/holdings` shell out to `run_scoring.py` (60s fast path, **no** `--cost-basis`). **Do not add latency here.**
- **spectacular-adaptation (SA)** / `run_scoring.py` — cron `0 11,23 * * *` UTC. Start Command:
  ```
  python3 run_scoring.py --no-concentration --force-send --holdings-gini --holdings-history --candidates 15 --cost-basis
  ```

**Start commands live in Railway Settings, NOT the repo.** Apply repo changes via the **GitHub web editor** (whole-file replace, or Add file → Upload). Railway cron is ephemeral; Infinity8 holds the gini cache but is unreachable from Railway → falls back to in-process gini fetch.

**Holdings (on-chain authoritative):** [0,4,9,44,46,55,68,107,123] — SN0 Root, SN4 Targon, SN9 iota, SN44 Score, SN46 Zipcode, SN55 NIOME, SN68 NOVA, SN107 Minos, SN123 MANTIS. **Watchlist:** SN3 Teutonic.

**Engine:** v4 `subnet_scoring_engine.py`, 10-gate scorer → `entry_score` (macro-scaled) + `health_score`. **NEW (dormant):** `subnet_allocation.py` — the score→size bridge. TAO macro **Bear** (last cron signal ≈ −0.89) → entries suppressed. Taostats free tier = 5 calls/min (~12.5s/call).

---

## CLOSED this session

### ✅ Allocation layer built + committed (the missing score→size half)
Model locked with Simon — **"be Siam"**: hold only green, cut red fast; size survivors by conviction; percentages (3τ units dropped). Decisions = **1A** (cut worst) + **2B** (conviction tiers) + **3A** (percentages).

- **`subnet_allocation.py`** (committed to `main`, **not imported anywhere yet**):
  - **Axis 1 — dial:** `macro.signal` → % of account deployed vs parked SN0 (stepped: ≥+0.4→100%, +0.1→80%, −0.1→50%, −0.4→25%, deep bear→15%). **Replaces the v4 `entry_score×0.3` Bear switch** (does NOT touch `p2_macro`).
  - **Axis 2 — cross-section:** cut on `markov=Bear` OR `health < health_b`; tier survivors off `health_score` (A+≥70/A≥55/B≥40 → conviction 4/2/1); conviction-weight × deployed = % of account; caps (A+ ≤40% of deployed, pool ≤1% — pool cap inert at current size); ≤10 positions.
  - **Anti-churn:** exits act immediately; rebalances only when `|drift| > 3%` of account.
  - `compute_target_allocation(eligible_scored, macro, policy, *, account_tao, current_weight_by_id, sn0_id=0) -> AllocationPlan`. Duck-typed (reads `subnet_id, name, health_score, markov_regime, pool_depth`) — no import cycle. Targets need no holdings (`/status`-safe); pass `current_weight_by_id` for Drift + actions. `plan.to_dict()` → dashboard JSON; `format_allocation_plan()` → Telegram block.
  - **Verified** vs the live book: −0.89 → deploy 15%, cut ~46% to SN0 (NIOME/Zipcode bear, NOVA/Targon/MANTIS health), hold Minos/iota/Score small. Bull +0.55 → deploy 100%, reds still cut.
- **`Allocation_Design_v1.md`** (committed) — canonical spec; **supersedes `Allocation_Policy_Brief.md`**. Contains the integration plan + calibration knobs.
- **Commits:** `feat(alloc): add be-Siam allocation engine + design doc (not yet wired)`, `docs(alloc): add allocation design v1`.

---

## ACTIONABLE SNAPSHOT

No new scoring cycle was run this session (design/repo work only). Last known book = LIVE_STATE_10's 22:07 UTC snapshot, macro Bear: Minos [60] 🟢 take-profit candidate (+49% P&L); Score/Zipcode [40], NIOME [25] 🔴 REVIEW/EXIT; Targon/NOVA/MANTIS/iota sideways. Pull a fresh cron for current numbers.

---

## OPEN ITEMS / NEXT (priority order)

1. **WIRE the allocator (this session's whole point).** Per `Allocation_Design_v1.md` → Integration plan:
   - `run_scoring.py` `run()`: after `run_scoring_cycle`, feed the **`ranked_by_health` survivors** (not just holdings) into `compute_target_allocation(..., macro)`. On the cost-basis path pass `current_weight_by_id` derived from the `get_wallet_stakes` balances **already fetched** → **closes #5 de-dup in the same edit**.
   - Push `plan.to_dict()` under an `allocation` key in the score JSON (`/api/ingest-score`).
   - `gordie.html`: **delete `TARGETS`**; render Drift = `current_weight − target_weight` from the `allocation` block.
   - Telegram: append `format_allocation_plan(plan)` on the cron digest only (keep `/status` lean — no balance fetch there).
2. **Calibration calls to make while wiring:** raise `health_b` 40→45 to cut marginal sideways names (iota/Score)? Add a general per-name cap (A name hit ~50% in a thin bull book) vs A+-only?
3. **Raise `--candidates`** to 20–25 (safe post-LIVE_STATE_10 #3 retry/backoff); watch Console retry frequency + runtime.
4. **`TP_MIN_PROFIT_PCT`** tuning (0.0 → ~0.10–0.15) if marginal trims clutter.
5. **`_ema` SMA-seed** — only if a short-history holding shows `EMA:` pinned near −90% (none observed).
6. **Dashboard client-side `FILTERS`** (min-pool 15,000τ) diverge from engine pre-filters — cosmetic "needs attention" noise.

---

## Working-env notes (for next Claude session)

- **Start here:** `curl` live files from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before any edit. **Project-file copies in the Claude Project are stale** (engine copies are pre-v4; `Allocation_Policy_Brief.md` is superseded by `Allocation_Design_v1.md`). Pull `gordie.html`, `run_scoring.py`, `subnet_scoring_engine.py`, `subnet_allocation.py` to start the wiring.
- `/api/score`, `/api/cost-basis`, dashboard are **Basic-Auth** — Claude can't fetch; paste JSON/screenshot.
- `api.taostats.io` **not** in Claude's bash allowlist — can't test the API directly; build against documented shapes (`balance_as_tao` is rao → /1e9), verify on first cron run.
- `raw.githubusercontent.com` **is** allowed.
- Deploy flow: GitHub web editor whole-file replace (or Add file → Upload) → Railway auto-redeploys all 3 → hit SA cron **Run** to refresh `/api/score` + `/api/cost-basis` → hard-refresh dashboard (Ctrl+Shift+R).

---

## Commits this session (chronological)
1. `feat(alloc): add be-Siam allocation engine + design doc (not yet wired)`
2. `docs(alloc): add allocation design v1`
