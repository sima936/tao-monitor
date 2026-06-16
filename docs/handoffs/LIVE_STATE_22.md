# LIVE_STATE_22 — TAO Monitor

**Session:** 2026-06-16 · **Repo:** sima936/tao-monitor `main` · **Builds on LS21** (gordie.html break-even indicator — non-Markov, no conflict) **and LS20** (regime/stop state).
**Status:** analysis + one file **staged, NOT deployed**. Live cron behaviour unchanged until Simon deploys. Stops still advisory.

---

## Net of this session

Lewis Jackson re-released the Markov hedge-fund method as **"Markov 2.0"** (rebuilt on Fable 5; prior was Opus 4.7). Reconciled its three documented fixes against our live regime stack, mapped exactly what consumes the regime post-LS20, and **upgraded `markov_regime.py`** with an opt-in non-overlap transition-matrix mode (the core Markov-2 fix). Default behaviour preserved; nothing changes on deploy until the macro call site opts in.

Target chosen by Simon: **TAO Monitor regime usage** (not GEO SCAFFOLD, not the full skill install).

---

## Live regime wiring (verified against repo `main`, not project copies)

- **Live path:** `run_scoring.py::compute_tao_macro_inline()` → imports `analyze`/`fetch_ticker` from `markov_regime.py`, runs on TAO-USD with the engine's own **`TAO_WINDOW=14`, `TAO_THRESHOLD=0.07`**, `hmm=False`, `min_train=60`. Emits `current_regime` + `signal`.
- **Fallback path:** `fetch_tao_macro.py` (legacy) — hardcodes window=20/threshold=0.05 (drifts from live 14/0.07); only used if inline fails → file → Unknown.
- **`markov_regime.py` in repo == project copy** (byte-identical), so the staged upgrade is a clean drop-in.

### What the Markov-2 matrix fix changes vs can't touch
- **`current_regime` (Bull/Bear/Sideways): UNCHANGED.** It is today's trailing-14-day return bucket, not a matrix output. So the entire entry gate (`MACRO_BEAR → no new entries`, `new_entries_only_in_bull`, `MARKOV_BEAR_REGIME` flag) is untouched.
- **`signal` / `bull_prob` / `bear_prob`: CHANGE.** Every live consumer of `signal` traced:
  1. **Axis-1 gross-exposure dial** `deployed_fraction(signal)` — **NEUTERED.** `deploy_bands=((-9.99,1.00),)` returns 100% for any signal (confirms LS20). The fix **cannot** reintroduce a cash dial. *(Stale comment at `subnet_scoring_engine.py:774-779` still describes this dial as live — flag for cleanup.)*
  2. **`macro_factor` → `entry_score`** (`subnet_scoring_engine.py:343-351`): signal used only in Bull (`70 + signal*30`) and Bear (`20 + signal*20`); Sideways flat 50. Entries gated to Bull, so the **only behavioural effect = nudging buy-candidate ranking while macro is Bull.**
  3. **Display only:** Telegram header + dashboard payload (`signal`/`bull_prob`/`bear_prob`, ~line 1310).

**Net blast radius: small and safe.** Cannot move exposure, cannot change the Bear/Sideways entry block; only reshapes buy-ranking in Bull + the displayed numbers.

---

## The three Markov-2 fixes, reconciled

**FIX 1 — stride sampling (autocorrelation).** = our overlap fix. Implemented in `markov_regime.py` as `build_transition_matrix(..., mode=..., window=...)` and `analyze(..., stickiness_mode=...)`, CLI `--stickiness {adjacent,nonoverlap}`. **Default `adjacent`** (unchanged).
- *Implementation choice:* the prompt's literal "stride = window length" subsamples to ~N/window transitions. On the live macro (1yr daily, window 14) that is **~25 transitions** → degenerate (Bear diagonal went to 0% in test). Our `nonoverlap` is the **phase-averaged** stride (every non-overlapping `t→t+window` pair = mean of all `window` stride offsets) → **~337 transitions**, same de-inflation (diag 84/89/85 → 29/63/39), all bars used. **Phase-averaged is the right call for TAO's short history.**
- *Semantic consequence:* matrix becomes ~window-ahead (≈14 trading days). Suits a slow macro overlay; would be wrong only if something treated `signal` as next-day (nothing does post-dial-removal).
- *Walk-forward left on `adjacent`* — not used by the macro (only `current_regime`+`signal` are read), so no effect. Adopting non-overlap as a *trading* step elsewhere requires moving the holding period to window units — separate deliberate change.

**FIX 2 — label-swap self-check.** Guards against the original shipping Bull/Bear swapped in a display. **Verified ours is clean:** `STATES=["Bear","Sideways","Bull"]`, `>+thr`→2(Bull), `<−thr`→0(Bear), macro propagates the string label. Optional: add a 3-line assertion self-test to enforce permanently.

**FIX 3 — FILTER vs STANDALONE.** Now defined. **TAO Monitor is FILTER** (regime gates entries; longs-only-in-Bull). We do NOT trade the differential directly. No change — confirms design.

**Enhanced states** (offered, not forced) = cluster on 20d return + ATR + relative volume ("violent" vs "sleepy" bear). **Recommendation: skip for the macro gate** — adds a clustering model + free params (overfitting surface) for marginal benefit on a slow gate. Parked.

---

## Deploy plan (NOT yet executed — Simon deploys via GitHub web)

Callee before caller (avoids mid-deploy crash window):
1. **`markov_regime.py`** — replace with staged upgrade (`/mnt/user-data/outputs/markov_regime.py`). Default `adjacent` → deploying it alone changes nothing.
2. **`run_scoring.py::compute_tao_macro_inline`** — add kwarg to the `analyze(...)` call:
   ```python
   r = analyze(close, source="TAO-inline",
               window=TAO_WINDOW, threshold=TAO_THRESHOLD,
               min_train=60, hmm=False,
               stickiness_mode="nonoverlap")   # Markov-2 FIX-1 (phase-avg stride)
   ```
3. **`fetch_tao_macro.py`** — same kwarg on its `analyze(...)` (fallback). Optionally align its 20/0.05 to `TAO_WINDOW`/`TAO_THRESHOLD` while there.
4. **(Optional, recommended) shadow log:** emit `signal_legacy` (adjacent) alongside corrected `signal` in the dashboard payload for a few cycles, watch divergence on real TAO before fully trusting — satisfies both the "show both matrices" honesty requirement and the validate-before-trust discipline. Not a fitted param, so no full Hermes/perturbation cycle needed.

---

## Cross-project applicability (asked this session)

`markov_regime.py` is asset-agnostic (`--ticker`/`--csv`, structured `analyze()` dict) → reuse is near-zero-cost. Best shared via the `markov-2-hedge-fund-method` Claude Code skill (install once, invoke anywhere) to avoid stale per-project copies.
- **GEO SCAFFOLD (parked):** strongest fit — the video's literal payload is a Pine Script + FILTER mode for it. Drop-in on un-park.
- **Casper (active, Hermes-driven):** strong fit. Directional strategy with PF/win-rate + trade logs. Integrate as FILTER (gate entries by regime). Cautions: (a) set window/threshold/stride to Casper's bar size; (b) phase-avg stride if the traded asset is young; (c) keep the filter threshold a **separate pre-registered tunable** — do NOT let Hermes joint-fit it with entry/exit.
- **Strategy Lab / QUANT (active):** likely useful as a research primitive; internals not yet reviewed.
- **Airdrop Farm:** poor fit — no directional price decision to gate.
- **Hermes:** the optimiser, not a host — would calibrate Markov filters across the others.

---

## Top of queue — next session
1. **Deploy decision** on the 3-file Markov-2 plan above (+ optional shadow log). Currently staged only.
2. **Get the actual `signal` divergence on real TAO** — sandbox can't reach yfinance/Yahoo; confirm corrected-vs-legacy via dashboard/Railway logs post-deploy.
3. **Stale comment cleanup** `subnet_scoring_engine.py:774-779` (Axis-1 dial described as live; it's neutered).
4. **LS21 not in project files** — upload `LIVE_STATE_21.md` so the chain is intact (gordie.html break-even session; non-Markov).
5. **Carried from LS20:** SN0-as-residual still triggers ADD (per-name cap / deploy ceiling needed; do NOT act on ADD); LS19 logging confirm; direct-chain wallet read; `parse_stake_balances` `=`→`+=`; Hermes re-point to `/data/outcome_log.csv`.

## Working-environment notes (carry forward)
- **Project file copies are stale** — pull live from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before editing.
- **bash allowlist:** GitHub raw + api **yes**; `api.taostats.io`/`api.geckoterminal.com`/`api.coingecko.com` **no**; **yfinance/Yahoo also not reachable** from sandbox — macro signal can't be live-tested here, verify post-deploy.
- **Deploy** = GitHub web whole-file replace (or surgical block); a push redeploys all three services; commit callee before caller on signature changes.
- **Live macro tuning:** `TAO_WINDOW=14`, `TAO_THRESHOLD=0.07` (inline path is source of truth, not fetch_tao_macro.py's 20/0.05).
- **Key constants (unchanged):** `TRAIL_PCT=0.25`, `STOP_PCT=0.30`, `OUTCOME_LOG_PATH=/data/outcome_log.csv`, `MAX_TOKEN_PRICE=0.15`, `MIN_POOL_DEPTH=5000`, `MAX_POOL_DEPTH=500000`, `confirm_hours=18`, `conviction_tags={4,107,46,44,68,123}`, `deploy_bands=((-9.99,1.00),)` (dial neutered).
- **Stops advisory** — detect/alert/log only; no keys in stack.
