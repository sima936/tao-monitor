# TAO Monitor — LIVE_STATE / Session Handoff

**Updated:** 2026-06-11 · **Repo:** sima936/tao-monitor (default branch `main`)
**Railway project:** bountiful-celebration / production
**This session:** **allocator WIRED + calibrated.** Score → size is now live end-to-end (pending deploy).

---

## Architecture (current)

Unchanged services from LIVE_STATE_11. Three Railway services from `main`:

- **tao-monitor** / `serve.py` — dashboard (`gordie.html`), Basic Auth. In-memory bridges `POST /api/ingest-score`→`GET /api/score`, `POST /api/ingest-cost-basis`→`GET /api/cost-basis`.
- **alluring-smile** / `tao_bot_listener.py` — Telegram bot; `/status`,`/holdings` shell out to `run_scoring.py` (60s fast path, **no** `--cost-basis`). **Do not add latency here.**
- **spectacular-adaptation (SA)** / `run_scoring.py` — cron `0 11,23 * * *` UTC. Start Command unchanged:
  ```
  python3 run_scoring.py --no-concentration --force-send --holdings-gini --holdings-history --candidates 15 --cost-basis
  ```

**Engine:** v4 `subnet_scoring_engine.py` (untouched this session) + `subnet_allocation.py` — **now imported and wired** by `run_scoring.py`. Taostats free tier = 5 calls/min (~12.5s/call).

**Holdings (on-chain authoritative):** [0,4,9,44,46,55,68,107,123]. **Watchlist:** SN3 Teutonic.

---

## CLOSED this session — allocator wired (LIVE_STATE_11 open #1) + #5 de-dup + calibration

**3 files changed. Deploy via GitHub web editor (whole-file replace). Engine NOT touched.**

### 1. `run_scoring.py` — the wire (per Allocation_Design_v1 integration plan, all 4 steps)
- **Imports** `compute_target_allocation, AllocationPolicy, format_allocation_plan`.
- **`run()`**: after `run_scoring_cycle`, feeds **`result.ranked_by_health`** survivors + **`result.macro`** into `compute_target_allocation(...)`. On the cost-basis path passes `account_tao` + `current_weight_by_id` (→ Drift + per-name actions); on `/status` they're `None` → targets-only, no balance fetch.
- **Dashboard push**: score JSON now carries an **`allocation`** key (`plan.to_dict()`). The old early `push_score_to_dashboard(to_json(result))` moved below the alloc step; `--json` prints the same payload. Push is wrapped — falls back to score-only on any embed error.
- **Telegram**: `format_allocation_plan(plan)` appended to the **cron digest only** (`if cost_basis`). `/status` stays lean.
- **#5 de-dup CLOSED**: new `parse_stake_balances()` is the single rao→TAO parser. `main()` now resolves holdings via **one** `get_wallet_stakes()` and threads the balances (`prefetched_balances`) into `run()`, reused for **both** the P&L gate and the allocator. `compute_holdings_pnl(..., bal_by_netuid=...)` skips its own fetch when given balances. Net: **one** stake fetch per cron (was two). `fetch_wallet_holdings` no longer called by `main()` (still in taostats_fetch, unused).

### 2. `subnet_allocation.py` — calibration (open #2 resolved)
- **`health_b` 40 → 45.** Cuts marginal sideways names (iota/Score-class) harder. More Siam. One-line revert if it over-prunes.
- **Per-name cap added & generalised.** New `max_weight_per_name = 0.40` applies to **every tier** (old cap was A+-only). Rationale: `health_b=45` thins the book, which *amplifies* single-name concentration in a bull — verified a lone green would otherwise hit ~100%. Now any name caps at 40% of deployed; overflow parks in SN0 (conservative, on-model). A+ keeps its own (equal-or-tighter) cap via `min()`. `capped_by` shows `"name"` or `"aplus"`.

### 3. `gordie.html` — TARGETS killed, Drift derived
- Hand-set `const TARGETS` **deleted**. `renderPortfolio(...)` gains a `score` arg and builds targets from the `allocation` block: held greens → `target_weight × 100`; **cut names → 0%** (so over-allocated holdings flag red instead of `—`). Drift = `actualPct − targetPct`, null-safe (`—` when no score yet). `loadData` threads `fetchScore().catch(()=>null)` into the portfolio render (optional — old/missing score degrades gracefully).

---

## Verified this session (offline — no live API/dashboard access)
- Allocator vs the live book (LIVE_STATE_10 22:07 snapshot): **deep bear −0.89 → deploy 15%, ~86% to SN0, reds + weak health cut, greens trimmed small**; **strong bull +0.55 → deploy 100%, per-name cap holds the top name at 40%, no 100%-one-name**; thin-bull stress caps correctly.
- Integration test against **real** engine objects: `ranked_by_health` → allocator → `to_json` + `allocation` embed → JSON round-trips; `format_allocation_plan` renders; `parse_stake_balances` handles rao strings/ints/missing fields.
- `run_scoring.py` + `subnet_allocation.py` compile; full module graph imports; `gordie.html` inline JS parses; diffs reviewed clean (no stray edits).

**Not yet verified live** — couldn't reach `api.taostats.io` (not in bash allowlist) or the Basic-Auth dashboard. Verify on first SA cron **Run** (below).

---

## DEPLOY + VERIFY (do this to finish)
1. GitHub web editor → whole-file replace: **`run_scoring.py`**, **`subnet_allocation.py`**, **`gordie.html`** → commit. Railway auto-redeploys all 3.
2. Hit **SA cron → Run** (don't wait for 11:00/23:00). Watch Console for:
   - `Wallet holdings from chain: [...] (N subnets)` (single fetch).
   - `Allocation: deploy X% · N green · M cut · SN0 Y%`.
   - the `🧭 ALLOCATION PLAN` block in the Telegram digest.
3. Hard-refresh dashboard (Ctrl+Shift+R) → Portfolio **Target/Drift** columns now populate from the engine (cut holdings show 0% target / red drift).
4. Sanity vs current macro (Bear ≈ −0.89): expect deploy ~15%, NIOME/Zipcode (bear) + low-health names cut to SN0, Minos/iota/Score held small.

---

## OPEN ITEMS / NEXT (priority order)
1. **Remove the engine's `entry_score *= 0.3/0.5/0.7` macro switch** — Allocation_Design says the Axis-1 dial *replaces* it. **Deliberately left this session** (it's the load-bearing v4 scorer; deserves its own verified change). The dial now governs gross exposure regardless, so the switch is redundant double-suppression on the entry side. Do as a focused engine edit + cron verify. Does **not** touch `p2_macro` (stays a 15% scoring input).
2. **Calibration watch (live data):** is `health_b=45` cutting names you'd rather hold once macro turns? Is the 40% per-name cap parking too much to SN0 in thin bull? Both are one-line `AllocationPolicy` fields. Revisit after a few cron cycles of real target-vs-actual.
3. **Raise `--candidates`** 15 → 20–25 (safe post-LIVE_STATE_10 #3 retry/backoff); watch Console retry frequency + runtime.
4. **`TP_MIN_PROFIT_PCT`** tuning (0.0 → ~0.10–0.15) if marginal trims clutter.
5. **Phase 3 rebalancer** — the allocator is the input. `AllocationPlan` already emits per-name `action` (enter/add/hold/trim/exit) + drift deadband; pair with a cooldown before any automated execution.
6. **Dashboard `FILTERS` (min-pool 15,000τ)** still diverge from engine pre-filters — cosmetic noise, unchanged.

---

## Working-env notes (for next Claude session)
- **Start here:** `curl` live files from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before any edit. **Project-file copies in the Claude Project are stale.** Pull `run_scoring.py`, `subnet_allocation.py`, `subnet_scoring_engine.py`, `gordie.html`, `taostats_fetch.py`.
- `/api/score`, `/api/cost-basis`, dashboard are **Basic-Auth** — Claude can't fetch; paste JSON/screenshot.
- `api.taostats.io` **not** in Claude's bash allowlist; `raw.githubusercontent.com` **is**. Build against documented shapes (`balance_as_tao` is rao → /1e9), verify on first cron Run.
- New score-JSON contract: top-level **`allocation`** = `{macro_signal, macro_regime, deployed_fraction, sn0_target_weight, positions[], cut[], notes[]}`. `serve.py` stores the whole blob as-is, so no serve.py change was needed.

---

## Commits this session (suggested messages)
1. `feat(alloc): wire allocator into run_scoring — ranked_by_health → compute_target_allocation; embed allocation in score JSON; alloc block on cron digest`
2. `refactor(scoring): single get_wallet_stakes per cron — parse_stake_balances reused for P&L + allocation weights (#5 de-dup)`
3. `feat(alloc): calibrate — health_b 40→45; generalise A+ cap to 40% per-name (all tiers)`
4. `feat(dashboard): derive Target/Drift from allocation block; delete hand-set TARGETS`
