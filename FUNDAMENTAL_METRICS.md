TAO Monitor — Fundamental Metrics Breakdown
Status: spec / reference (2026-06-22, builds on LS29 + the 2026-06-22 handoff).
Discipline: every claim carries a source; inferences are labelled `[inference]`; anything I couldn't verify is labelled `[unverified]` or `[guess]`. Numbers that need a live pull are marked `[pull: taostats]` rather than invented.
---
0. Premise (read first — it changes how the metrics are used)
Under dTAO, a subnet's alpha price is set by a constant-product AMM between TAO and the subnet's alpha token; staking is a swap of TAO into the pool, and net TAO flow is what drives the subnet's emission share — holders "vote with TAO." (Source: bittensor.com dTAO whitepaper; docs.learnbittensor.org "Understanding Subnets"; emission injection is now flow-based / "Taoflow", an EMA of net stake/unstake flows — cryptotimes.io TAO guide, Apr 2026.)
The consequence is uncomfortable and worth stating plainly: price is driven by flows, not directly by fundamentals. A subnet can produce nothing of value and still pump if TAO flows in; a genuinely useful subnet can bleed if flows leave. So fundamentals do not give you a price-prediction edge on any short horizon.
What fundamentals do give you:
A filter — they tell you what not to hold (recycling-only emissions, dead repos, thin pools, captured validators, operator risk).
A deterioration alarm — flows eventually chase real demand and flee dead subnets; fundamentals let you see the rot before the flow does.
This is consistent with the pivot in your handoff: the bot is a seatbelt; profit comes from the thesis. The metrics below are the seatbelt's sensors. None of them is a profit engine, and the doc does not pretend otherwise. The single most important fundamental — external paying demand — is also the least reliably measurable on Bittensor (self-reported metrics are pervasive and hard to verify independently — ownyourmind.ai, Apr 2026). That gap is the whole reason conviction is partly thesis and not a metrics readout.
---
1. Metric tiers by data quality
Tiered by how much you can trust the number, not by how interesting it sounds. The scoring rule (§4) forbids a Tier-3 claim from moving a conviction read upward without Tier-1/2 corroboration.
Tier 1 — Verifiable on-chain (trust these)
Pulled directly from chain / the official explorer. taostats has been the official block explorer since 2022 and exposes emissions, ownership, registration date, immunity, recycle volume, plus an API and the deepest historical on-chain data (Source: taostats.io/subnets).
Metric	What it measures	Source	Healthy / Unhealthy	Caveat
Emission share + trajectory	% of network TAO emission the subnet earns, and its slope over weeks	taostats API `[pull]`	Stable/rising share = market keeps voting for it. Falling = losing the vote	Share is relative; a subnet can fall purely because others rose
Net staking flow (Taoflow EMA)	Net TAO in/out of the subnet pool — the dTAO driver	taostats `[pull]`	Sustained inflow = demand for exposure. Sustained outflow = exit	Flow ≠ fundamentals; whales distort. Read the trend, not one print
Pool liquidity / depth + growth	TAO reserve backing the alpha pool	taostats `[pull]`; your engine already tracks `MIN/MAX_POOL_DEPTH`	Deep + growing = exitable. Thin = slippage trap on exit	Many alpha pools are thin enough that exits are expensive (dextools.io, May 2026) — size to the pool
Alpha price (in TAO)	Per-unit alpha price	taostats / tao.app; engine tracks `MAX_TOKEN_PRICE=0.15`	Use for sizing + the existing upper-bound filter	Price is an AMM artifact of flow, not a fundamental — do not read it as "value"
Holder concentration (Gini)	Top-wallet control of the alpha supply	tao.app concentration column (per your TAO_Monitor_Subnet_Reference); derivable from taostats holder data	Low = distributed. High (≥0.85) = manipulation/dump risk	Currently fails OPEN in your engine via 0.5 placeholder — LS29 TO-DO #3
Registration age / immunity	How long the subnet has existed; immunity status	taostats `[pull]`	Older + out of immunity = survived selection	New subnets are noisier, not necessarily worse
Recycle / burn rate	Whether emissions are being burned (subnet not running)	taostats "recycle volume" `[pull]`	~0 burn = active. Near-100% burn = the subnet isn't running	A subnet on "near 100% burn code" is effectively dead — exactly what Steeves cited for the dormant Covenant subnets (newsbtc via tradingview, Apr 2026)
Operator concentration	Share of emissions controlled by one team across subnets	derive from taostats ownership `[pull]`	Spread = resilient. Concentrated = single-operator risk	One team (Rayon Labs) captures ~¼ of all emissions (ownyourmind.ai, Apr 2026) — concentration risk lives above the wallet level too
Tier 2 — Externally verifiable usage (the best fundamental signal — but only exists for some subnets)
These are the closest thing to "is anyone actually paying for this," verifiable outside the subnet team's own dashboard.
Metric	What it measures	Source	Note
External throughput (inference subnets)	Real tokens served, measured by a third party	OpenRouter provider data	The canonical discipline lesson: Chutes/Rayon claimed ~160B tokens/day (Mar 2026) while OpenRouter measured ~8–12B/day, peak ~42B on 7 Feb — a large gap pointing the opposite way to the growth narrative (ownyourmind.ai, Apr 2026). External measurement beats self-report.
Dev activity	GitHub commit cadence across the subnet's repos	taostats Git Activity Tracker (added 25 May 2026), scores repos 0–100; active-development days = 40% of the score (coinmarketcap.com updates, Apr 2026)	Cheap, on-platform, automatable. Dead repo = dying fundamental. Some teams (SN64, SN4, SN5) submitted PRs to correct repo discovery — so a 0 can be a discovery miss, not abandonment; verify before penalising
External funding / institutional validation	Real capital + named partners putting reputation on the line	Press / filings	Verifiable examples: Manifold Labs (Targon/SN4) raised a $10.5M Series A and joined NVIDIA Inception (webopedia.com, Apr 2026). This is a genuine Tier-2 signal because it's externally checkable, unlike a roadmap
Tier 3 — Self-reported / thesis (label as guesses; never auto-promote a read)
Metric	Why it's Tier 3
Team-reported revenue / ARR	Single-source and often contradicted by external data. "Approaching $10M ARR" for Chutes was single-source (0xSammy, Apr 2026) and OpenRouter throughput pointed the other way (ownyourmind.ai). Targon's ~$10.4M projected annual revenue (IBS, Nov 2025) is a projection, not booked
Roadmap / milestone promises	Forward-looking. e.g. Teutonic's targeted 1-trillion-parameter run "late May" (abittensorjourney.com, Apr 2026) — a real catalyst if it lands, worthless until it does
"Real demand" narratives	The thing you most want to know and least able to verify. Treat as thesis input, not a metric
---
2. Operator & governance risk — now a first-class fundamental, not a footnote
This is where I'm extending your framing rather than restating it. The Covenant AI exit proved operator/governance risk can erase a thesis faster than any chart signal.
What happened (well-sourced, multiple outlets): on 9–10 April 2026 Covenant AI — operator of SN3 Templar, SN39 Basilica, SN81 Grail, and authors of the Covenant-72B model that drove TAO's March rally — publicly quit, dumped ~37,000 TAO (~$10.2M), and accused co-founder Jacob Steeves of "decentralization theatre." TAO fell ~25% in hours, ~$900M cap wiped, ~$9M longs liquidated (coinmarketcap.com, cryptotimes.io, 99bitcoins.com, kucoin.com, Apr 2026).
The antifragility counter-signal (also sourced): community miners restarted SN3/39/81 from open-source code with no central operator, ~70% of supply stayed staked through the disruption, and the revenue baseline held (blockonomi.com, abittensorjourney.com, Apr 2026). Teutonic (formerly Templar) is the SN3 successor — this reconciles your "SN3 Teutonic" watchlist label.
Monitorable operator-risk signals to add:
Burn rate spike (Tier 1) — a subnet going to near-100% burn = operator stopped running it.
Dev-activity cliff (Tier 2) — commits stop.
Founder behaviour — large visible alpha sells by the operator, public disputes. Hard to automate; worth a manual flag field per held name.
BIT-0011 "Conviction Mechanism" — protocol response to the exit: founders/stakers lock alpha across 30-day intervals to earn a conviction score; the highest-score staker gains subnet ownership; locked tokens can't exit until the interval closes (abittensorjourney.com, blockonomi.com, Apr 2026). This is directly relevant to your conviction pivot — it's a protocol-level encoding of "ownership through commitment, not founder discretion." Worth tracking whether your held subnets opt in, because it changes both rug-risk and exit liquidity. `[status: watch — confirm rollout/adoption before relying on it]`
---
3. Mapping to the existing engine
Metric	In TAO Monitor today?	Action
Alpha price + upper bound	✅ `MAX_TOKEN_PRICE=0.15`	keep
Pool depth bounds	✅ `MIN_POOL_DEPTH=5000 / MAX=500000`	keep
Concentration (Gini)	⚠️ present but fails OPEN (0.5 placeholder)	LS29 TO-DO #3: fail-closed + re-enable ≥0.85 hard filter
Trend (72 EMA, macro `TAO_WINDOW=14/THRESHOLD=0.07`)	✅	this is a market/chart sensor, not fundamental — keep as the seatbelt, don't confuse with conviction
Cost-basis P&L (gross `tao_in`)	✅ fixed LS29	keep
Emission share + trajectory	❌	add — cheap from taostats API
Net staking flow (Taoflow)	❌	add — cheap from taostats API; the single most decision-relevant new sensor
Recycle / burn rate	❌	add — cheap from taostats API; near-100% burn = dead-subnet alarm
Dev activity (git tracker 0–100)	❌	add — taostats git tracker; weekly cadence is enough
External throughput (OpenRouter)	❌	add only for inference subnets; manual/periodic — note OpenRouter is not on your bash allowlist, so this is a fetch-side job
Operator / founder-risk flag	❌	add a manual per-name field in `fundamentals.json` (you already stamp review dates)
Note on allowlist: taostats raw + API are already permitted in your stack; coingecko/gecko and taostats-adjacent price feeds are not (LS29 working-env notes). Pull fundamentals from the taostats API path, not scrapers.
---
4. How the tiers compose into a conviction read
The conviction tags already in `fundamentals.json` (ACCUMULATE / KEEP / WATCH / AVOID) become the output of a rule, not a gut call:
Tier 1 is a gate, not a booster. Any of these forces ≤ WATCH (or AVOID) regardless of thesis: Gini ≥ 0.85, near-100% burn, pool depth out of bounds, sustained emission-share collapse, dev-activity cliff. The seatbelt overrides the story.
Tier 2 is what earns ACCUMULATE/KEEP. Externally verified usage or real institutional capital is the only thing allowed to lift a name above WATCH.
Tier 3 cannot raise a read on its own. A revenue/roadmap claim with no Tier-1/2 corroboration sits at WATCH at best and gets a `[unverified]` stamp. This is the rule that would have caught the Chutes throughput gap.
Review dates already exist — extend them: each read carries the Tier-1/2 evidence it rests on and the date that evidence was checked. A read with only Tier-3 evidence past its review date auto-demotes to WATCH.
This keeps the discipline mechanical: a read can only be as strong as the most verifiable evidence under it.
---
5. Re-check of the 6 current reads against the framework
Sourced facts I could find per name. I did not pull live emissions/flow/pool/burn for your book — those are `[pull: taostats]` and should be filled before trusting any read. Identities sourced; numbers not invented.
Read	Subnet	Sourced fundamentals	Framework verdict
SN9 ACCUMULATE	model training/pre-training `[identity: DEXTools, partial]`	I have no Tier-2 external-usage or funding source for SN9	ACCUMULATE currently rests on thesis — demote to WATCH until a Tier-1 (rising emission share / net inflow) or Tier-2 (dev activity) signal is attached. `[pull required]`
SN4 KEEP	Targon (Manifold Labs)	Tier 2: $10.5M Series A + NVIDIA Inception (webopedia, Apr 2026); confidential/verifiable compute positioning; alpha ~0.062τ, liquidity mostly in-pool not on exchanges (webopedia). Tier 3: ~$10.4M projected rev (IBS, Nov 2025)	KEEP is grounded — strongest verifiable structure of your book. Watch pool liquidity for exitability
SN44 KEEP	Score (sports)	Tier 2-ish: DKING/$SIRE hedge-fund partnership via CreatorBid (ownyourmind, Apr 2026) — real but unproven ("if the performance materialises")	KEEP is borderline — the partnership is a Tier-3→2 candidate only once performance is external-verifiable. Hold, don't add, until then
SN68 WATCH	NOVA (AI drug/molecule discovery)	Thesis: high-upside real-world use case (ourcryptotalk, May 2026); validation gated on wet-lab results (ownyourmind)	WATCH is correct — upside is real but entirely forward-looking. Don't promote without external validation
SN3 WATCH	Teutonic (ex-Templar)	Governance shock (Covenant exit, §2); community-restarted, ~70% supply stayed staked; Teutonic 1T-param run targeted ~late May, timed to ETF window (multiple, Apr 2026)	WATCH is correct and the most information-rich name. Upside catalysts: 1T run landing + BIT-0011. Downside: operator/governance overhang unresolved. `[pull: did the May run land? burn rate? net flow since restart?]`
SN110 AVOID	"Green Compute" `[identity: LS29 only, unverified externally]`	No external Tier-2 source found	AVOID consistent with the framework (no verifiable usage). Keep as AVOID; if you want to revisit, a Tier-1 inflow/dev-activity signal is the trigger
---
6. Flags (need your call — not blocking)
Book mismatch — reconcile before the worked examples are trusted. The project header lists holdings SN0, SN4, SN51, SN62, SN64, SN68, SN75 (SN51 = Lium GPU; SN64 = Chutes/Rayon — both sourced). `fundamentals.json` covers a different set: SN9, SN4, SN44, SN68, SN3, SN110. Overlap is only SN4 + SN68. Which is the live book? The breakdown above followed `fundamentals.json` (the more recent artifact), but SN51/62/64/75 have no conviction read and SN9/44/3/110 may not be held. `[need: confirm live book]`
SN62 / SN75 identities — I couldn't source these. `[need: identity]` before they get reads.
TRAIL_PCT / confirm_hours — out of scope here (these are risk-control params, not fundamentals), but noting your flag stands: they remain unprincipled placeholders. The fundamentals work doesn't ground them; they need their own data-driven pass (your RL idea, or crude-and-safe for now).
OpenRouter fetch path — external-throughput verification needs a fetch route that isn't on the current bash allowlist. Decide: periodic manual check vs. a Railway-side fetcher.
---
7. Next data to pull (taostats API, one batch)
For each name in the confirmed book: emission share + 4-week slope · net staking flow (Taoflow) · pool depth + 4-week growth · recycle/burn rate · Gini · git-activity score. That single pull turns every read in §5 from thesis to gated. Until it lands, treat §5 verdicts as `[provisional]`.
---
Sources are named inline by outlet + date. dTAO mechanics: bittensor.com whitepaper, docs.learnbittensor.org. Metrics-that-matter framing: coingecko.com (May 2026), ownyourmind.ai (Apr 2026). Covenant/governance: coinmarketcap.com, cryptotimes.io, blockonomi.com, abittensorjourney.com (Apr 2026). taostats capabilities + git tracker: taostats.io, coinmarketcap.com updates (May 2026). Backtests/projections are historical or forward-looking, not guarantees.
