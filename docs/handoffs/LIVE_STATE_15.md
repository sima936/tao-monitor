# TAO Monitor — LIVE_STATE / Session Handoff

**Updated:** 2026-06-13 (~03:30 UTC) · **Repo:** sima936/tao-monitor (`main`)
**Railway:** bountiful-celebration / production · cron = `spectacular-adaptation` (11:00/23:00 UTC), web = `tao-monitor` (serve.py / dashboard)
**This session:** **added the Railway Volume and verified all three of LIVE_STATE_14's persistence fixes now write to `/data` and survive restarts** — the keystone is closed. Also ran a full subnet-fundamentals research pass for a conviction sleeve. No new commits this session (the 4 commits below were already on `main` from the prior session; this session was deploy-infra + verification + research).

---

## CLOSED this session

### 1. Railway Volume added → persistence fixes now live (THE keystone)
- Volume `spectacular-adaptation-volume` created on the **canvas** (Ctrl+K → "volume"; it is **not** under service Settings in this Railway version), attached to the **`spectacular-adaptation` cron** (the writer), mount path **`/data`**.
- Variables added on that service: `SCORE_LOG_PATH=/data/score_log.csv`, `GINI_CACHE_PATH=/data/gini_cache.json`.
- **VERIFIED LIVE** in the 03:26 run (deployment `12ecc2e7`), via `Run now`:
  - `Score log: appended 15 rows → /data/score_log.csv` — calibration log now accumulates across containers.
  - `Gini cache: saved 11 fresh → /data/gini_cache.json (11 total)` — last-good Gini persists.
  - `Closed-bar trim: dropped today's forming bar on 15/15 series` — intraday/midnight whipsaw fix confirmed.
  - `Real history: 15 from GeckoTerminal, 0 from taostats fallback` — GT primary carried all 15; 429 backoff recovered SN107 (3 retries) + SN123 (2).
  - `Scoring complete: 128 passed, 1 filtered out (334.1s)` — runtime ~5.5 min, in bounds.
- **Browse the files:** `railway volume browse` (CLI) or the Backups tab.

### Prior-session commits (already on `main`, now confirmed persisting)
1. `feat(calibration): per-cycle f_-prefixed score_log.csv logger` → now writing to `/data`.
2. `fix(history): use closed daily bars only (drop today's forming GT bar)` → confirmed 15/15.
3. `fix(gini): persistent last-good cache (+ drops dead CANDIDATE_BUDGET)` → confirmed writing 11 to `/data`.
4. `fix(dashboard): holdings badge counts live positions (was static "7")`.
(`score_calibration.py` analyzer also already committed; reads `score_log.csv`.)

---

## Current portfolio state (03:26 run)

**Macro: Bear (signal −0.89)** → deploy 15% · SN0 85% (29.6τ). New entries suppressed (7 healthy non-held names held back). Chain-read book = **9 subnets: SN0, 4, 9, 44, 46, 55, 68, 107, 123.**

| SN | Name | Health | Regime | 7d | EMA | Gini | Allocator action |
|----|------|--------|--------|-----|-----|------|------------------|
| 107 | Minos | 62 | 🟢Bull | +40% | +26% | 0.72 | TARGET 5% · **TRIM / take-profit (+57% P&L)** |
| 46 | Zipcode | 67 | 🟢Bull | +22% | +3% | n/a* | TARGET 5% · TRIM |
| 9 | iota | 56 | ⚪Side | −10% | +3% | 0.74 | TARGET 5% · TRIM |
| 44 | Score | 42 | ⚪Side | +5% | −3% | 0.61 | **CUT→SN0 (health_below_floor)** |
| 55 | NIOME | 42 | ⚪Side | −9% | −9% | n/a* | **CUT→SN0** |
| 4 | Targon | 39 | ⚪Side | −3% | −5% | 0.78 | **CUT→SN0** |
| 68 | NOVA | 38 | ⚪Side | +8% | +0% | 0.75 | **CUT→SN0** |
| 123 | MANTIS | 35 | ⚪Side | +6% | −12% | 0.76 | **CUT→SN0** |
| 0 | Root | — | — | — | — | — | fail_price_too_high (Kraken hotkey — leave) |

\* Gini n/a = taostats stake_balance 429'd this run (SN46/55/126/117); correctly shown as n/a, **not** fake 0.50. Cache holds 11 real values; the 4 n/a names need ≥1 successful fetch each to cache.

**health_b floor = 45.** The 5 CUT names (h35–42) all sit just under it — and 4 of the 5 are real-utility verticals (see research). This is the live evidence for the conviction-tag guard.

---

## Subnet research — conviction sleeve (this session, all web-verified)

**Netuid mappings (corrected):**
- **Templar = SN3** — your watchlist mislabels it "Teutonic"; it's Templar (decentralized LLM pretraining, trained Covenant-72B). ⚠️ The **Covenant operator exited ~10 Apr 2026** (ran SN3/39/81; TAO −20–25% on exit), **no confirmed successor** — Siam's top conviction pick is materially weakened since his interview.
- **Synth = SN50** (Mode) · **Zeus = SN18** (Orpheus, weather) · **Bitcast = SN93** (Siam's "Bit Coast", creator-ad marketplace, 300k+ creators) · **Hippius = SN75** (Siam's "Hippias", S3/IPFS storage, ~900TB) · **Targon = SN4** (Manifold, inference) · **Affine = SN120** · **Score = SN44** (football prediction) · **Sportstensor = SN41** · **NextPlace = SN48** (real-estate) · **Ridges = SN62** (autonomous SWE) · **MANTIS = SN123** (financial alpha).
- Still **unmapped** from Siam's list: **Video, Basilica, Grail, Lead Power.**

**KEY INSIGHT — your book is already a conviction book, picked on momentum:** 5 of 8 active holdings are real-utility vertical-AI subnets: **Minos/SN107 = genomics** (biggest, +57% P&L), **NOVA/SN68 = drug discovery**, **Score/SN44 = football prediction**, **Zipcode/SN46 = real-estate price**, **MANTIS/SN123 = financial alpha.** Likely action is *re-classify* existing holds to "conviction" rather than buy new names.

**Affine (SN120) — reconciled:** Const/Jacob Steeves (Bittensor co-founder)-led. It is positioned (by Const himself, and analysts) as a **coordination / composition layer** — "backbone" bridging Chutes (SN64) ↔ Ridges (SN62), composing many subnets into one "open intelligence commodity." Demonstrated product *today* is still RL competitions (code/reasoning) leaning on Chutes for hosting; the "non-negotiable backbone" is roadmap/analyst language. Net: **infra-plumbing thesis = real lock-in IF it lands** (flips the "commoditizable, no switching cost" caveat upward), but it's a **high-ceiling, less-proven bet on Const executing**, vs Targon's here-now enterprise revenue.

**Comparison verdict (Targon vs Bitcast vs Score vs Affine):**
- Demonstrated demand: **Targon ≫ Bitcast > Score ≈ Affine** (Targon has Dippy AI — 8.6M users, whole backend migrated, six-figure deal; $10.5M Series A).
- Future moat: Targon (enterprise stickiness) + Affine (coordination lock-in if real) have moats; Bitcast/Score more commoditizable.
- Caveat: subnet demand is structurally opaque (API calls off-chain) + open models = near-zero switching cost, so *named external customers* (Targon's Dippy) > inferred demand.

---

## OPEN ITEMS / NEXT (priority)

1. **Conviction-tag guard (TOP)** — exempt named real-utility holds from the bear `health_below_floor` cut so Gordie stops binning them to SN0. This run cut **Targon/NOVA/Score/MANTIS** (+ NIOME). Candidates to tag: Targon SN4, Minos SN107, Zipcode SN46, Score SN44, NOVA SN68 (decide on MANTIS SN123 — real vertical but h35; and NIOME SN55 — unclassified). Design: a `CONVICTION_TAGS` set + guard in the allocator's cut path; keep them held (or floored at a small %) through Bear instead of →SN0. Needs a deliberate "conviction ≠ momentum" rule so it doesn't just disable the bear book.
2. **Pool-floor decision (was awaiting your pick).** Engine `MIN_POOL_DEPTH=5.0` vs dashboard `FILTERS.min_pool_depth=15000` (gordie.html ~line 611) — hard divergence. Recommended: set engine to **5k now** (kills 1–2.5k dust, keeps the book; 10k would cut Minos ~7.7k / NIOME ~8.4k / MANTIS ~7.0k), align dashboard to match, step up later. **Still unpicked.**
3. **health_b calibration — NOW UNBLOCKED.** score_log is accumulating to `/data`. After a few runs land, tune `health_b=45` (sits on a flat plateau — untunable on one run) against stable real-data regimes. Use `score_calibration.py`.
4. **Gini 429 soft issue.** taostats stake_balance rate-limits ~4 holdings/run → n/a* until each gets one clean fetch into the cache. Options: wider pacing, an SDK/RPC Gini source, or accept it (cache fills over time). CoinGecko key already steadies GT (history), not Gini.
5. **Finish 4 subnet mappings** — Video, Basilica, Grail, Lead Power.
6. **Exit-design / persistence counter** — N-cycle confirmation before bear/health cuts; now feasible with persistent `/data` state. Pairs naturally with #1.
7. **Dashboard cleanup batch (cosmetic)** — stale `CURRENT_HOLDINGS=[0,4,51,62,64,68,75]` fallback (real book is 0/4/9/44/46/55/68/107/123), Scanner position-count, dead constants. (Carried from LIVE_STATE_14 #6.)

---

## Working-env notes

- Volume now mounted at **`/data`** on `spectacular-adaptation`; `score_log.csv` + `gini_cache.json` persist there. **Volumes are created from the canvas, not service Settings** in this Railway version (Ctrl+K → "volume"). Volume mounts at runtime (not build/pre-deploy), as root.
- Pull live files from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before edits — **Project copies are stale.** `api.taostats.io` and `api.geckoterminal.com` are **not** in the bash allowlist (probe GT locally: `python geckoterminal_fetch.py --netuid N`; Simon's pip = `python -m pip`). Dashboard `/api/*` is Basic-Auth — paste JSON/screenshots.
- Cron Start cmd: `python3 run_scoring.py --no-concentration --force-send --holdings-gini --holdings-history --candidates 15 --cost-basis`. Both services rebuild on any `main` push. Deploy = GitHub web whole-file replace → Railway auto-redeploy.
- Key files (live): `run_scoring.py` (~1096 lines), `subnet_scoring_engine.py` (v4, ~1321; SubnetScore carries ParameterScores p1_genie…p10_relative_perf + health_score/entry_score/markov_regime), `subnet_allocation.py` (~410; AllocationPolicy: health_b=45, deploy_bands, cut_on_bear_regime=True, new_entries_only_in_bull=True, max_weight_per_name=0.40), `geckoterminal_fetch.py`, `gini_fetch.py`, `score_calibration.py`, `gordie.html`, `serve.py`.

---

## Next session — start here
Pick up at **OPEN ITEM #1 (conviction-tag guard)** — it's the highest-value change and the 03:26 run is the proof case (Targon h39 / NOVA h38 / Score h42 / MANTIS h35 all cut to SN0). Decide the tag set + whether it's a hard hold or a small floor through Bear, then wire the guard into the allocator cut path. Also still owe you: **pool-floor pick (5k vs 10k)** and the **4 remaining subnet mappings**.
