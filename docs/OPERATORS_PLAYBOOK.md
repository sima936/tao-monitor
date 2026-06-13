TAO Monitor — Operator's Playbook
Reading the Telegram + dashboard and turning metrics into actions
Grounded in the live engine: `subnet_scoring_engine.py` (pre-filters + scoring) and `subnet_allocation.py` (the dial + tiers + actions).
---
0. The mental model — two independent layers
Every signal is the product of two axes. Read both or you'll misjudge it.
Axis 1 — the macro dial. The single `TAO macro` signal sets how much of the whole account is deployed vs parked in SN0 / Root. Root is "cash."
Axis 2 — per-subnet selection. Among the deployed slice, which names you hold and at what size.
A best-in-class subnet in a deep Bear still only gets a small position, because Axis 1 caps total deployment. Conversely, a mediocre name can't sneak in during Bear because the entry gate is shut. Always ask: what is the dial, and what is this name's tier/drift?
---
1. Axis 1 — the macro dial
Header line: `🌐 TAO macro: <regime> (signal X.XX)`
Signal	Macro	Deploy	New entries?	Your posture
≥ +0.40	Strong Bull	100%	Yes	Fully deployed; Opportunities = buy list
+0.10 … +0.40	Bull	80%	Yes	Rotate into top-scored names
−0.10 … +0.10	Sideways	50%	No	Hold/cut what you have; half parked in SN0
−0.40 … −0.10	Mild Bear	25%	No	Trim toward SN0; defend
< −0.40	Deep Bear	15%	No	Capital preservation; mostly SN0
The rule that catches people: un-held names only enter on Bull. In Sideways/Bear the Opportunities/Top list is discovery only — not a buy trigger. In those regimes a failing name's capital is meant to go to Root, not into a new subnet.
---
2. Axis 2 — the per-subnet metrics (your holdings line)
Example: `SN123 MANTIS [35] ⚪Sideways 0.0038τ 24h:+0% 7d:+6% EMA:-12% Gini:0.76`
Field	Meaning	Healthy / Unhealthy
`[health 0-100]`	Composite score — the master number	≥70 A+, ≥55 A, ≥45 B, <45 = cut (or CV toehold if tagged)
`regime` 🟢/⚪/🔴	Per-subnet Markov state	🟢Bull good · ⚪Sideways neutral · 🔴Bear = hard exit regardless of health
`price τ`	Alpha token price	≥0.15τ → fail_price_too_high (limited upside; why Root always fails)
`24h / 7d %`	Momentum + freshness of the move	Up = constructive; bleeding 7d = warning
`EMA %`	Price vs 72-EMA (trend)	`+` above trend (healthy) · `−` below (weak); −12% = well below
`Gini`	Wallet concentration 0–1	≥0.85 = avoid/exit (manipulation risk) · >0.75 = warning · lower = healthier
`Gini n/a`	Not yet fetched this cycle (rate-limited)	Cache fills over runs — it is not a zero/0.50
---
3. Axis 2 — the allocation plan fields (the actual instructions)
Example: `A SN46 Zipcode — 5% (1.7τ) (h64/Bull) · drift +3% → TRIM`
Field	Meaning
`tier` A+/A/B/CV	Conviction-weighted size. A+ = 4× a B. CV = conviction toehold (~1%) for a tagged vertical rescued from the health cut
`target %`	What the allocator wants you at (fraction of account)
`drift %`	current − target. Outside ±3% deadband triggers an action; inside = hold
`action`	The verb you execute: enter / add / hold / trim / exit
---
4. The decision matrix — metric → action
Action	Trigger conditions	On-chain move
BUY (enter)	Bull macro + un-held name in target book / top Opportunities; passes all pre-filters; A/A+ tier; ideally 🟢Bull + EMA positive	Stake into it
ADD (scale in)	Held name, drift < −3% (below target)	Stake more, toward target
HOLD	Drift within ±3%, health ≥45, not 🔴Bear	Do nothing
TRIM (scale out)	Drift > +3% (overweight) OR take-profit (large +P&L well above EMA, e.g. Minos +57%) OR tagged name now at CV toehold	Unstake the excess
SELL (exit → SN0)	Untagged name health <45, OR any name 🔴Bear regime, OR Gini ≥0.85, OR price/pool filter fail	Unstake all → Root
AVOID (don't enter)	Un-held name failing any pre-filter (Gini ≥0.85 / price ≥0.15 / pool out of range) or 🔴Bear	Skip — even if it's on Opportunities
Pre-filter gates (a fail on ANY = excluded entirely): `price < 0.15τ` · `pool depth 5000–500000τ` · `Gini < 0.85`. These run before scoring — a name that fails never reaches the tiering above.
---
5. Worked walkthrough — BEAR cycle (today, signal −0.89)
Dial: 15% deployed, 85% in Root.
Greens (Zipcode/Minos/iota, A-tier) sized ~3.8% each.
Untagged failing (NIOME, h37) → SELL → SN0 (full exit).
Tagged failing (Targon/Score/NOVA/MANTIS) → CV toehold ~1%, shown as TRIM, not a full sell (conviction guard).
Minos → 🔻 TAKE PROFIT (+57% P&L, +26% over EMA) → trim into strength.
Opportunities list renders but entries are suppressed → discovery only; do not buy new names.
Your move: execute the trims/sells on-chain; surplus parks in Root. Next cron read re-derives the book from your wallet.
---
6. Worked walkthrough — BULL cycle (what changes when the signal flips +)
Dial opens: e.g. signal +0.5 → 100% deploy, Root → ~0%. Capital drains out of Root into the book.
Entry gate opens: un-held passing names become eligible.
Opportunities → target book: top-scored names (pass filters → tiered by health → conviction-weighted) up to max 10 enter the plan with ENTER actions.
The rotation is now real: a failing name EXITs, and its freed capital plus the de-parked Root capital fund the ENTER targets the allocator picked.
Caps still bind: no single name > 40% of the account; pool cap applies as you scale.
Your move: execute EXITs first, then ENTER/ADD into the named targets. Next cycle re-reads the chain and confirms convergence.
---
7. How state updates (no manual holdings edits)
Holdings are read on-chain each cron via a single `get_wallet_stakes()` on wallet `5HR3cMSE…` — the source of truth for which subnets, their weights, and P&L.
You only ever act on-chain (tao.app / btcli). The system is advisory — it does not execute.
After a manual trade, the book updates automatically on the next cron (11:00 / 23:00 UTC). To see it immediately, hit Run now on the `spectacular-adaptation` service.
---
8. Known caveats (so the dashboard doesn't mislead you)
Lag: the book reflects your wallet as of the last cron, not real-time. Force-refresh with Run now after trading.
Filters aligned (OPEN #2 resolved): engine and dashboard now share the same pre-filter gates — pool `5000–500000τ`, price `<0.15τ`, Gini `<0.85`. The dashboard Scanner's pass/fail now matches the held book; you can trust the filter readout.
Conviction guard does NOT rescue pre-filter fails: the tag only exempts the marginal health cut. A pool/price/Gini fail excludes a name regardless of tag (the pre-filter runs first). MANTIS (~7k pool) has only ~2k headroom over the 5k floor — if its pool drifts down it gets excluded despite being tagged. Watch it.
No N-cycle confirmation (OPEN #6): every signal fires off a single cycle. Sanity-check a one-cycle regime/health flip before acting.
Conviction tags are a manual list: `{4, 107, 46, 44, 68, 123}`. They suppress the full-sell signal for those names on the health floor only — a 🔴Bear regime still exits them.
---
Reference: TAO_Monitor_Subnet_Reference.md (Siam framework) · Subnet_Scoring_Engine_Integration_Design.md (scoring) · LIVE_STATE_15.md (current book + open items).
