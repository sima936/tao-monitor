# TAO Monitor — LIVE_STATE / Session Handoff

**Updated:** 2026-06-13 (~15:30 UTC) · **Repo:** sima936/tao-monitor (`main`)
**Railway:** bountiful-celebration / production · cron = `spectacular-adaptation` (11:00/23:00 UTC), web = `tao-monitor` (serve.py / dashboard)
**This session:** **closed OPEN #1 (conviction-tag guard) and OPEN #2 (pool-floor / filter alignment), shipped the Operator's Playbook, and ran SN58 Handshake / Axolotl research.** 4 commits to `main`, all verified live on the 14:33 + 15:04 runs.

---

## CLOSED this session

### 1. Conviction-tag guard (OPEN #1) — LIVE & verified
- `subnet_allocation.py`: new `Tier.CONVICTION` ("CV", weight 0.5, below B). A tagged name cut on the **marginal health floor** drops to CV (~1% toehold) instead of →SN0. Scoped to `health_below_floor` ONLY — a tagged name in a real **Markov Bear regime still EXITs**. Held-only is enforced upstream by `new_entries_only_in_bull`.
- `CONVICTION_TAGS = frozenset({4, 107, 46, 44, 68, 123})` — Targon, Minos, Zipcode, Score, NOVA, MANTIS. **NIOME (55) excluded** (unmapped — keeps the set from collapsing into "just the book").
- **Tag on thesis, not price.** MANTIS (h35) included despite being weakest, because excluding on metrics = momentum-gating, the thing the guard exists to avoid.
- Verified (14:33 / 15:04 runs): NIOME = only full cut; Targon/Score/NOVA/MANTIS = CV 1% toeholds; greens shaved 5%→4% to fund them; **dial unchanged (deploy 15% / SN0 85%)**.
- Commit: `feat(allocator): conviction-tag guard for real-utility verticals`.

### 2. Pool floor + filter alignment (OPEN #2) — LIVE & verified
- Three-way divergence found & resolved. Engine was source of truth; aligned dashboard to it.
  - `subnet_scoring_engine.py` L64: `MIN_POOL_DEPTH` 5.0 → **5000.0** (MAX already 500000).
  - `gordie.html` FILTERS: `min_pool_depth` 15000→**5000**, `max_pool_depth` 150000→**500000**, `price_cap` 0.04→**0.15** (price cap was a *second* divergence found mid-edit — engine `MAX_TOKEN_PRICE`=0.15).
  - `tao_gordie.py` (min 2000) left alone — **legacy, not imported by run_scoring.py/serve.py**. Align for hygiene later if revived.
- Verified (15:04 run): passing filters **128 → 89** (5k floor culled ~39 thin/dust subnets); **book untouched** (smallest hold ~7k > 5k). Confirms 5k pick over 10k/15k (which would cut Minos/NIOME/MANTIS at 7–8.4k).
- Commits: `fix(filters): raise MIN_POOL_DEPTH 5.0 → 5k` + `fix(filters): align dashboard FILTERS to live engine (pool 5k/500k, price 0.15)`.

### 3. Operator's Playbook — committed
- `docs/OPERATORS_PLAYBOOK.md` — maps every Telegram/dashboard metric → buy/add/hold/trim/sell/avoid, with Bear + Bull worked walkthroughs. Built against **live** constants (price <0.15τ, pool 5000–500000τ). Commit: `docs: add operator's playbook (metric → action guide)`.

### 4. SN58 Handshake / Axolotl research (web-verified)
- **SN58 Handshake** = agent-first payments + inference marketplace (DRAIN = Polygon USDC payment channels; SN58 = the proof-of-service oracle scoring providers). One MCP server `drain-mcp`. **Pre-emission**, ~9.3k τ liquidity. Team: **Harry Jackson** (co-founder/GTM) + **Arthur** (dev, ex-hedge-fund). **Siam Kidd / DSV bought SN58** — Harry came up through DSV. Came via Macrocosmos BitStarter.
- **Axolotl** (marketplace provider id `axelot`) = SN58's trading agent. 10 models incl. market-snapshot, subnet-analyze, friction-quote, portfolio-analyze, opportunity-scan — i.e. **Gordie's surface, sold per-call.** It's the commercial sibling of our stack.
- **"Polkadot data" resolved:** one Axolotl skill connects **directly to the Bittensor (Substrate/Polkadot-SDK) chain** instead of lagged taostats. Relevant to OPEN #4 — but on analysis the gain for us is **coverage, not freshness** (our fast data is GeckoTerminal; taostats is only the Gini fallback, and Gini is slow-moving). **Parked** — only worth it when scoring Gini across many candidates for Bull entries.

---

## Current portfolio state (15:04 run)

**Macro: Bear (signal −0.89)** → deploy 15% · SN0 85% (29.4τ). Chain-read book = **9 subnets: SN0, 4, 9, 44, 46, 55, 68, 107, 123.** Everything shows TRIM (book is overweight for a 15%-deploy bear; correct posture).

| SN | Name | Health | Regime | Tier | Allocator action |
|----|------|--------|--------|------|------------------|
| 46 | Zipcode | 64 | 🟢Bull | A | 4% · TRIM (drift +4%) |
| 107 | Minos | 62 | 🟢Bull | A | 4% · **TAKE PROFIT +57%** |
| 9 | iota | 56 | ⚪Side | A | 4% · TRIM |
| 44 | Score | 42 | ⚪Side | **CV** | 1% · TRIM (conviction toehold) |
| 4 | Targon | 39 | ⚪Side | **CV** | 1% · TRIM |
| 68 | NOVA | 38 | ⚪Side | **CV** | 1% · TRIM |
| 123 | MANTIS | 35 | ⚪Side | **CV** | 1% · TRIM |
| 55 | NIOME | 37 | ⚪Side | exit | **CUT→SN0** (untagged, health_below_floor) |
| 0 | Root | — | — | — | fail_price_too_high (Kraken hotkey — leave) |

---

## Subnet mappings (OPEN #5) — partial

- **Basilica = SN39** (GPU compute marketplace) · **Grail = SN81** (decentralized AI training, ex-"Patrol").
- **Templar SN3 + Basilica SN39 + Grail SN81 = the Covenant operator trio** (SN3/39/81). That cluster took the ~10 Apr operator exit, no confirmed successor — so three of Siam's conviction names share one weakened operator.
- **Still unresolved:** **Video** (ambiguous — SN99 Neza / SN85 Vidaio / SN24 OMEGA; needs interview context) · **Lead Power** (no subnet by that name — transcription artifact; needs original audio).

---

## OPEN ITEMS / NEXT (priority)

1. **Exit-design / N-cycle confirmation (#6)** — every signal fires off a single cycle; no confirmation. Now feasible with persistent `/data`. Add an N-cycle counter before bear/health cuts. Pairs with the guard.
2. **health_b calibration (#3) — unblocked, accumulating.** `score_log.csv` writing to `/data`. After several stable runs, tune `health_b=45` via `score_calibration.py` (sits on a flat plateau — untunable on one run).
3. **Finish 2 mappings (#5)** — disambiguate **Video** (likely SN85 Vidaio or SN99 Neza) and **Lead Power** (re-check the interview transcript / audio).
4. **MANTIS pool watch (NEW caveat).** MANTIS ~7k pool has only ~2k headroom over the new 5k floor. The conviction guard does **NOT** rescue pre-filter fails (pool/price/Gini run before tiering) — if MANTIS pool drifts <5k it's excluded despite the tag.
5. **Gini 429 (#4)** — parked. Direct-chain Gini (per Axolotl) only buys coverage, not freshness; revisit when scoring Gini across many candidates. Note: `run_scoring.py` already has a holdings-Gini path (SDK→RPC→taostats fallback).
6. **Conviction-as-derived (NEW idea).** Long-term, make `conviction_tags` a *derived* property (utility/demand signal — e.g. named external customers like Targon's Dippy) rather than a manual ID list, to resist scope creep / bag-holding. Decide before the tag set grows.
7. **Dashboard cleanup batch (#7, cosmetic).** `run_scoring.py` L77 `CURRENT_HOLDINGS` fallback now updated to real book (good). Remaining: Scanner position-count, dead constants, and `gordie.html` L741 pool-"mid" calc now centres ~252k (cosmetic under-scoring of holdings on the Scanner — pass/fail is fixed).
8. **SN58/Axolotl (NEW watch).** Competitor/benchmark for Gordie; potential pay-per-call data source via `drain-mcp`. SN58 itself pre-emission — watch, not buy.

---

## Working-env notes

- **Pull live files from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before edits — Project copies are stale** (v4 engine ~1321 lines, run_scoring ~1095, gordie.html ~1224, tao_gordie.py ~1004 legacy). Confirmed this session: project copies had wrong MAX_TOKEN_PRICE (0.04 vs live 0.15), MAX_POOL_DEPTH (5000 vs 500000), sweet-spot scoring removed in v4.
- `api.taostats.io` and `api.geckoterminal.com` are **not** in the bash allowlist; `raw.githubusercontent.com` and `api.github.com` **are** (GitHub API rate-limits on the shared IP — unauthenticated).
- Deploy = GitHub web whole-file replace (or single-line edit) → Railway auto-redeploys both services on any `main` push. Force a cycle with **Run now** on `spectacular-adaptation`.
- Cron Start cmd: `python3 run_scoring.py --no-concentration --force-send --holdings-gini --holdings-history --candidates 15 --cost-basis`.
- Key live files: `run_scoring.py`, `subnet_scoring_engine.py` (v4), `subnet_allocation.py` (+ CONVICTION tier as of this session), `geckoterminal_fetch.py`, `gini_fetch.py`, `score_calibration.py`, `gordie.html`, `serve.py`. New: `docs/OPERATORS_PLAYBOOK.md`.

---

## Next session — start here
Pick up at **OPEN #6 (exit-design / N-cycle confirmation)** — highest-value remaining change, now unblocked by persistent `/data`. Then **#3 health_b calibration** once a few runs have accumulated in `score_log.csv`. Still owe: **Video + Lead Power mappings**, and a decision on **conviction-as-derived** before the tag set grows.
