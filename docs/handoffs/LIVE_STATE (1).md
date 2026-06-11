# TAO Monitor — Live State
## Last verified: June 1, 2026

> **Rule:** Update this file at the end of every session.
> **Rule:** Start every session by fetching live files from GitHub, not reading project files.
> **Rule:** Test on Infinity8 before pushing to GitHub.

---

## Integration test (run this first every session)

```bash
cd ~/tao-monitor && set -a && . ./.env && set +a && python3 run_scoring.py --no-concentration
```

Expected: clean output in ~1 second, 128/129 passing, Telegram sent or skipped with reason logged.
If this fails, fix it before building anything new.

---

## Infrastructure

| Component | Location | Purpose |
|-----------|----------|---------|
| Dashboard | Railway — tao-monitor | gordie.html via serve.py |
| Scoring cron | Railway — believable-contentment | run_scoring.py every 6h |
| Bot listener | Railway — alluring-smile | tao_bot_listener.py (persistent) |
| Hermes agent | Railway — spectacular-adaptation | tao_gordie.py (old system) |
| Macro cron | Infinity8 — crontab `0 */6` | fetch_tao_macro.py every 6h |
| Code repo | github.com/sima936/tao-monitor | Single source of truth |
| Dashboard URL | tao-monitor-production.up.railway.app | login: tao/bittensor |

---

## Crontab (Infinity8) — current state

```
0 9 * * *    python3 /home/simar/tao_enhanced_alerts.py daily
5 9 * * *    python3 /home/simar/tao_performance_tracker.py snapshot
10 9 * * *   /usr/bin/python3 /home/simar/tao_rebalancer.py
15 9 * * *   /usr/bin/python3 /home/simar/tao_enhanced_monitor.py
5 * * * *    /usr/bin/python3 /home/simar/tao_watchdog.py
15 8 * * *   /usr/bin/python3 /home/simar/tao_yield_cache.py
10 * * * *   /usr/bin/python3 /home/simar/tao_vtrust_monitor.py
0 */6 * * *  cd /home/simar/tao-monitor && set -a && . ./.env && set +a && python3 fetch_tao_macro.py >> /home/simar/tao-monitor/macro.log 2>&1
```

Note: scoring cron REMOVED from Infinity8 — now runs on Railway (believable-contentment).
Note: top 7 jobs run on Infinity8 (laptop) — will silently fail when laptop is closed.

---

## Key files (all in ~/tao-monitor on GitHub main)

| File | Purpose | Last changed |
|------|---------|-------------|
| run_scoring.py | Cron entry point. Loads macro, scores, sends Telegram. Paths now relative. | Jun 1 2026 |
| subnet_scoring_engine.py | v4 scoring engine. Genie hard filter DISABLED. GENIE_APPROACHING_THRESHOLD alert REMOVED. | Jun 1 2026 |
| taostats_fetch.py | Taostats API client. Synthetic history. Metagraph cap 20 subnets. | Jun 1 2026 |
| fetch_tao_macro.py | Fetches TAO-USD via yfinance, runs Markov, writes tao_macro.json. Path now relative. | Jun 1 2026 |
| tao_bot_listener.py | Telegram command listener. /status /macro /holdings /help | Jun 1 2026 |
| markov_regime.py | Markov regime detection library | May 31 2026 |
| gini_fetch.py | Bittensor SDK Gini fetch for holdings. Writes gini_cache.json | May 31 2026 |
| tao_gordie.py | Old Gordie agent (spectacular-adaptation). Separate system, still running. | May 2026 |
| serve.py | Railway web server — serves gordie.html | May 2026 |

---

## Current thresholds (subnet_scoring_engine.py)

```python
MAX_TOKEN_PRICE  = 0.15    # covers all holdings incl. SN64 Chutes
MIN_POOL_DEPTH   = 5.0
MAX_POOL_DEPTH   = 500000
MAX_GENIE_SCORE  = 0.85    # threshold exists but filter is COMMENTED OUT
```

**Genie filter: DISABLED** — stake-weighted Gini incompatible with tao.app Genie metric.

---

## Current holdings

| SN | Name | Health Score | Notes |
|----|------|-------------|-------|
| SN0 | Root | — | Kraken hotkey — always fails price filter (expected) |
| SN4 | Targon | 42 | ~0.054 TAO |
| SN51 | lium.io | 46 | ~0.051 TAO |
| SN62 | Ridges | 45 | ~0.017 TAO |
| SN64 | Chutes | 57 | ~0.10 TAO |
| SN68 | NOVA | 39 | ~0.022 TAO |
| SN75 | Hippius | 47 | ~0.021 TAO |

Watchlist: SN3 Teutonic

---

## Telegram commands (via alluring-smile on Railway)

| Command | Action |
|---------|--------|
| /status | Run full scoring cycle immediately, send update |
| /macro | Show TAO macro regime (Bear -0.890) |
| /holdings | Show all holdings health scores and 24h change |
| /help | List commands |

## Automatic alert behaviour

| Trigger | Action |
|---------|--------|
| New critical failure on a holding | Send immediately |
| Holding recovers from failure | Send immediately |
| 6 hours (cron digest) | Send via believable-contentment |
| No change | Silent |

---

## TAO macro state

- Current regime: **Bear**
- Signal: **-0.890**
- Bull: 1%, Bear: 90%
- Updated every 6h by fetch_tao_macro.py on Infinity8
- Written to relative path (tao_macro.json in script dir) — works on both Infinity8 and Railway

---

## Known issues / outstanding work

| Issue | Priority | Notes |
|-------|----------|-------|
| SN0 Root alert fires every cycle | Low | Needs exemption from price filter |
| Scores flat ~18-57/100 | Low | Bear macro suppressing entries — accurate |
| Synthetic price history | Medium | SQLite cache planned since May 30, not built |
| Entry price / P&L tracker | Medium | Planned since May 30, not built |
| Two dashboards | Low | gordie.html primary, index.html legacy |
| spectacular-adaptation overlap | Medium | Old Gordie duplicates new scoring system |
| Infinity8 crons depend on laptop | Medium | 7 jobs will fail when laptop closed |

---

## What NOT to do

- Do not read `/mnt/project/*.py` files in Claude — stale snapshots
- Do not build files in Claude and upload via GitHub web UI
- Do not push to GitHub without testing first
- Do not start a session without running the integration test

---

## Session checklist

**Start of session:**
1. Run integration test (above)
2. `cat ~/tao-monitor/macro.log | tail -5` — check last macro run
3. State objective in one sentence before writing any code

**End of session:**
1. Test changes manually
2. `git add -A && git commit -m "..." && git push`
3. Update this LIVE_STATE.md
