# TAO Monitor — LIVE_STATE / Session Handoff

**Updated:** 2026-06-11 (eve) · **Repo:** sima936/tao-monitor (`main`)
**Railway:** bountiful-celebration / production
**This session:** allocator wire from LIVE_STATE_12 **deployed & confirmed live** (dashboard Target/Drift populate, cron logs `Allocation:`, 🧭 plan in digest). Then **fixed 3 problems** the first live bear digest exposed. **2 files changed** (`run_scoring.py`, `subnet_allocation.py`).

---

## What the first live cron (20:03, macro Bear −0.89) revealed

The wire worked, but the **bear allocation book was wrong**:
- It listed **10 mostly un-held names** (SN91/111/117/92/21/38/118/126/60 + Minos) at ~2% each, while cutting the real holdings.
- Most of those are **placeholder-data** subnets — the engine's pre-filters are loose (128/129 "pass"), so `ranked_by_health` is the whole network, not the real-data set the Opportunities tab already gates to ("12 REAL-DATA · 116 HIDDEN").
- It recommended **new entries in a Bear**, contradicting the macro line ("no new entries / capital preservation").
- Cosmetic: header said `deploy 15% · SN0 94%` — the 15%≠6%-actual gap came from the per-name cap being **of-deployed** (40%×15%=6%), shaving Minos and leaking to SN0.

Dashboard side was already fine (it only renders held positions: Minos target 1.5%, cuts 0%, red drift).

---

## CLOSED this session — 3 fixes

### 1. `run_scoring.py` — allocator sizes REAL-DATA only
Feed `compute_target_allocation` the enrichment set, not the whole network:
```python
real_data_ids = set(targets) | set(holdings)          # targets = holdings + watchlist + movers (got real history/Gini)
eligible_scored = [s for s in result.ranked_by_health if s.subnet_id in real_data_ids]
```
`targets` is the `--candidates`-enriched set (real history/Gini); `∪ holdings` guarantees a held name is never dropped. Placeholder-history subnets (untrustworthy health) no longer enter the book — they stay on the Opportunities tab.

### 2. `subnet_allocation.py` — no new entries outside Bull (`new_entries_only_in_bull=True`)
Capital preservation: under **Sideways/Bear/Unknown**, un-held names are held back from the book (hold/cut what you have; rotate in only on **Bull**). Needs holdings (`current_weight_by_id`); no-op on the holdings-less `/status` path (run() already feeds holdings-only there). Adds a digest note: `new entries suppressed (N held back)…`.

### 3. `subnet_allocation.py` — per-name cap is now of-ACCOUNT, not of-deployed
`per_name_cap_abs = policy.max_weight_per_name` (was `× f`). Keeps the cap **orthogonal to the Axis-1 dial**: still prevents ~100%-one-name in a bull (40% of account), but never shaves a lone green in a low-deploy bear — so `deploy%` and `SN0%` always reconcile.

**Net effect, same Bear −0.89 book:** `deploy 15% · SN0 85%` (consistent) · TARGET BOOK = **Minos 15%** (drift +7% → TRIM) · CUT = the other 7 holdings · note explains suppressed candidates. **Bull +0.55** (verified): candidates flow back in (Bitstarter/oneoneone/wgmi/gm + Minos), 20% each, ENTER actions, cap holds — rotation intact.

All three are one-field/one-line reverts (`new_entries_only_in_bull=False`; cap `× f`; pass `result.ranked_by_health` again).

---

## Verified (offline)
- Bear −0.89 vs the live book → Minos-only at 15%, header reconciles, 7 cut, suppressed-note present.
- Bull +0.55 → 4 candidates + Minos enter at 20% each (40%-of-account cap non-binding), failing holdings cut.
- Both files compile; full module graph imports; new policy field present; diffs vs deployed reviewed clean (no stray edits).
- **Not** verified live (no taostats/dashboard access) — verify on next SA cron Run.

---

## DEPLOY + VERIFY
1. GitHub web editor → whole-file replace **`run_scoring.py`** + **`subnet_allocation.py`** → commit. Railway auto-redeploys.
2. SA cron → **Run**. Expect in the 🧭 digest (macro still ≈ Bear −0.89): `deploy 15% · SN0 85%`, **TARGET BOOK = Minos ~15%**, the 7 holdings in CUT, and the "new entries suppressed" note. Console: `Allocation: deploy 15% · 1 green · N cut · SN0 85%`.
3. Dashboard unchanged behaviour (Target/Drift already correct from gordie.html).

---

## OPEN ITEMS / NEXT (priority)
1. **Remove engine `entry_score *= 0.3/0.5/0.7` macro switch** — dial replaces it (still deferred; load-bearing scorer, own verified change; doesn't touch `p2_macro`).
2. **Calibration watch (live):** `health_b=45` (only Minos survives now — is that too tight when macro turns?), `new_entries_only_in_bull` (want Sideways to allow entries too?), per-name cap 40%-of-account. All one-line `AllocationPolicy` knobs.
3. **Lone-survivor bear (Minos 15% of account):** if that's more single-green exposure than you want in a deep bear, the lever is the **dial bands** (drop deep-bear deploy below 15%) — not the cap. Tune in `AllocationPolicy.deploy_bands`.
4. `--candidates` 15 → 20–25 (more real-data names available to rotate into on Bull).
5. **Phase-3 rebalancer** — allocator output (`positions[].action` + drift deadband) is the input; add cooldown.
6. Dashboard `FILTERS` (min-pool 15,000τ) still diverges from engine pre-filters — cosmetic; this is the same looseness that caused problem #1 on the allocator side (now fixed there).

---

## Working-env notes
- Pull live files from `raw.githubusercontent.com/sima936/tao-monitor/main/<file>` before edits (Project copies stale). `api.taostats.io` not in bash allowlist; dashboard/`/api/*` are Basic-Auth (paste JSON/screenshots).
- Score-JSON `allocation` block contract unchanged (`macro_signal, macro_regime, deployed_fraction, sn0_target_weight, positions[], cut[], notes[]`); `serve.py`/`gordie.html` untouched this session.

---

## Commits this session (suggested)
1. `fix(alloc): size real-data subnets only — eligible = ranked_by_health ∩ (targets ∪ holdings); stop placeholder names entering the book`
2. `feat(alloc): no new entries outside Bull (new_entries_only_in_bull) — capital-preservation book = current holdings under Sideways/Bear`
3. `fix(alloc): per-name cap is of-account not of-deployed — keeps cap orthogonal to the dial; deploy% and SN0% reconcile`
