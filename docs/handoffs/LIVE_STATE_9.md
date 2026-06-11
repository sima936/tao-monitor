# TAO Monitor — LIVE_STATE / Session Handoff

**Updated:** 2026-06-10 18:05 UTC
**Repo:** sima936/tao-monitor (default branch `main`)
**Railway project:** bountiful-celebration / production

---

## Architecture (current)

Three Railway services, all from `main`:

- **tao-monitor** / `serve.py` — always-on dashboard (`gordie.html`), Basic Auth. In-memory bridges: `POST /api/ingest-score`→`GET /api/score`, and **NEW** `POST /api/ingest-cost-basis`→`GET /api/cost-basis`. Also proxies `/api/price`, `/api/gordie/pools`, `/api/portfolio/stakes`, `/api/vtrust`, `/api/yield`. URL: https://tao-monitor-production.up.railway.app
- **alluring-smile** / `tao_bot_listener.py` — always-on Telegram bot; `/status`,`/holdings` shell out to `run_scoring.py` (60s fast path). **Do not add latency here.**
- **spectacular-adaptation (SA)** / `run_scoring.py` — cron `0 11,23 * * *` UTC. Posts 12h holdings report to Telegram + pushes v4 score AND cost-basis to dashboard.

**Start commands live in Railway service Settings, NOT the repo.** `Procfile` only has `web: python3 serve.py`. SA's Custom Start Command is now:
```
python3 run_scoring.py --no-concentration --force-send --holdings-gini --holdings-history --candidates 15 --cost-basis
```

Apply repo changes via the **GitHub web editor**. Railway cron is ephemeral (no disk); Infinity8 (SSH box) holds the gini cache but it's unreachable from Railway, so Railway falls back to in-process gini fetch.

**Holdings (on-chain authoritative):** [0,4,9,44,46,55,68,107,123] ≈34.4τ (£5,302) — SN0 Root, SN4 Targon, SN9 iota, SN44 Score, SN46 Zipcode, SN55 NIOME, SN68 NOVA, SN107 Minos, SN123 MANTIS. **Watchlist (reference):** SN3 Teutonic. (Dashboard `gordie.html` `WATCHLIST`/`HOLDINGS`/`TARGETS` constants are stale vs this set — see open item 4.)

**Engine:** v4 `subnet_scoring_engine.py`, 10-gate scorer → `entry_score` (macro-scaled) + `health_score`. TAO macro currently **Bear** (signal −0.89) → entry_score ×0.3 → entries suppressed ("MACRO_BEAR — no new entries"). Taostats **free tier = 5 calls/min (~12.5s/call)**.

---

## CLOSED this session

### ✅ Opportunities panel — v4 board on the dashboard (SHIPPED + verified live)
`gordie.html` Opportunities tab rewritten to consume `/api/score` (the cron's v4 board) instead of the browser-side pool scorer, **gated to `params.p9_data_maturity === 100`** (real-data only). This solved the "mixed board" problem — the synthetic-placeholder majority that used to outrank truthfully-scored subnets is now hidden (badge shows e.g. "11 real-data · 117 hidden"). Macro banner up top (regime + entry-scaling note), columns Entry/Health/Regime/24h/7d/Gini/Signals, `0.5→n/a` Gini sentinel, NaN/Infinity-resilient `fetchScore()`, decoupled `loadScore()` on its own 5-min interval. **Verified:** banner reads "Bear · −0.89 · ×0.3"; board renders the enriched set with real matrices.

### ✅ Engine `to_json` macro block (SHIPPED)
Added `macro` block (regime/signal/bull_prob/bear_prob/strategy_mode/available) to `to_json()` so the dashboard banner can populate. Additive only.

### ✅ #4 — AUTO cost-basis / P&L (SHIPPED + verified live)
Replaced the abandoned manual-file idea with a fully automatic system across 4 files + 1 setting:
- **`taostats_fetch.py`** — `fetch_cost_basis()`: pages `GET /api/delegation/v1?nominator={coldkey}` oldest→newest, buckets by netuid, computes `net_invested = Σ TAO in (DELEGATE) − Σ TAO out (UNDELEGATE)`. Returns `{positions:{netuid:{tao_invested,tao_in,tao_out,n_events,transfers}}, _capped, _computed, _total_events, ...}`. Safety cap 25 pages (flags `_capped`).
- **`run_scoring.py`** — `push_cost_basis_to_dashboard()` (derives ingest URL from `DASHBOARD_INGEST_URL` by swapping `ingest-score`→`ingest-cost-basis`, same token) + `--cost-basis` flag; computes & pushes on the cron, non-fatal on failure.
- **`serve.py`** — `LATEST_COST_BASIS`, `POST /api/ingest-cost-basis`, `GET /api/cost-basis`.
- **`gordie.html`** — Portfolio tab now shows Cost (τ) / P&L (τ) / P&L % per position + Total P&L card, fed by `/api/cost-basis`.

**Method note:** P&L = current `balance_as_tao` − net_invested, TAO-denominated. Emission/staking rewards handled correctly for free (not DELEGATE events → show up as value at zero cost). SN0 Root can show *negative* net_invested (house money) → P&L% suppressed to "—" (cosmetic; 0.0024τ dust).

**Verified live (18:01 cron run):** Total +0.42τ (+1.2% vs cost, priced positions). No `_capped`, no transfer anomalies — full history fit, basis complete. Numbers cross-check against regime/momentum (Minos winner, NIOME/MANTIS losers).

---

## ACTIONABLE SNAPSHOT (18:01 UTC, macro Bear → entries blocked)

Per-position P&L (TAO cost basis now real):
- **SN107 Minos: +2.44τ (+48.8%)** — Bull regime, +68% 7d, "at recent high / good exit zone". Real profit + exit signal → the live take-profit candidate.
- **SN9 iota: +1.65τ (+29.5%)** — Sideways, solid.
- **SN46 Zipcode: −0.2%** (≈break-even), **SN4 Targon: −3.6%**.
- **SN68 NOVA: −15.3%**, **SN55 NIOME: −25.2%** (weakest health 25, Bear regime), **SN123 MANTIS: −30.4%** (biggest % loss, Sideways).
- **SN0 Root:** dust, negative basis (house money), P&L% n/a.
- **Total:** +0.42τ (+1.2%) — winners carrying losers; flat overall in bear macro.

---

## OPEN ITEMS / NEXT (priority order)

1. **#4 follow-on — P&L into Telegram exit logic.** Gate "good exit zone" / trim alerts on **≥X% real profit** (e.g. only flag Minos-style exits when actually in profit; never "trim into strength" on an underwater name). Now feasible — cost basis is computed on the cron at push time; thread the cost-basis dict into `format_telegram_alert` (engine) or compute the gate in `run_scoring.py` before building the message.
2. **EMA window refinement.** `pct_from_ema` cluster at −0.94…−0.998 on enriched movers — EMA anchored to launch-era prices, not tactically useful. Shorten EMA period (~20–30d) or sanity-cap `pct_from_ema` so p3/p7 mean something. Eyeball one enriched subnet's raw `price_history` first.
3. **Taostats read-timeout resilience** (retry/backoff in `TaostatsClient.get`). The cron now does score + gini + history + cost-basis paging in one run — more calls = more timeout exposure. Do this before raising `--candidates` past 15 or the cost-basis page cap.
4. **Minor cleanups:** sync dashboard `HOLDINGS`/`TARGETS`/`WATCHLIST` constants in `gordie.html` to on-chain set {0,4,9,44,46,55,68,107,123}; SN0 negative-basis is cosmetic; strip SN0 from forced candidate budget slot.

---

## Working-env notes (for next Claude session)
- `/api/score`, `/api/cost-basis`, and the dashboard are **Basic-Auth** — Claude can't fetch them; paste the JSON/screenshot.
- `api.taostats.io` is **not** in Claude's bash allowlist — Claude can't test the API directly; build against documented shapes and verify on first cron run.
- `raw.githubusercontent.com` **is** allowed — Claude can `curl` live repo files to diff against edits (do this before whole-file replaces).
- Cost basis needs **no persistence** — it's recomputed fresh each cron run from immutable on-chain stake events (this is why it lives on the cron + dashboard bridge, not a file; also dodges Railway's ephemeral disk).
- Deploy flow: GitHub web editor whole-file replace → Railway auto-redeploys all 3 services → hit SA cron **Run** to refresh `/api/score` + `/api/cost-basis` without waiting for 11:00/23:00 UTC → hard-refresh dashboard (Ctrl+Shift+R).
- GitHub API auth incident was showing on Railway's banner during this session — upstream, not ours; commits/deploys went through fine.
