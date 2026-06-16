# LIVE_STATE_21 — TAO Monitor

**Session:** 2026-06-15 (evening) · **Repo:** sima936/tao-monitor `main` · **Builds on LS20.**

---

## Net of this session

Shipped the **break-even / P&L-from-entry indicator** to the dashboard (the feature agreed back on 8–9 June that never got built), and **reconciled the orange-marker decision history** that's been costing repeated sessions. One file changed: `gordie.html`.

---

## The orange-marker decision — NOW CANONICAL (stop re-litigating)

Two separate decisions in two sessions had been conflated:

- **8–9 June:** agreed to surface **gain/loss from entry** (orange marker → break-even line, green above cost / red below). Cost-basis source resolved (tx-history reconstruction).
- **11 June:** agreed to replace the dead hand-set `TARGETS` relic with a **derived** allocation engine — which gave the target/drift marker a legitimate purpose again.

**What actually shipped historically:** the *cost-basis half* of the 8–9 plan landed as the COST / P&L table columns, but the **marker itself was never converted** — it stayed pointed at the derived target. So both decisions were "done" in different places and never reconciled. That's the churn.

**Resolution (this session):** keep BOTH, cleanly separated.
- **Allocation Chart** — orange ▼ marker = **derived target weight** (`allocation.positions[].target_weight`). Legitimate, not a relic. Confirmed in code: hand-set `TARGETS` is gone (gordie.html ~625–628, derived now).
- **NEW "Profit / Loss From Entry" chart** — grey break-even axis, green right = profit, red left = underwater, scale ±60%. This is the 8–9 June intent, finally delivered. Sits **above** the Allocation Chart.
- TARGET / DRIFT **table columns retained** for future rebalancing automation.

**Two Lewis-Jackson relics existed (the source of confusion):** (1) the macro cash dial — removed LS20; (2) the hardcoded `TARGETS` map — already replaced by the derived engine. The current orange tick is neither.

## gordie.html changes (deploy = whole-file replace on `main`)

- CSS: `.pnl-bar-row / .pnl-track / .pnl-fill / .pnl-breakeven / .pnl-val`.
- HTML: new `Profit / Loss From Entry` section + `#pnl-chart` container above Allocation Chart.
- JS (`renderPortfolio`): `pnlHtml` built per position from existing `invested` + `p.tao`; injected into `#pnl-chart`. No new data dependency — reuses the cost basis already on the page.

**Verify after deploy:** Portfolio tab shows the new P&L chart; Minos ≈ full green (+59.7%), MANTIS ≈ quarter red (−30.4%), names with no basis show a centred grey axis only.

---

## Top of queue — next session

1. **7-day per-position chart (sparkline) — needs a data source.** Dashboard only receives *current* subnet prices + single-number `pct_change_7d`; no daily series, and `serve.py` has no history proxy. Cleanest fix: have the cron emit the price window it already computes for EMA into `/api/score`, then render an SVG sparkline per row. Touches `run_scoring.py` — **not container-testable (taostats blocked on bash allowlist); verify on a live cron run.**
2. **Per-name cap / deploy ceiling (carried LS20 #1).** Minos/iota still size to 33% each → bogus ADD on parked residual. Until built: **do not act on ADD flags.**
3. Carried LS20: LS19 #1 logging confirm · direct-chain wallet read · `parse_stake_balances` `+=` fix · Hermes re-point to `/data/outcome_log.csv`.

## Carried operational notes

- **Unintentional cron run (this session) = non-event.** No keys in stack → no trades; latch de-dups outcome-log. The degraded `/status` read (Gini n/a, no cost basis, impossible 2-min swings) was rate-limited data APIs, NOT a state change. The **17:47 SA cron report is authoritative**; the `/status` one is not.
- Don't re-trigger the cron repeatedly — back-to-back runs race the rate-limited free data APIs into degraded reports.
- Project file copies stale — pull live from `raw.githubusercontent.com/sima936/tao-monitor/main`.
- Execution to-do (manual/advisory, carried): MANTIS unstake→SN0; Minos partial TP (ignore ADD); Zipcode exit on bear confirm ~23:00 UTC.
