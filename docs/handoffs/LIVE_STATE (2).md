# TAO Monitor — Live State
## Last verified: June 8, 2026 (corrected from stale June 1 version)

> **Rule:** Update this file at the end of every session.
> **Rule:** Start every session by fetching live files from GitHub, not reading project files.
> **Rule:** The previous LIVE_STATE had wrong service names, wrong holdings, and wrong
> cadence. Trust the Railway dashboard + `/status`, not memory.

---

## Railway topology (verified via dashboard + CLI, Jun 8)

Account: simart936@gmail.com · Workspace: sima936's Projects · Plan: HOBBY

**TWO projects exist:**

### Project: `bountiful-celebration` (the TAO Monitor stack) — env `production`
| Service | Runs | Schedule | Status | Role |
|---------|------|----------|--------|------|
| `tao-monitor` | `serve.py` (Procfile) | always-on web | Online | Dashboard — serves **gordie.html**. URL: tao-monitor-production.up.railway.app |
| `alluring-smile` | `python3 tao_bot_listener.py` | always-on | Online | On-demand bot (**Tao Seeker**). `/status` shells out to `run_scoring.py`. **Now runs the v4 report.** |
| `spectacular-adaptation` | `python3 tao_gordie.py once` | cron `0 11,23 * * *` (11:00 & 23:00 UTC) | Online (cron) | The **12h report** — STILL old momentum gordie (chaser). **Repurpose target.** |
| `believable-contentment` | `python3 run_scoring.py --no-concentration` | none | **Crashed** | v4 engine, but **0 variables** (no `TAOSTATS_API_KEY`) and **no cron** — misconfigured + redundant. **Leave crashed; delete later.** |

### Project: `hermes-trading` — 1 CLI-deployed service, Online
Not part of the TAO Monitor stack. Out of scope. Do not touch without checking what it is.

**Telegram bot:** Tao Seeker (replaced **Tao Watcher**, which was suspended for flooding — do not recreate multi-sender setups).

---

## Real on-chain holdings (from chain via `/status`, Jun 8)

`[0, 4, 9, 44, 46, 55, 68, 107, 123]`

| SN | Name | Notes |
|----|------|-------|
| SN0 | Root | Kraken hotkey — always fails price filter (expected) |
| SN4 | Targon | |
| SN9 | iota | |
| SN44 | Score | |
| SN46 | Zipcode | |
| SN55 | NIOME | |
| SN68 | NOVA | |
| SN107 | Minos | bear regime as of Jun 8 |
| SN123 | MANTIS | bear regime as of Jun 8 |

NOTE: old gordie (`tao_gordie.py`) has a **stale hardcoded** holdings list (`SN5/32/75`) that is WRONG. `/status` (v4) reads the real wallet dynamically — trust that.

---

## What shipped this session (Jun 8)

Two commits to `main`, both in `subnet_scoring_engine.py` (one function: `format_telegram_alert`):
1. `report: holdings-first, take-profit, honest Gini, non-chasing buys`
2. trim guard — only show TRIM when genuinely extended (≥ +15% over EMA)

The new report (drives BOTH `/status` and, once the cron is flipped, the 12h report):
- **Holdings first**, sorted weakest-health at top.
- **TRIM / TAKE PROFIT** — only fires when a holding is ≥ +15% above its EMA.
- **REVIEW / EXIT** — failed filters + bear-regime / below-EMA holdings.
- **Buy read is non-chasing** — CHASING-tagged subnets excluded; macro-gated (Bull = buy pullbacks, Sideways/Unknown = WATCH ONLY, Bear = capital preservation).
- **Honest Gini** — placeholder shown as `n/a*`, never a fake `0.50`.
- `⚠️CONCENTRATION` tag at Gini ≥ 0.85 (display only; hard filter stays disabled).

**Verified working:** `/status` on alluring-smile returns the new report on the real wallet, TRIM correctly empty in a flat market.

---

## Known issues / next steps (in priority order)

1. **Macro = "Unknown" on Railway (HIGH).** `run_scoring.py` reads `tao_macro.json`, which was produced by `fetch_tao_macro.py` on the (now-retired) Infinity8 cron. No file on Railway → macro Unknown → report permanently "WATCH ONLY", never surfaces Bull pullback buys. **Fix: make `run_scoring.py` compute the TAO macro inline** (import the markov/macro logic, fetch TAO price in-cycle) so it's self-contained — no cross-service file dependency (Railway services don't share a filesystem).
2. **Gini = `n/a*` placeholder (MED).** Real concentration isn't fetched on Railway (`gini_fetch.py` is SDK/Infinity8, writes to a local disk Railway can't read). Compute inline or build a transport.
3. **Phase 2 — flip the 12h cron to v4 (after #1).** Railway → `spectacular-adaptation` → Settings → Custom Start Command: `python3 tao_gordie.py once` → `python3 run_scoring.py --no-concentration`. Keep cron `0 11,23`. Pre-check: `railway variables --service spectacular-adaptation | grep -iE "TAOSTATS|TELEGRAM"`. Rollback: set command back to `python3 tao_gordie.py once`.
4. **Delete `believable-contentment`** — only after Phase 2 is stable for a few cycles.
5. **Untangle the Infinity8 `~/tao-monitor` clone** — 1 unpushed local commit, ~5 days stale, dirty working tree. NOT in the deploy path. Inspect the local commit, then re-sync or fresh-clone. **Do not push from it.**
6. **`git rm` the junk (later):** `__pycache__/*.pyc` (committed bytecode — can shadow source), `yield_cache.json` (208 KB), `patch_scoring_engine.py`, `session_patches.py`, and the retired `tao_dashboard.py` (had a now-dead leaked key).
7. **`price_cache.py` exists but isn't wired** into `run_scoring.py` — still on synthetic history.

---

## Rollback (everything done this session)

Revert the two commits on GitHub `main` → `/status` returns to the old format. `spectacular-adaptation` (12h cron) and the dashboard were never touched.

---

## What NOT to do

- Do NOT push from the dirty Infinity8 clone — apply changes via GitHub pencil editor (overwrite in place) or a fresh clone.
- Do NOT delete `believable-contentment` or any files yet.
- Do NOT flip the 12h cron until the macro feed works (else the scheduled report says "hold" forever).
- Do NOT revive `believable-contentment` — it's misconfigured; we repurpose `spectacular-adaptation` instead.
- Do NOT run multiple Telegram senders on one bot (that's what got Tao Watcher suspended).
