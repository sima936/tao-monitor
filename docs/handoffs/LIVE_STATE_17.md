# LIVE_STATE_17 — TAO Monitor

**Session:** 2026-06-13 → 14 (late) · **Repo:** sima936/tao-monitor `main` · **Cron:** spectacular-adaptation (11:00/23:00 UTC)

---

## Net of this session

Shipped and **verified live** (5 commits + 1 Railway env var):

| # | Commit | File |
|---|--------|------|
| 1 | `feat(allocator): time-confirmation gate before bear/health cuts (OPEN #6)` | subnet_allocation.py |
| 2 | `test(allocator): smoke test for OPEN #6 confirmation gate` | test_confirm_gate.py (new) |
| 3 | `feat(scoring): thread cut_since + now_ts into allocator, persist streak` | run_scoring.py |
| 4 | `fix(report): exempt SN0 sink from holdings-fail + review/exit display` | subnet_scoring_engine.py |
| 5 | `fix(scoring): hard-exit main() so cron doesn't hang on a lingering thread` | run_scoring.py |
| — | Railway env var **`STATE_FILE=/data/scoring_state.json`** added on spectacular-adaptation | (volume) |

---

## OPEN #6 — CLOSED & verified

Time-confirmation gate. A **held** name that would EXIT on a gated reason (`bear_regime` / `health_below_floor`) is held at a **CV-style toehold** (de-risk now, TRIM not zero) until the cut-worthy state persists `confirm_hours` (**18h**), then exits. State persisted per-subnet in `scoring_state.json` (`cut_since`) on the `/data` volume.

- Policy: `confirm_hours=18.0`, `confirm_gates={bear_regime, health_below_floor}`. Inert when `now_ts` absent (/status path). Gate engages **held names only** — never creates a position for an un-held name.
- **Verification:** NIOME (SN55) ran `0h → 2h → 3h/18h` across a container swap **and** a reason-change (health_below_floor → bear_regime) without resetting — proves both `/data` persistence and the "consecutively cut-worthy for *any* gated reason" streak rule. iota (SN9) freshly flipped bear → `0h/18h`.
- **Behaviour note:** a conviction-tagged name in a real Bear regime now waits `confirm_hours` before exiting (was immediate). Tag still never exempts bear — just confirms first.

## SN0 display fix

SN0 (Root) is the cash sink, always price-filtered (root price 1.0 > `MAX_TOKEN_PRICE` 0.15). It was leaking into `💼 YOUR HOLDINGS` (`⛔ fail_price_too_high`) and `⚠️ REVIEW / EXIT` every cycle. Now skipped in both loops (`if h == 0: continue`) in `format_telegram_alert`. Allocator already used it purely as the sink; allocation plan still prints `SN0 85%` as the de-risk target. **Confirmed clean** in the 00:53 report.

## Cron exit-hang fix

`run_scoring.py main()` had no forced exit; the on-chain holdings fetchers (`--holdings-gini`/`--holdings-history`) leave a substrate websocket (non-daemon thread) alive that blocked normal exit → container sat "Running" ~1h+ after the report sent. `sys.stdout/stderr.flush()` + **`os._exit(0)`** at end of `main()`. All side effects flushed before exit. **Confirmed:** 1:47 AM run completed 5m37s, service returned to "Last run succeeded" with no lingering timer.

---

## Mappings (OPEN #5)

- Basilica = **SN39**, Grail = **SN81** (prior)
- **Video = SN85 Vidaio** (resolved this session): phonetic transcription of "Vidaio" → "Video"; it's *the* Bittensor video subnet; DSV/Siam covered its founder on the Revenue Search podcast. High confidence.
- **Hippias ≈ Hippius** (storage subnet) — likely; confirm SN number next session.
- **Lead Power** — still unresolved, no phonetic neighbour; needs the original interview audio.

## Portfolio state (00:53 UTC)

Macro **Bear −0.89** → deploy 15% / SN0 85% (~28.8τ). Book (8 held alpha + SN0 dust):
Minos (h63 🟢), Zipcode (h59), iota (h45 🔴Bear, **pending exit 0h/18h**), Score (h43), NOVA (h39), Targon (h39), MANTIS (h34), NIOME (h29 🔴Bear, **pending exit 3h/18h**).
- `conviction_tags = {4, 107, 46, 44, 68, 123}`. **NIOME (55) deliberately excluded** — which is why it's the one untagged name riding the confirmation gate.
- **SN0 Root dust = 0.0024τ ≈ £0.46.** Tx fee to move it > its value → **leave it** (confirmed via GORDIE portfolio view).

---

## Top of queue — next session

1. **Watch NIOME / iota confirm-exits.** At 12h cadence NIOME hits ≥18h on the ~23:00 (6/14) run → confirmed EXIT, *unless* health recovers > 45 (resets clock). iota similar, one cycle behind.
2. **GORDIE price-fetch** (NEW open). Red "Error: Price fetch failed" badge; was frozen since ~15:15 6/13, recovered after tonight's redeploy (scanning fresh again, targets populated). Badge may be sticky or a non-fatal partial fetch. Inspect `tao_gordie.py` / `gordie.html` / `serve.py`. Separate scanner from the allocation engine (GORDIE shows 3.9% pass vs engine's 90/129).
3. **Cron cadence decision** — still open: leave 12h, or move to 6h (`0 */6 * * *`). Code is time-based so correct either way; 6h gives a cleaner 18h confirmation (exits at the 18h mark vs ~24h at 12h).

## Backlog

- **OPEN #3** — `health_b` calibration. `score_log.csv` now accumulating to `/data` (volume verified working), so data is building.
- **OPEN #4** — Gini 429 (parked; 48h cache + SDK→RPC→taostats fallback absorbs it).
- **NEW — cash-sink modelling:** should the bear sink be root-stake or free unstaked TAO? Near-interchangeable (both ≈ TAO, no alpha vol); free TAO is more liquid, zero validator dependency. Context: 50%-root was the pre-dTAO Lewis-era play; post-dTAO root ≈ conservative cash, yield decays by design, **no principal risk** (TAO never leaves wallet).

## Working-environment notes (carry forward)

- Project file copies are **stale** — pull live from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before editing.
- bash allowlist: GitHub raw + api **yes**; `api.taostats.io` / `api.geckoterminal.com` **no**.
- Deploy = GitHub web (whole-file replace or surgical edit). Push redeploys **all three** services (alluring-smile, spectacular-adaptation, tao-monitor) from the same repo.
- GeckoTerminal free API = **30 calls/min** (per-minute pacing, *not* a daily cap) → cron cadence is unconstrained by it.
- Key constants: `MAX_TOKEN_PRICE=0.15`, `MIN_POOL_DEPTH=5000` (OPEN #2 floor), `MAX_POOL_DEPTH=500000`, `confirm_hours=18`.
- **MANTIS pool watch** — ~7k, only ~2k over the 5k floor; conviction guard won't rescue a pre-filter pool fail.
