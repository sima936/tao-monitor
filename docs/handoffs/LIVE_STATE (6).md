# TAO Monitor — Live State
## Last verified: June 9, 2026 (later session — Patch A labels LIVE; v4 score bridge shipped + wired; gordie.html orphan reconciled)

> **Rules:** update this file at end of every session · start sessions by fetching live files from GitHub, not project-file copies · trust the Railway dashboard + `/status`/`/holdings`, not memory · **Railway Variables ARE the `.env`** · Railway cron containers are **EPHEMERAL, no volume** (no disk/SQLite persistence — use in-process fetches / in-memory) · secrets in env vars only · apply repo changes via the **GitHub web editor**, not the Infinity8 clone push.
> **Upload gotcha (Windows):** Explorer hides extensions → downloads become `serve.py.py`. Turn on Explorer → View → Show → **File name extensions**, and verify GitHub staged names are exactly `serve.py` (no `.py.py`, no `(n)`) before commit.

---

## Railway topology (project `bountiful-celebration` / `0af4009d…`, env `production`)

| Service | Runs | Schedule | Status | Role |
|---------|------|----------|--------|------|
| `tao-monitor` | `serve.py` | always-on web | Online | Dashboard (`gordie.html`) behind `DASHBOARD_USER`/`DASHBOARD_PASS`. **NEW:** `POST /api/ingest-score` (token) → in-memory `LATEST_SCORE`; `GET /api/score`. URL: tao-monitor-production.up.railway.app |
| `alluring-smile` | `tao_bot_listener.py` | always-on | Online | Tao Seeker bot. `/status` + `/holdings` → `run_scoring.py` subprocess (60s timeout, fast path). **No ingest vars set → dashboard push is a no-op here (intentional).** |
| `spectacular-adaptation` | `run_scoring.py --no-concentration --force-send --holdings-gini --holdings-history` | cron `0 11,23 * * *` (UTC) | Online | 12h report. **NEW:** also pushes the v4 result to the dashboard (`push_score_to_dashboard`). |

**Telegram:** Tao Seeker.

### New Railway env vars (this session)
- `tao-monitor`: `SCORE_INGEST_TOKEN`
- `spectacular-adaptation`: `SCORE_INGEST_TOKEN` (**same value**) + `DASHBOARD_INGEST_URL` = `https://tao-monitor-production.up.railway.app/api/ingest-score`
- `alluring-smile`: **none** (intentional — keeps `/status` fast, prevents a thin fast-path result overwriting the good 12h one)

---

## Holdings (on-chain, confirmed)
`[0, 4, 9, 44, 46, 55, 68, 107, 123]` — now rendering with correct **subnet** names on the dashboard: SN0 Root, SN4 Targon, SN9 iota, SN44 Score, SN46 Zipcode, SN55 NIOME, SN68 NOVA, SN107 Minos, SN123 MANTIS. ~34τ.

---

## What shipped this session (Jun 9, later)

### 1. gordie.html orphan reconciliation (`2598e5d8`) — CLOSED
- Orphan commit **garbage-collected** from GitHub (commit page / `raw` / `codeload` all 404). Unrecoverable.
- `main` `gordie.html` blob `ac861666` == `c64b55c`; the earlier four Jun 9 commits (`07eb798`/`42e3ed8`/`ed9309a`/`78ae3b3`) touched only `gini_fetch.py` + `run_scoring.py`.
- **Live == main confirmed by content:** the `v3` badge, subtitle, footer, `TARGETS` values, and the validator-label bug mechanism all reproduce from main's source; the Portfolio tab is `/api/portfolio/stakes`-fed (explains the on-chain holdings set). No orphan-only delta — nothing to recover. The prior LIVE_STATE "CRITICAL — orphan divergence" section is **RESOLVED**.

### 2. Patch A — subnet-name labels — SHIPPED + VERIFIED LIVE
- **Bug:** `renderPortfolio` displayed `stake_balance.hotkey_name` (the *validator* you delegate through — "Taostats"/"Datura"), not the subnet.
- **Fix (`gordie.html`):** `resolveSubnetName(netuid, nameByNetuid)`; `nameByNetuid` built in `loadData` from `/api/gordie/pools` `pool.name` (race-free — built before the parallel portfolio fetch); `SUBNET_NAME_FALLBACK = {0 Root, 4 Targon, 9 Iota, 44 Score, 46 Zipcode, 55 NIOME, 68 NOVA, 107 Minos, 123 MANTIS}`.
- Committed to main, deployed, **verified live** (SN9 → iota, SN107 → Minos, etc.).

### 3. Score bridge (Option 1 transport, Parts 1–2) — SHIPPED; awaiting first healthy cron push
- **`serve.py`:** `POST /api/ingest-score` (header `X-Ingest-Token` must equal `SCORE_INGEST_TOKEN`) → stores raw JSON in module global `LATEST_SCORE`; `GET /api/score` (Basic-Auth) → returns `LATEST_SCORE` or `{"status":"awaiting_first_scan","ranked":[]}`. Single-process `HTTPServer`, so in-memory is fine; lost on web-service restart → empty until next cron (acceptable).
- **`run_scoring.py`:** `push_score_to_dashboard(to_json(result))` called right after `run_scoring_cycle`; **no-op unless** `DASHBOARD_INGEST_URL` + `SCORE_INGEST_TOKEN` are both set (so inert on the `/status` fast path and in local runs).
- **Verified:** `/api/score` is live and returns `awaiting_first_scan` (endpoint confirmed). **Cron push NOT yet confirmed** — the first manual cron run hit a transient `api.taostats.io` read-timeout (`read timeout=30`) and aborted *before* scoring, so nothing was pushed. Re-run when Taostats is healthy (the scheduled 23:00 UTC run retries automatically).

---

## Decisions settled
- **Option 1 over Option 2:** surface the v4 **10-gate `entry_score`** on the dashboard (single scoring brain) rather than a lighter JS dip heuristic. **Patch B (JS dip scorer) PARKED** — duplicates the v4 engine; code is in chat history if ever wanted as a stopgap.
- The 10 gates are **built + running** in `subnet_scoring_engine.py` (v4): `p1` Genie · `p2` TAO macro (gates strategy) · `p3` EMA slope · `p4` pullback-from-high · `p5` pool trajectory · `p6` Markov persistence · `p7` EMA displacement rate · `p8` volume trend · `p9` data maturity · `p10` relative-vs-TAO. Outputs `entry_score` (new buys, pullback bias) + `health_score` (holds); TAO macro scales `entry_score` down in Bear/Sideways.
- `to_json(result)` emits `{timestamp, summary, ranked:[SubnetScore…] sorted by entry_score, filtered_out}`. Each `SubnetScore` carries `entry_score`, `health_score`, `markov_regime`, **`pct_from_recent_high`**, `genie_score_raw`, `entry_flags`, etc. — exactly what the dashboard render needs.

## dTAO mechanics note (for the cost-basis work)
Staking swaps TAO → subnet **alpha** via the subnet AMM; you hold alpha. `balance_as_tao` = your alpha marked back to TAO at the current pool price → it **fluctuates with alpha price, the TAO isn't consumed**. ~35τ → ~34τ is mark-to-market + entry slippage, realised only on unstake. **SN0 Root is the exception** (TAO-denominated). So: cost-basis = net TAO staked-in (stakes − unstakes, via tx-history); current value = `balance_as_tao`; gain/loss = the new "break-even" line.

---

## Open items (priority) — next session
1. **Confirm `/api/score` populates** after a healthy cron run (re-run `spectacular-adaptation` when Taostats is responsive, or let the 23:00 UTC run auto-retry). This unblocks #2.
2. **Part 3 — `gordie.html` Opportunities render:** fetch `/api/score`, render the `entry_score` **top-10** with regime / `pct_from_recent_high` / genie / `entry_flags`; replace the local JS `scoreSubnet` ranking (which currently rewards *positive* momentum = chasing). Handle the `awaiting_first_scan` state.
3. **#7 — real data for the candidate set:** extend real history + Gini to the **prefilter survivors** (not just holdings) so the wider `entry_score` ranking is real, not synthetic. Cheap-filter all 128 → fetch history/Gini for survivors only (same bounded pattern as holdings).
4. **Portfolio rework:** retire `TARGET`/`DRIFT` (stale Lewis-Jackson fixed-weight); add **cost-basis / break-even** (repurpose the orange marker as the break-even line) via a tx-history `serve.py` proxy (exact path from `docs.taostats.io/llms.txt`); make the scanner-side held-set dynamic (retire the static `HOLDINGS` const; the Portfolio tab is already on-chain).
5. **Backlog:** Taostats read-timeout resilience (retry/backoff in `TaostatsClient.get` — caused tonight's failed cron) · genie `0.85` recalibration (holder-Gini runs ~0.58–0.77) · `TP_WARN_PCT` tunable · `git rm` junk (`patch_scoring_engine.py`, `session_patches.py`, `__pycache__/*.pyc`, `yield_cache.json`) · SN55 Gini intermittency retry.

---

## Rollbacks (this session)
- **Patch A:** re-upload the prior `gordie.html` / revert the commit — but it's verified good.
- **Bridge:** `serve.py` `/api/score` is inert (returns `awaiting_first_scan`); the `run_scoring` push is a no-op without the two env vars. To disable: remove `DASHBOARD_INGEST_URL` + `SCORE_INGEST_TOKEN` from `spectacular-adaptation`. The new `serve.py` endpoints are harmless if unused.

## What NOT to do
- **Don't** add `DASHBOARD_INGEST_URL` / `SCORE_INGEST_TOKEN` to `alluring-smile` — would add latency to `/status` (60s timeout) and overwrite the good 12h result with a thin fast-path one.
- **Don't** rely on in-memory `LATEST_SCORE` surviving a `tao-monitor` restart (ephemeral; refills on next cron).
- (carry-over) Railway Variables ARE the `.env`; apply repo changes via the GitHub editor, not the Infinity8 clone; secrets in env only; don't blank the cron schedule; one Telegram sender per bot; no SQLite/disk cache on the Railway path.
- Windows hides extensions → always verify GitHub staged filenames before commit.

## Security note
Dashboard is behind HTTP Basic Auth (`DASHBOARD_USER`/`DASHBOARD_PASS`). Creds cache for the whole browser session (incl. an Incognito window) once entered — that's why a second load in the same Incognito window doesn't re-prompt. It IS protected; verify with a brand-new Incognito window. Basic Auth over HTTPS = adequate obscurity, not strong security (no lockout/rate-limiting) — harden if ever needed.
