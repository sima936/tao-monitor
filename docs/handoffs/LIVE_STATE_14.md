# TAO Monitor — LIVE_STATE / Session Handoff

**Updated:** 2026-06-12 (eve) · **Repo:** sima936/tao-monitor (`main`)
**Railway:** bountiful-celebration / production · cron = `spectacular-adaptation` (11:00/23:00 UTC), web = `serve.py`
**This session:** confirmed LIVE_STATE_13's bear-book fixes are live, closed OPEN ITEM #1 (engine macro multiplier), fixed the dashboard caption it orphaned, then **diagnosed and fixed the synthetic-history root cause** that was whipsawing holdings' regimes. **4 commits**, **1 new file** (`geckoterminal_fetch.py`).

---

## Verified live at session start (LIVE_STATE_13 fixes working)

The 11:07 and 19:24 UTC digests both matched the predicted bear book: `deploy 15% · SN0 85%`, Minos-led TARGET BOOK, the held losers in CUT, and the "new entries suppressed" note. Header reconciles. Dashboard tabs cross-check clean. The 3 allocator fixes (real-data sizing, `new_entries_only_in_bull`, per-name cap of-account) are confirmed in production.

---

## CLOSED this session

### 1. Engine — removed redundant `entry_score` macro multiplier (OPEN ITEM #1)
`subnet_scoring_engine.py`: deleted the post-composite `entry_score *= 0.3/0.5/0.7` (Bear/Sideways/Unknown) switch. Macro was triple-counted (p2_macro in-score + this multiplier + the allocator dial). Now single-counted: **p2_macro (15% of entry_score) + the Axis-1 dial at allocation.**
- **Proven safe (offline):** rank order preserved (uniform multiplier never changed sort); allocation provably unchanged (allocator sizes off `health_score`, which has **no** p2_macro and was untouched; dial untouched); entry numbers now read true composite (~50) instead of bear-crushed (~15).
- **Confirmed LIVE** via Opportunities tab: entries >30 are impossible under the old ×0.3 (max 30) — so the new engine is running.
- Does **not** touch `p2_macro`.

### 2. Dashboard — corrected Opportunities caption (orphaned by #1)
`gordie.html`: banner said `entries suppressed ×0.3 / dampened ×0.5 / scaled ×0.7` and subtitle `sorted by entry_score (macro-scaled)` — all false after #1 (and the ×0.5/×0.7 were already mismatched vs the engine's old ×0.7/×1.0). Now: subtitle `macro-aware via p2 … new entries macro-gated, acted on only in Bull`; banner notes `no new entries — capital preservation (rank only)` (Bear/Sideways) / `entries live — rotate actively` (Bull). Client-side JS only; web service.

### 3. ROOT CAUSE — synthetic-history regime instability → GeckoTerminal real history
**Symptom:** Minos 7d read +39.7% (scan) / +68% (11:07) / −3% (19:24) on a flat price (~0.032), flipping it Bull→Sideways and shuffling the book on noise.
**Cause:** `pool/latest` stopped returning `seven_day_prices`, so `_synthetic_history()` rebuilt a 9-bar line from 3 anchors {price, 1d%, 1w%}. The engine's 7d/EMA/Markov/regime became a deterministic re-encoding of the volatile `price_change_1_week` anchor. taostats `pool/history` fallback (`--holdings-history` IS on in the cron) returned too few daily bars → kept synthetic.
**Fix:** **GeckoTerminal** (CoinGecko on-chain DEX API) daily OHLCV.
- Pool address is just `0-{netuid}` (no map needed). Free, no key, ~90 daily bars, ~6mo history.
- `currency=token` → **TAO-denominated, values match taostats exactly** (Minos 0.0326≈0.0325, NOVA 0.0215, Zipcode 0.0111, etc.) → no discontinuity, slots straight in.
- New `geckoterminal_fetch.py` (probe-tested locally on all 8 holdings: 90 real bars each, stable 7d). GT free tier 429s under load; module uses **4s pacing + 3-step backoff (5/10/20s, honors Retry-After)** which recovers every subnet.
- Wired in `run_scoring.py` as **PRIMARY** history source for the `targets` set; **taostats `pool/history` is the FALLBACK** for any subnet GT lacks. Same `{netuid:(closes,timestamps)}` contract → drops into `apply_history_overrides`. Rides the existing `--holdings-history` flag (no Railway change).

---

## Architecture change

History for holdings/targets now comes from **GeckoTerminal real daily OHLCV**, not taostats synthetic reconstruction. taostats still owns: current snapshot (price/pool/volume), Gini, and the **absolute-threshold pre-filters** (price<0.04, pool depth) — all unchanged. GT is used for time-series shape only; values happen to match taostats so the two are consistent.

---

## Verified (offline / local) — NOT yet verified on a live wired cron

- Engine: rank-order + allocation-unchanged proofs pass; confirmed live via Opportunities entries >30.
- gordie.html: 3 stale spots fixed, inline JS parses, no residual ×N text.
- GT: all 8 holdings → 90 real TAO-denominated bars matching taostats; backoff recovers SN46 (2 retries) & SN123 (3 retries).
- `run_scoring.py`: compiles, full module graph imports, GT wired, fallback intact, clean diff.
- **Verify on the 23:00 UTC cron** (no taostats/GT access from the build sandbox).

---

## DEPLOY + VERIFY (23:00 UTC cron)

1. All 4 commits on `main`; Railway redeploys cron + web.
2. Cron log expected: `Real history: N from GeckoTerminal, M from taostats fallback (K not on GT)` — expect N≈holdings (more if `--candidates` enriches movers).
3. Holdings now on real bars → **Minos reads a consistent regime/EMA**; the real test is **run-to-run stability** (23:00 vs tomorrow 11:00 — no Bull↔Sideways flip on flat price).
4. **Watch cron runtime once.** GT replaces ~187s of taostats history; 429 backoff chains (≤35s each) could stretch it. Bounded; demo key is the lever if ugly.
5. Dashboard Opportunities banner now reads `no new entries — capital preservation (rank only)` in Bear.

---

## OPEN ITEMS / NEXT (priority)

1. **Verify the 23:00 UTC cron** — GT history live + runtime sane (above).
2. **#2 calibration — NOW UNBLOCKED.** Once a couple of real-data runs land, tune against *stable* regimes: `health_b=45` (only Minos survived the noisy bear — recheck on real data), `new_entries_only_in_bull` (allow Sideways entries?), per-name cap 40%-of-account, deep-bear `deploy_bands`. One-line `AllocationPolicy` knobs.
3. **#4 `--candidates` 15 → 25** — Railway `spectacular-adaptation` Start command edit (one token). GT history now enriches candidate movers too → better Opportunities ranking on Bull. Mind combined runtime.
4. **#6 dashboard cleanup batch (cosmetic):** stale `CURRENT_HOLDINGS = [0,4,51,62,64,68,75]` fallback (real book is 107/9/55/44/68/46/4/123/0 — only 0/4/68 overlap); Scanner "7 positions" badge vs 9 rows; dashboard FILTERS min-pool 15,000τ vs engine pre-filter divergence; dead `CANDIDATE_BUDGET = 25` constant in run_scoring.py.
5. **#5 Phase-3 rebalancer** — allocator `positions[].action` + drift deadband is the input; add cooldown.
6. **Optional reliability:** free CoinGecko **Demo API key** → steadier GT limits (kills most 429s) if cron runtime balloons. Module currently keyless.

---

## Working-env notes

- **GeckoTerminal:** `https://api.geckoterminal.com/api/v2/networks/bittensor/pools/0-{netuid}/ohlcv/day?aggregate=1&limit=N&currency=token`. Free, no key, aggressive 429s → 4s pacing + backoff. `currency=token` = TAO (matches taostats). NOT in the build-sandbox allowlist — probe locally: `python geckoterminal_fetch.py --netuid 107` (needs `requests`; pip is `python -m pip` on Simon's box).
- Pull live files from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before edits (Project copies stale). `api.taostats.io` not in bash allowlist; dashboard `/api/*` Basic-Auth (paste JSON/screenshots).
- Railway: `spectacular-adaptation` = cron (Start cmd: `python3 run_scoring.py --no-concentration --force-send --holdings-gini --holdings-history --candidates 15 --cost-basis`); `serve.py` = web/dashboard. Both rebuild on any `main` push.

---

## Commits this session

1. `refactor(engine): remove redundant entry_score macro multiplier — dial single-counts macro (doesn't touch p2_macro)`
2. `fix(dashboard): correct Opportunities caption — entry_score is macro-aware via p2, not ×0.3-scaled (engine multiplier removed)`
3. `feat(history): GeckoTerminal real daily-OHLCV fetcher for subnet pools — 4s pacing + 429 backoff (no key)`
4. `feat(history): GeckoTerminal real daily history as primary source (taostats fallback) — kills synthetic-anchor regime instability`
