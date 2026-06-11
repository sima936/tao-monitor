# TAO Monitor — Live State
## Last verified: June 8, 2026 (inline-macro fix + 12h cron flip + full credential remediation)

> **Rule:** Update this file at the end of every session.
> **Rule:** Start every session by fetching live files from GitHub, not project-file copies.
> **Rule:** Trust the Railway dashboard + `/status`, not memory.
> **Rule:** Railway **Variables ARE the `.env`** for deployed services — there is NO local tao-monitor `.env`.
> **Rule:** Railway web **Console is NOT a faithful runtime** — verify via `/status` or a cron fire.
> **Rule:** Secrets go in env vars only — never in committed code or client-side JS. Keep `.env` gitignored.

---

## Railway topology (project `bountiful-celebration`, env `production`)

Account: simart936@gmail.com · Plan: HOBBY

| Service | Runs | Schedule | Status | Role |
|---------|------|----------|--------|------|
| `tao-monitor` | `serve.py` (Procfile: `web: python3 serve.py`) | always-on web | Online | Legacy GORDIE dashboard (gordie.html). Now behind `DASHBOARD_USER`/`DASHBOARD_PASS`. URL: tao-monitor-production.up.railway.app |
| `alluring-smile` | `python3 tao_bot_listener.py` | always-on | Online | **Tao Seeker** bot. `/status` → `run_scoring.py` (v4, inline macro). |
| `spectacular-adaptation` | **`python3 run_scoring.py --no-concentration --force-send`** | cron `0 11,23 * * *` | Online (cron) | 12h report — **FLIPPED to v4 this session.** First scheduled fire 23:00 UTC Jun 8 = **pending verification**. |
| `believable-contentment` | `run_scoring.py --no-concentration` | none | **Crashed** | Misconfigured. Leave; delete after v4 cron is stable. |

Other project `hermes-trading`: separate, out of scope. (Confirmed it does NOT use the Taostats key — see below.)

**Telegram:** Tao Seeker (current; token in Railway env, matches BotFather). Old **Tao Watcher** (bot ID `8570695279`) is dead/suspended.

---

## Holdings (on-chain via `/status`, Jun 8)

`[0, 4, 9, 44, 46, 55, 68, 107, 123]` — SN0 Root (Kraken hotkey, always fails price filter), SN4 Targon, SN9 iota, SN44 Score, SN46 Zipcode, SN55 NIOME, SN68 NOVA, SN107 Minos, SN123 MANTIS. SN44/107/123 in/near Bear regime as of Jun 8.

---

## What shipped this session (Jun 8)

### 1. Inline TAO macro (run_scoring.py) — DONE + verified
- Commit `macro: compute TAO regime inline (file fallback retained)`.
- New `compute_tao_macro_inline()`: lazy-imports `markov_regime.fetch_ticker`+`analyze`, fetches TAO-USD, runs `analyze(window=TAO_WINDOW=14, threshold=TAO_THRESHOLD=0.07, min_train=60, hmm=False)`, returns the tao_macro.json-shaped dict.
- Wiring: `macro = compute_tao_macro_inline() or load_tao_macro_signal()` (inline → file fallback → Unknown; never worse than before).
- Removes the cross-service file dependency; recomputes every cycle (6h staleness gate moot).
- **Macro tuning = 14/0.07** (engine constants, single source of truth). Old fetcher used 20/0.05. Macro is the deliberately-calm layer vs per-subnet 7/±10%. Tuning param to validate later.
- **Verified:** `/status` shows `TAO macro: Bear (signal -0.89)`, corroborated vs market (TAO ~-19% wk / ~-32% mo).

### 2. 12h cron flipped to v4 (#3) — deployed + runtime-verified; 23:00 fire pending
- `spectacular-adaptation` start command `python3 tao_gordie.py once` → `python3 run_scoring.py --no-concentration --force-send`. Cron unchanged.
- `--force-send` because cron containers are ephemeral (no persisted state) — makes the scheduled send explicit.
- Runtime-verified via `/status` (identical engine/runtime). **23:00 UTC scheduled fire = pending verification** (check Cron Runs + Telegram).

### 3. Credential remediation (repo was/IS public) — leak CLOSED
- **Rotated the Taostats API key:** new key created on taostats Pro, swapped across all 3 Railway services, verified (`/status` + dashboard work), **old `tao-monitor` key deleted**. Leak closed.
- **hermes does NOT use the Taostats key** (checked both hermes `.env` files) — safe to delete old key.
- **Deleted dead files:** `tao_enhanced_alerts.py` (held dead Tao Watcher token `8570695279`, nothing imports it) and `tao_dashboard.py` (dead key).
- **Stripped unused hardcoded Taostats key from `gordie.html`** (was line 607, declared but never used — dashboard fetches via `serve.py` proxy).
- **Set `DASHBOARD_USER`/`DASHBOARD_PASS`** on `tao-monitor` (was defaulting to `tao`/`bittensor`). Dashboard verified working on new login.

---

## Dashboard findings (gordie.html) — for the planned revamp

- It's the **legacy GORDIE dashboard**, fetches Taostats directly via `serve.py` proxies (`/api/gordie/pools`, `/api/portfolio/stakes`, `/api/price`). **NOT wired to the v4 scoring engine** — separate data path from `/status`.
- **TARGET/DRIFT = stale hardcoded `TARGETS` map** `{9,68,44,5,55,4,123,75,32}` — still references SN5/32/75 (no longer held), omits SN107/46/0 (held). Lewis-Jackson fixed-weight model; **redundant under Siam v4 strategy** → retire.
- **Orange ▼ markers = target-allocation markers, NOT gain-since-entry.** Gain-since-entry needs a *cost-basis source* (record entry cost, or reconstruct from Taostats stake history) — not currently tracked.
- **Position name labels show validator/delegate names** (Taostats, Datura), not subnet names — inconsistent with `/status`.

---

## Rollbacks (this session)
- Macro fix: revert that commit, or one line → `macro = load_tao_macro_signal()`.
- Cron flip: `spectacular-adaptation` start command back to `python3 tao_gordie.py once`.
- Dashboard creds: delete `DASHBOARD_USER`/`DASHBOARD_PASS` vars → reverts to default login.

---

## Open items (priority)
1. **Verify the 23:00 UTC cron fire** on `spectacular-adaptation` (Cron Runs + Telegram). IMMEDIATE.
2. **Gini = `n/a*` (HIGH dev) — quick win, report-side.** The code already exists: `concentration_from_metagraph()` + `compute_gini_coefficient()` in `taostats_fetch.py`. It's just skipped by `--no-concentration`. Fix: fetch the metagraph for the **9 holdings only** on the 12h cron (bounded, ~2 min at 5/min), leave fast `/status` as-is. Verify the metagraph endpoint (`/api/dtao/metagraph/latest/v1`) returns clean data on the new key.
3. **Dashboard revamp (NEW) — bigger, dashboard-side.** Retire the stale target/drift; fix validator-vs-subnet name labels. **Cost-basis question RESOLVED:** Taostats has no ready "gain since entry" (its portfolio view is timeframe yield/APY and breaks if you bought/sold in-window), but it exposes a **transaction-history endpoint (stakes/unstakes/transfers)**. So reconstruct cost basis per holding = net TAO staked in; gain = current value − cost. Repurpose the orange marker as the break-even/entry line, bar green above / red below. Needs a new `serve.py` proxy for the tx-history endpoint + `gordie.html` rework. (Exact endpoint path: see `docs.taostats.io/llms.txt`.)
4. **15% TRIM (`TP_WARN_PCT`) tuning.** Figures suggest too conservative — make tunable, validate against live `/status` data, don't eyeball.
5. **Wire `price_cache.py`** into `run_scoring.py` (still on `seven_day_prices`/synthetic).
6. **Make repo private** (optional; leak already closed by rotation).
7. **git history scrub** of old secrets (optional — rotation already neutralized them).
8. **`git rm` remaining junk:** `__pycache__/*.pyc`, `yield_cache.json`, `patch_scoring_engine.py`, `session_patches.py`.
9. **Delete `believable-contentment`** after v4 cron is stable a few cycles.
10. **Untangle Infinity8 `~/tao-monitor` clone** (dirty, not in deploy path — do not push from it).

---

## What NOT to do
- Don't treat the Railway web Console as a runtime (no pip pkgs; numpy fails on missing `libstdc++.so.6`).
- Don't look for a local tao-monitor `.env` — there isn't one; Railway Variables are the `.env`.
- Don't push from the Infinity8 clone — apply via GitHub editor.
- Don't blank the cron schedule on `spectacular-adaptation` (`run_scoring.py` runs once and exits → would restart-loop).
- Don't run multiple persistent Telegram senders on one bot (suspended Tao Watcher).
- Don't put secrets in committed code / client JS.
