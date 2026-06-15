# LIVE_STATE_18 — TAO Monitor

**Session:** 2026-06-14 (afternoon) · **Repo:** sima936/tao-monitor `main` · **Cron:** spectacular-adaptation (`0 11,23 * * *`, 11:00/23:00 UTC)

---

## Net of this session

Diagnosed and fixed the recurring dashboard freeze + the "less than impressive" Telegram fetch-error. Three commits on `main` (all whole-file replace via GitHub web):

| # | Commit | File | Service |
|---|--------|------|---------|
| 1 | `fix(dashboard): make price fetch best-effort so a blip can't freeze the board` | gordie.html | tao-monitor |
| 2 | `fix(dashboard): wire CoinGecko key + stale-serve last-good price` | serve.py | tao-monitor |
| 3 | `fix(scoring): soft-fail on transient fetch error, not a raw-traceback alert` | run_scoring.py | spectacular-adaptation (+ bot `/status`) |

---

## Root cause — the recurring "Price fetch failed" freeze

Two independent free upstreams, polled every 5 min on Railway's **shared egress IP**, with no buffering and an all-or-nothing fetch:

- **The real cause:** `serve.py proxy_price` hit CoinGecko **with no key header** despite `COINGECKO_API_KEY` being set on the `tao-monitor` service. The cron-side `tao_gordie.py` sends `x-cg-demo-api-key`; `serve.py` never did. So the dashboard's price calls were unauthenticated → rate-limited unpredictably.
- **The amplifier:** `gordie.html` did `await Promise.all([fetchPools(), fetchPrice()])` — `Promise.all` rejects if *either* upstream blips, aborting the whole `loadData()` **before any render**. A single CoinGecko 429 left the last scan frozen on screen with the red `Error: Price fetch failed` pill. It retried every 5 min but re-aborted each time; "recovery" only happened when the upstream healed (the redeploys that seemed to fix it were incidental).

The 6/13 15:15 freeze logged in LS17 was the same fragility, not a bad commit from that session.

## The fix — three layers

1. **serve.py — key:** `proxy_price` now reads `COINGECKO_KEY` and sends `x-cg-demo-api-key` when present. Authenticated, higher limits → far fewer failures.
2. **serve.py — stale-serve:** new module global `LATEST_PRICE_BODY` caches the last good CoinGecko bytes; on a failure it serves them as `200` (flagged `X-Price-Cache: stale`), returning `502` only on a cold cache. After first success, a blip never reaches the browser.
3. **gordie.html — decouple:** `fetchPrice().catch(() => null)` makes price best-effort (pools stay load-bearing); price display guarded with `priceData?.bittensor?.usd`. Even a cold-cache failure can no longer blank the board.

## Telegram fetch-error (separate path) — soft-fail

The unimpressive Telegram was the **cron** hitting a transient `api.taostats.io` read-timeout (25s, 2 attempts) in `fetch_all_subnet_metrics`. `run_scoring.py run()` returns **before** any `save_state`/dashboard push on this path, so good state was never clobbered — the only problem was the raw-traceback message. Now it sends a calm `🟡 data source slow … holding last state, no changes made, will retry next run`, classifying transient (timeout/connection/5xx) vs unexpected errors. State preserved either way.

---

## Service ↔ file map (confirmed this session — important)

| Railway service | Runs | CoinGecko key needed? |
|---|---|---|
| **tao-monitor** | `serve.py` (dashboard) | **Yes** — already set; code now reads it |
| **spectacular-adaptation** | `run_scoring.py` (cron) | No — taostats + GeckoTerminal only (key present but inert) |
| **alluring-smile** | `tao_bot_listener.py` (bot) | No — bot + run_scoring + `fetch_tao_macro.py` (yfinance) never call CoinGecko |

> Method for any "does service X need var Y": check the service's Start command → search the repo for the var name + the API host it gates. Only `serve.py` and (legacy) `tao_gordie.py` reference CoinGecko; only `serve.py` runs on a live service.

---

## Verification

- Dashboard recovered to **Live**, price populated, no error badge (2:44 PM scan); Portfolio `TARGET` column repopulated after SA re-seeded `/api/score`.
- Cron clean reports at **13:10 and 13:43** (macro flipped Bear → **Sideways +0.00**).
- After this session's push (which redeploys tao-monitor and clears the in-memory score cache), give SA a **Run now** to re-seed `/api/score`, else it self-heals at the 23:00 UTC run.

## Portfolio state (13:43 UTC report)

Macro **Sideways +0.00** → deploy 50% · SN0 50% (16.5τ). 9 positions (8 alpha + SN0 dust):
Minos (h63 🟢Bull), Zipcode (h59), iota (h45 🔴Bear, **pending exit 13h/18h**), Score (h43), NOVA (h39), Targon (h39), MANTIS (h34), NIOME (h30 🔴Bear, **pending exit 16h/18h**).
- iota flagged 🔻 take-profit (+26% P&L).
- `conviction_tags = {4, 107, 46, 44, 68, 123}`. NIOME (55) untagged → it's the one riding the confirmation gate alongside iota.

---

## Top of queue — next session

1. **Watch NIOME / iota confirm-exits.** NIOME ~16h, iota ~13h of 18h — both should hit the gate around the ~23:00 (6/14) run → confirmed EXIT, *unless* health recovers >45 (resets the clock).
2. **NEW OPEN — `/api/score` persistence.** Every deploy clears `serve.py`'s in-memory `LATEST_SCORE` / `LATEST_COST_BASIS`, so the dashboard's Opportunities + Portfolio targets read "awaiting scan / —" until the next cron POST. Persist these to the `/data` volume (or have serve.py read a file) so a redeploy doesn't blank the score panels. (`LATEST_PRICE_BODY` could persist too — minor.)
3. **Cron cadence decision** — still open: 12h vs 6h (`0 */6 * * *`). 6h gives a cleaner 18h confirmation.
4. **Audit `price_cache.py`** — SQLite per-subnet history cache designed for Infinity8; `status()` still references the stale holdings list `[0,4,51,62,64,68,75]`; unclear if it's wired into the live Railway cron (cron uses GeckoTerminal for history). Confirm whether it's used/needed.
5. **Unreviewed repo files** — `patch_scoring_engine.py`, `session_patches.py`, `tao_enhanced_monitor.py`, `debug_api.py` seen in the tree but never read; audit roles if relevant.

## Backlog (carried)

- **OPEN #5 mappings** — Video=SN85 Vidaio ✓, Basilica SN39, Grail SN81; **Hippias≈Hippius** (confirm SN#); **Lead Power** still unresolved (needs interview audio).
- **OPEN #3** — `health_b` calibration; `score_log.csv` accumulating on `/data`.
- **OPEN #4** — Gini 429 (parked; 48h cache + SDK→RPC→taostats fallback absorbs it).
- **OPEN #2** — pool-floor display divergence: dashboard `FILTERS.min_pool_depth` (15000) ≠ engine `MIN_POOL_DEPTH` (5000) — align.
- **cash-sink modelling** — bear sink as root-stake vs free unstaked TAO (near-interchangeable; free TAO more liquid).

## Working-environment notes (carry forward)

- **Project file copies are stale** — always pull live from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before editing.
- **bash allowlist:** GitHub raw + api **yes**; `api.taostats.io` / `api.geckoterminal.com` / `api.coingecko.com` **no** — can't test data APIs from the sandbox; verify on deploy or via the dashboard proxies / Railway logs.
- **Deploy** = GitHub web whole-file replace (or surgical block); a push redeploys **all three** services from the same repo. tao-monitor redeploy clears the in-memory score cache (see open #2 above) — Run now on SA to re-seed.
- **Key constants:** `MAX_TOKEN_PRICE=0.15`, `MIN_POOL_DEPTH=5000`, `MAX_POOL_DEPTH=500000`, `confirm_hours=18`.
- CoinGecko Demo key auth = header `x-cg-demo-api-key` on `api.coingecko.com` (matches `tao_gordie.py`).
