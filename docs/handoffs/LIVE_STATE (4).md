# TAO Monitor — Live State
## Last verified: June 9, 2026 (Gini re-sourced to holder concentration + real price history + holdings drift fix)

> **Rule:** Update this file at the end of every session.
> **Rule:** Start every session by fetching live files from GitHub, not project-file copies.
> **Rule:** Trust the Railway dashboard + `/status`/`/holdings`, not memory.
> **Rule:** Railway **Variables ARE the `.env`** for deployed services — there is NO local tao-monitor `.env`.
> **Rule:** Railway cron containers are **EPHEMERAL with no volume** — no disk/SQLite persistence. Any cache-file approach (gini_cache.json, price_cache.db) is dead on the Railway path. Use **in-process Taostats fetches bounded to holdings**.
> **Rule:** Secrets go in env vars only — never in committed code or client-side JS.

---

## Railway topology (project `bountiful-celebration`, env `production`)

Account: simart936@gmail.com · Plan: HOBBY

| Service | Runs | Schedule | Status | Role |
|---------|------|----------|--------|------|
| `tao-monitor` | `serve.py` (Procfile: `web: python3 serve.py`) | always-on web | Online | Legacy GORDIE dashboard (gordie.html). Behind `DASHBOARD_USER`/`DASHBOARD_PASS`. URL: tao-monitor-production.up.railway.app |
| `alluring-smile` | `python3 tao_bot_listener.py` | always-on | Online | **Tao Seeker** bot. `/status` + `/holdings` → `run_scoring.py` via subprocess (`--holdings <on-chain set>`, **60s timeout**, NO holdings-gini/history flags → fast path). |
| `spectacular-adaptation` | **`python3 run_scoring.py --no-concentration --force-send --holdings-gini --holdings-history`** | cron `0 11,23 * * *` | Online (cron) | 12h report. v4 + holdings Gini + real history. Now runs ~3–4 min (bounded Taostats calls). |
| ~~`believable-contentment`~~ | — | — | **DELETED Jun 9** | Was misconfigured/crash-looping; removed once v4 cron confirmed stable. |

**Telegram:** Tao Seeker (token in Railway env). Old Tao Watcher (`8570695279`) dead.

---

## Holdings (on-chain, confirmed via `/holdings` Jun 9)

`[0, 4, 9, 44, 46, 55, 68, 107, 123]` — SN0 Root (Kraken hotkey, always fails price filter), SN4 Targon, SN9 iota, SN44 Score, SN46 Zipcode, SN55 NIOME, SN68 NOVA, SN107 Minos, SN123 MANTIS. Total staked ~33.7τ (~£5.4k).
- The cron now **resolves holdings on-chain** (`fetch_wallet_holdings`) instead of a hardcoded list; `CURRENT_HOLDINGS` is now only a last-resort fallback (corrected to the 9-set; was stale `[0,4,51,62,64,68,75]`).

---

## What shipped this session (Jun 9)

### 1. Holdings drift fix — DONE
- `run_scoring.py main()`: when `--holdings` not passed (bare cron), resolve on-chain via `fetch_wallet_holdings(api_key)`, fall back to `CURRENT_HOLDINGS` only on failure.
- Fixed stale `CURRENT_HOLDINGS` constant. Cron report now matches `/status`/`/holdings` (was scoring the wrong 7-subnet portfolio).
- Commit: `fix: in-process holdings Gini + on-chain holdings resolution`.

### 2. Gini (#2) — CLOSED, re-sourced to the correct metric
- **Problem:** the Infinity8 `gini_cache.json` path (`load_gini_cache`) is dead on Railway (ephemeral, no volume; nothing writes it) → genie stayed at 0.5 placeholder → `n/a*`.
- Added **`--holdings-gini`**: in-process Gini for holdings only (skip SN0), opt-in, **cron only** (never `/status` — 60s timeout). `fetch_holdings_gini()` in `run_scoring.py` drops 0.5 placeholders so `n/a*` ≠ fake.
- **Metagraph path bug:** code used `/api/dtao/metagraph/latest/v1` → **404**. Correct path is `/api/metagraph/latest/v1`. Fixed, then discovered metagraph **neuron-stake** Gini is structurally **~0.95 on every dTao subnet** (validator stake concentration) → non-discriminating noise. Abandoned.
- **Correct metric (live now):** holder concentration via **`/api/dtao/stake_balance/latest/v1`** — top 200 holders by `balance_as_tao`, aggregate by `coldkey.ss58`, compute Gini over coldkey totals. One call/subnet. Now **discriminating**: holdings read 0.58–0.77.
- Genie is a **soft signal**, not a gate: the hard `FAIL_GENIE` pre-filter is **commented out** (`subnet_scoring_engine.py:303-304`). It's a 20%-weighted score (`score_genie` zeros ≥0.85) + `⚠️CONCENTRATION` tag (≥0.85). **No auto-exits from Gini.**
- Commits: `fix: correct taostats metagraph path + post-dTao stake field` (superseded), then `feat: Genie = top-holder concentration via stake_balance (not neuron stake)`.

### 3. Real price history (#5) — CLOSED via pool/history (NOT price_cache)
- **Problem:** pool/latest no longer returns `seven_day_prices` → every subnet was scoring on a **9-bar SYNTHETIC** series (`taostats_fetch._synthetic_history`, reconstructed from just the 24h/7d % anchors). Markov regimes/trend were essentially two numbers.
- **`price_cache.py` (SQLite) is the WRONG tool on Railway** (ephemeral fs, no volume → never persists). **SUPERSEDED** — leave the file, it's harmless/dormant.
- Added **`--holdings-history`**: `fetch_holdings_history()` pulls real daily bars via **`/api/dtao/pool/history/v1`** (`frequency=by_day`, `limit=200`, `order=timestamp_asc`), overwrites holdings' `price_history`/`timestamps` before scoring. Skip SN0; subnets with <9 real bars keep synthetic. Opt-in, cron only.
- **Effect (real vs synthetic):** SN9 iota Bull→Sideways, SN44 Score Bear→Sideways (off exit list), SN55 NIOME Sideways→Bear (onto exit list). Decisions now grounded in real price action.
- Commit: `feat: real daily price history for holdings (pool/history, replaces synthetic)`.

### 4. Cron command + ops
- `spectacular-adaptation` start command updated to add `--holdings-gini --holdings-history`.
- **#1 verified:** 23:00 UTC Jun 8 cron fired, scored, Telegram sent.
- **#9 done:** `believable-contentment` deleted.

---

## ⚠️ CRITICAL — orphaned deployment / gordie.html divergence (reconcile before #3)

- Railway was running orphaned commit **`2598e5d8`** (NOT on `main`; `main` HEAD was `c64b55c`). `main` was force-pushed/reset, orphaning the deployed commit; the cron reused the last build.
- The `.py` scoring files were **identical** across both (report behaviour matched `c64b55c` exactly), so the divergence is confined to **`gordie.html`** — the live dashboard may be **ahead** of `main`.
- Today's commits put `main` ahead and Railway redeployed from `main`. **Before any dashboard work (#3), confirm the live dashboard's `gordie.html` == `main`'s** — the orphan may have had dashboard edits not on `main`. Check `https://github.com/sima936/tao-monitor/commit/2598e5d8` (orphans often still load) or diff the live served HTML vs `main`'s `gordie.html`. If the orphan is ahead, pull its `gordie.html` into `main` first.

---

## Verified Taostats endpoints (docs.taostats.io OpenAPI, Jun 2026)

| Purpose | Path | Notes |
|---------|------|-------|
| Pools (price/depth) | `/api/dtao/pool/latest/v1` | No longer returns `seven_day_prices`. |
| Price history | `/api/dtao/pool/history/v1` | `frequency` (by_block/by_hour/by_day), `limit`≤200, `order` (timestamp_asc…). `price` in TAO + `timestamp` per bar. |
| Holder concentration (Gini) | `/api/dtao/stake_balance/latest/v1` | `order=balance_as_tao_desc`, `limit`≤200. Records: `coldkey.ss58`, `balance_as_tao` (rao), `subnet_total_holders`. |
| Aggregated stake sum | `/api/dtao/stake_balance_aggregated/latest/v1` | Coldkey totals **across all subnets** (no `netuid`) — NOT a per-subnet denominator. |
| Metagraph | `/api/metagraph/latest/v1` | NOT `/api/dtao/...` (that 404s). Neuron stake → ~0.95 everywhere → **not used**. |
| Coldkey distribution | `/api/subnet/distribution/coldkey/v1` | `{coldkey, count}` = position count, not stake → not used. |
| Wallet holdings | (`fetch_wallet_holdings` in `taostats_fetch.py`) | On-chain holdings for `/status` + cron resolution. |

---

## Open items (priority) — for the next chat

1. **#3 Dashboard revamp (BIG).** FIRST reconcile the `gordie.html` orphan (confirm `main` == live). Then: fix **validator-vs-subnet name labels** (SN9 shows delegate "Taostats"/"Datura", should show "iota" etc.); retire the **stale TARGET/DRIFT** map (Lewis-Jackson fixed-weight, redundant under Siam v4); **cost-basis** via the tx-history (stakes/unstakes/transfers) endpoint → reconstruct net TAO staked → gain vs break-even; repurpose the orange marker as the break-even line (green above / red below). Needs a new `serve.py` proxy for tx-history + `gordie.html` rework. (Exact tx-history path: see `docs.taostats.io/llms.txt`.)
2. **Genie threshold (0.85) recalibration (tuning).** Holder-Gini runs ~0.58–0.77 (conservative — top 200 only, tail omitted), so `MAX_GENIE_SCORE = 0.85` may rarely fire. Spot-check a couple vs tao.app, then tune `MAX_GENIE_SCORE` (one line in `subnet_scoring_engine.py`).
3. **SN55 NIOME Gini intermittency.** Flipped 0.69 → `n/a*` between runs (transient `stake_balance` miss / rate blip from 17 back-to-back calls). If it persists 2+ runs, add a retry to `fetch_holdings_gini` / `GiniFetcher`.
4. **#4 `TP_WARN_PCT` (15% trim) tunable.** Too conservative — make tunable, validate vs live `/status`.
5. **SDK-path footgun (tracked, dormant).** `GiniFetcher.get_gini` tries the Bittensor SDK first; that path still computes **neuron-stake** Gini (wrong). Dormant on Railway (bittensor not installed). If ever `pip install bittensor` on a host, point the SDK path at holder balances too.
6. **#8 `git rm` junk:** `patch_scoring_engine.py`, `session_patches.py`, `__pycache__/*.pyc`, `yield_cache.json`. (Audit the other `tao_*.py` files — several may be dead too.)
7. **Extend real history + Gini beyond holdings** to top-ranked candidates (currently holdings-only; the other ~120 subnets are still synthetic/placeholder). Improves opportunity ranking outside Bear macro.
8. **#6 make repo private** (optional; leak already closed by key rotation). **#7 git-history scrub** (optional).

---

## Rollbacks (this session)
- **Disable holdings Gini + real history:** remove `--holdings-gini --holdings-history` from `spectacular-adaptation` start command (both default `False` in `run_scoring.py` → fully off).
- **Gini source:** if `stake_balance` misbehaves, revert `gini_fetch.py` to the prior commit. Safe failure mode is Gini → `n/a*` (no fake values, no auto-exits).
- **Holdings resolution:** revert `main()` to `holdings = CURRENT_HOLDINGS` (now the corrected 9-set) if `fetch_wallet_holdings` errors.

---

## What NOT to do
- **Don't** add `--holdings-gini` / `--holdings-history` to `/status` (`alluring-smile`) — 60s subprocess timeout; they add ~200s.
- **Don't** rely on SQLite/disk cache on the Railway cron (ephemeral, no volume) — `gini_cache.json` (Infinity8) and `price_cache.py` are both dead on the Railway path.
- **Don't** redeploy assuming `main`'s `gordie.html` == live until the orphan (`2598e5d8`) is reconciled.
- (carry-over) Railway Variables ARE the `.env`; don't push from the Infinity8 clone (apply via GitHub editor); secrets in env only; don't blank the `spectacular-adaptation` cron schedule (`run_scoring.py` runs once and exits → would restart-loop); don't run multiple persistent Telegram senders on one bot.
