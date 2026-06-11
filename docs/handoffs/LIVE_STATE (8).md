# TAO Monitor — LIVE_STATE / Session Handoff

**Updated:** 2026-06-10 11:06 UTC
**Repo:** sima936/tao-monitor (default branch `main`)
**Railway project:** bountiful-celebration / production

---

## Architecture (unchanged)

Three Railway services, all from `main`:

- **tao-monitor** / `serve.py` — always-on dashboard (`gordie.html`), Basic Auth. Hosts `POST /api/ingest-score` → in-memory `LATEST_SCORE`, and `GET /api/score`. URL: https://tao-monitor-production.up.railway.app
- **alluring-smile** / `tao_bot_listener.py` — always-on Telegram bot; `/status`,`/holdings` shell out to `run_scoring.py` (60s fast path). **Do not add latency here.**
- **spectacular-adaptation (SA)** / `run_scoring.py` — cron `0 11,23 * * *` UTC. Posts 12h holdings report to Telegram + pushes v4 result to dashboard.

**Start commands live in Railway service Settings, NOT the repo.** `Procfile` only has `web: python3 serve.py`; `railway.toml` only sets the nixpacks builder. SA's Custom Start Command is now:
```
python3 run_scoring.py --no-concentration --force-send --holdings-gini --holdings-history --candidates 15
```

Apply repo changes via the **GitHub web editor**. Railway cron is ephemeral (no disk); Infinity8 (SSH box) holds the gini cache but it's unreachable from Railway, so Railway falls back to in-process gini fetch.

**Holdings (on-chain authoritative):** [0,4,9,44,46,55,68,107,123] ≈34τ — SN0 Root, SN4 Targon, SN9 iota, SN44 Score, SN46 Zipcode, SN55 NIOME, SN68 NOVA, SN107 Minos, SN123 MANTIS. **Watchlist:** SN3 Teutonic.

**Engine:** v4 `subnet_scoring_engine.py`, 10-gate scorer → `entry_score` (macro-scaled) + `health_score`. TAO macro currently **Bear** (signal −0.89) → entry_score ×0.3 → all entries suppressed ("MACRO_BEAR — no new entries"). Taostats **free tier = 5 calls/min (~12.5s/call)** — confirmed.

---

## CLOSED this session

### ✅ #1 — `pct_from_recent_high` sentinel bug (CLOSED + verified live)
Root cause: `p4_score_pullback` short-circuited `return 50.0, 0.0` on `<10` bars (every subnet, since synthetic series = 7–9 bars), so `pct_from_recent_high=0.0` everywhere, and `detect_take_profit`'s `abs(pct)<0.03` fired **AT_RECENT_HIGH on all 129 subnets** including ones down −72%. Sentinel collision (0.0 = both "couldn't compute" and "at high").
Fix: use `None` sentinel — 4 edits to `subnet_scoring_engine.py` (returns → `None`; guards `if pct is not None`). Committed to `main`, verified on `/api/score`: false AT_RECENT_HIGH gone from bleeders; only genuine signals remain. The 3 remaining `return 50.0, 0.0` lines are in other functions (p3, p7) — correct to leave.

### ✅ #7 — Bounded candidate real-data enrichment (SHIPPED + verified)
Added to `run_scoring.py`: `select_candidates()` + `_recent_7d_change()` + constants (`WATCHLIST=[3]`, `CANDIDATE_BUDGET`, `CAND_MIN_POOL=50`, `CAND_MAX_PRICE=0.10`); new `--candidates N` flag (default 0 = old holdings-only behaviour, backward-compatible). `run()` now enriches **holdings + watchlist + top 7d movers** (liquid, sane-priced, non-deprecated) up to budget, instead of holdings only.
Committed to `main`; SA start command updated to `--candidates 15`; deployed ACTIVE.
**Verified on the 10:58 UTC run:** 14 subnets at `data_maturity:100` = 8 holdings + SN3 + 5 movers (92,111,95,105,23), all with real transition matrices / `genie_score_raw` / varied `p4_pullback` / populated `pct_from_recent_high`. SN0 correctly excluded by price prefilter.

---

## KEY FINDING from #7 (drives next work)

Enriching only candidates makes the **board ranking inconsistent**: the un-enriched majority keep optimistic placeholder scores (`genie 0.5 → p1 41.2`, degenerate matrix → fake "Bull" → `p6 100`, `data_maturity 10`) and **outrank every truthfully-scored subnet**. Top ~80 ranks are all synthetic (Bitstarter 17.6, Liquidity, Poker44, Zeus…); first real subnet is Zipcode at `entry_score` 9.2. **Sorting the whole board by `entry_score` is currently meaningless.**

Also: real-history EMA reveals the momentum movers are crashed-then-bouncing tokens (`pct_from_ema` −0.94 to −0.998) — the engine correctly demotes them (entry 6–9, CHASING flags). But that −0.96 cluster means the EMA is anchored to launch-era prices and isn't tactically useful.

---

## OPEN ITEMS / NEXT

1. **Part 3 — gordie.html Opportunities panel**, rendering `entry_score` top-N **gated to `data_maturity == 100`** (real-data only). This is now the priority — it both delivers the dashboard feature AND solves the mixed-board problem above. Don't render the synthetic majority as opportunities.
2. **#4 — cost-basis / break-even** tracking. Still the only thing that enables "sell for *decent profit*" judgments (system has no cost basis today). Required for real exit decisions.
3. **EMA window refinement** — shorten the EMA period (e.g. 20–30d) or sanity-cap `pct_from_ema` so p3/p7 are tactically meaningful. Eyeball raw `price_history` for one enriched subnet first.
4. **Taostats read-timeout resilience** (retry/backoff in `TaostatsClient.get`). Per-subnet history fetch already continues-on-error, so #7@15 is safe, but **do this before raising `--candidates` toward 25** — more calls = more timeout exposure.
5. **Minor:** SN0 eats a candidate budget slot (excluded downstream but counted in `forced`). Strip SN0 from forced, or just bump budget by 1.
6. **Known intermittency:** SN55 NIOME Gini occasionally n/a (flaky metagraph fetch) — resolved this run.

---

## ACTIONABLE SNAPSHOT (10:58 UTC, macro Bear → entries blocked)

- **Minos (SN107, holding):** +68% 7d, at recent high, "good exit zone" — only holding flashing an exit signal.
- **NIOME (SN55, holding):** weakest — health 24.9, Bear regime.
- Other holdings cluster health 35–41, mostly Sideways. Targon/iota/Score/NOVA/MANTIS/Zipcode all real-data now.

## Working-env notes
Free-tier confirmed 5/min. `/api/score` is Basic-Auth (Claude can't fetch — paste it). `api.taostats.io` not in Claude's bash allowlist (can't test API directly); `raw.githubusercontent.com` IS allowed (can curl live repo files). Patched `run_scoring.py` deliverable staged at `/mnt/user-data/outputs/run_scoring.py`.
