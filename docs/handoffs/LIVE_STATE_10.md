# TAO Monitor — LIVE_STATE / Session Handoff

**Updated:** 2026-06-10 22:30 UTC
**Repo:** sima936/tao-monitor (default branch `main`)
**Railway project:** bountiful-celebration / production

---

## Architecture (current)

Three Railway services, all from `main`:

- **tao-monitor** / `serve.py` — always-on dashboard (`gordie.html`), Basic Auth. In-memory bridges: `POST /api/ingest-score`→`GET /api/score`, `POST /api/ingest-cost-basis`→`GET /api/cost-basis`. Proxies `/api/price`, `/api/gordie/pools`, `/api/portfolio/stakes`, `/api/vtrust`, `/api/yield`. URL: https://tao-monitor-production.up.railway.app
- **alluring-smile** / `tao_bot_listener.py` — always-on Telegram bot; `/status`,`/holdings` shell out to `run_scoring.py` (60s fast path, **no** `--cost-basis`). **Do not add latency here.**
- **spectacular-adaptation (SA)** / `run_scoring.py` — cron `0 11,23 * * *` UTC. Posts 12h holdings report to Telegram + pushes v4 score AND cost-basis to dashboard. Custom Start Command:
  ```
  python3 run_scoring.py --no-concentration --force-send --holdings-gini --holdings-history --candidates 15 --cost-basis
  ```

**Start commands live in Railway service Settings, NOT the repo.** Apply repo changes via the **GitHub web editor** (whole-file replace). Railway cron is ephemeral (no disk); Infinity8 holds the gini cache but is unreachable from Railway → Railway falls back to in-process gini fetch.

**Holdings (on-chain authoritative):** [0,4,9,44,46,55,68,107,123] — SN0 Root, SN4 Targon, SN9 iota, SN44 Score, SN46 Zipcode, SN55 NIOME, SN68 NOVA, SN107 Minos, SN123 MANTIS. **Watchlist:** SN3 Teutonic. (As of this session, `gordie.html` HOLDINGS/WATCHLIST are **now synced** to this set — see CLOSED #4.)

**Engine:** v4 `subnet_scoring_engine.py`, 10-gate scorer → `entry_score` (macro-scaled) + `health_score`. TAO macro currently **Bear** (signal −0.89) → entries suppressed. Taostats **free tier = 5 calls/min (~12.5s/call)**.

---

## CLOSED this session (LIVE_STATE_9 open items #1, #2, #3 + #4)

### ✅ #1 — Real-P&L gate on trim / take-profit (SHIPPED + verified live)
Trim/take-profit alerts now fire on **actual profit vs net-invested**, not the (broken) EMA — so winners at a high surface and underwater names never read "trim into strength".
- **`subnet_scoring_engine.py`** — `format_telegram_alert(..., pnl_by_netuid=None)`. New `TP_MIN_PROFIT_PCT = 0.0`. When P&L known (cron) → gate on real profit, ignore launch-anchored EMA; when unknown (`/status`) → original EMA gate, behaviour unchanged.
- **`run_scoring.py`** — `compute_holdings_pnl(client, cb, holdings)` → `{netuid: pnl_fraction}` from `(balance_as_tao/1e9 − tao_invested)/tao_invested`; skips house-money (net_invested ≤ 0, e.g. SN0). Threaded into the formatter on the `--cost-basis` path. Adds **one** `get_wallet_stakes` call to the cron.
- **Units bug fixed:** `balance_as_tao` is an integer **rao** string → must `/1e9` (matches `gordie.html` parse). First run showed +1.49e12% before this; after fix **Minos = +49% P&L** (cross-checks the dashboard's +48.8%).
- **Verified (22:07 cron):** `🔻 TRIM / TAKE PROFIT — SN107 Minos — +49% P&L, +41% over EMA, take profit`. NIOME (−17%) / Zipcode (−12%) correctly suppressed from trim, routed to REVIEW/EXIT.

### ✅ #2 — EMA period 72 → 24 + pct_from_ema surfaced (SHIPPED + verified live)
`EMA_PERIOD = 24` (was 72). Root cause: 72 was Siam's "72 EMA on **1H**" applied to a **daily** series, so the `_ema` launch-price seed never decayed → `pct_from_ema` pinned at −0.94…−0.998. At 24 the seed washes out in ~35 bars.
- Holdings line now shows **`EMA:±X%`** (`pct_from_ema`) for verification + ongoing use.
- **Verified (22:07):** Minos **+41%** over EMA (bounced), bleeders −16/−18%, sideways cluster −2…−8%. Nothing pinned near −90%.
- **Side benefit:** p3 (EMA slope) + p7 (displacement) were feeding off the broken EMA and suppressing strong names. Minos health jumped **41 → 60**; all holdings' healths now trustworthy.

### ✅ #3 — Taostats transient-failure resilience (SHIPPED)
`TaostatsClient.get` now retries **transient** failures only (read/connect timeout, 429, 5xx); 4xx fail fast. New ctor knobs: `max_retries=1`, `backoff_base=3.0`, `timeout=(8, 25)`. 429 backs off ≥ rate-limit delay and honours `Retry-After`. **Zero added latency on the happy path** — retries fire only on failure, so the 60s `/status` budget is safe (worst-case single-call fail ≈ 53s).
- Verification is passive: on a real blip the **Console** logs `... retry in Xs` and the cron recovers instead of aborting.
- **Unblocks** raising `--candidates` past 15.

### ✅ #4 — Dashboard constant sync + SN0 candidate slot (SHIPPED)
- **`gordie.html`** — HOLDINGS synced to {0,4,9,44,46,55,68,107,123} with correct names; WATCHLIST → {3: Teutonic} (old list wrongly contained 46, now a holding, and mislabelled SN3 as Templar); TARGETS pruned of stale non-held 5/32/75. **`run_scoring.py`** CURRENT_HOLDINGS/WATCHLIST were already correct.
- **`run_scoring.py`** — `select_candidates` now skips SN0 in the forced set, so SN0 no longer eats an enrichment budget slot (it's price-filtered + skipped downstream anyway) → +1 real mover enriched per cycle.

---

## ACTIONABLE SNAPSHOT (22:07 UTC, macro Bear → entries blocked)

Holdings by health (EMA = price vs 24-day EMA):
- **SN107 Minos [60]** 🟢 Bull · +68% 7d · **EMA +41% · +49% P&L** → live take-profit candidate (real profit at a high).
- **SN44 Score [40]** ⚪ · −7% 7d · EMA −16% · marginal.
- **SN46 Zipcode [40]** 🔴 Bear · −12% 7d · EMA −8% → REVIEW/EXIT.
- **SN55 NIOME [25]** 🔴 Bear · −17% 7d · EMA −18% → weakest, REVIEW/EXIT.
- SN4 Targon [35], SN68 NOVA [36], SN123 MANTIS [39], SN9 iota [41] — sideways, EMA −2…−8%.
- **SN0 Root [--]** ⛔ fail_price_too_high (dust / house money).

---

## OPEN ITEMS / NEXT (priority order)

1. **TARGETS weights for SN46 (Zipcode) + SN107 (Minos)** in `gordie.html` — currently placeholdered `0`. Held split is `9:16, 68:14, 44:12, 55:11, 4:11, 123:9` (=73%); SN0=0 (dust). Set the intended % for the two new holdings (and any rebalance) — one-line edit.
2. **Raise `--candidates`** (now safe post-#3). Try 20–25 in SA's start command; watch Console for retry frequency + total runtime before going higher. Also relevant: cost-basis page cap (25) untouched.
3. **`TP_MIN_PROFIT_PCT` tuning** — currently `0.0` (any real profit + a take-profit flag). If marginal names (Score-style) clutter the trim list, raise to ~0.10–0.15. Moot at 22:07 (only Minos surfaced).
4. **`_ema` SMA-seed** — only if a short-history (<~30 bar) holding shows `EMA:` pinned near −90%. None observed at 22:07, so deferred; watch the new `EMA:` column.
5. **De-dup the stake-balance fetch** — `main()` resolves holdings via `get_wallet_stakes` and discards balances; `compute_holdings_pnl` re-fetches the same endpoint. Thread the stakes through to save one cron call (pairs well with #2 above before raising candidates).

---

## Working-env notes (for next Claude session)
- `/api/score`, `/api/cost-basis`, and the dashboard are **Basic-Auth** — Claude can't fetch them; paste the JSON/screenshot.
- `api.taostats.io` is **not** in Claude's bash allowlist — can't test the API directly; build against documented shapes (e.g. `balance_as_tao` is rao → /1e9, confirmed via `gordie.html`) and verify on first cron run.
- `raw.githubusercontent.com` **is** allowed — Claude can `curl` live repo files to diff before whole-file replaces. **Project-file copies in the Claude Project are stale (pre-v4); always pull live from raw first.**
- Deploy flow: GitHub web editor whole-file replace → Railway auto-redeploys all 3 services → hit SA cron **Run** to refresh `/api/score` + `/api/cost-basis` without waiting for 11:00/23:00 UTC → hard-refresh dashboard (Ctrl+Shift+R).
- GitHub API auth incident still showing on Railway's banner — upstream, not ours; commits/deploys went through fine all session.

---

## Commits this session (chronological)
1. `feat(engine): gate trim/take-profit on real P&L, not launch-anchored EMA`
2. `feat(scoring): compute holdings P&L map and thread into Telegram trim gate`
3. `fix(scoring): balance_as_tao is rao — divide by 1e9 for correct P&L`
4. `fix(engine): EMA 72→24 (daily, not 1H) + surface pct_from_ema in report`
5. `feat(client): retry/backoff on transient Taostats failures (timeout/429/5xx)`
6. `chore(dashboard): sync HOLDINGS/WATCHLIST to on-chain set; prune stale TARGETS`
7. `fix(candidates): drop SN0 from forced enrichment set`
