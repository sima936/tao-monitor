TAO Monitor — Strategy Spec: `/brief <netuid>` Telegram Command
Status: spec (2026-06-22, builds on FUNDAMENTAL_METRICS.md + LS29).
Discipline: the brief separates live on-chain fact from stored conviction from what you still must check by hand. It never fakes a Tier-2/3 signal. Anything I'm unsure of at build time is marked `[confirm]`.
---
Objective
Send `/brief 51` in Telegram → get a one-screen fundamental brief for SN51, on demand. Three labelled blocks: LIVE (auto on-chain gates), ON FILE (curated conviction read from `fundamentals.json`), VERIFY (the un-automatable Tier-2/3 checklist). Read-only, no scoring run, and built on the free Subtensor RPC so each call costs zero taostats credits.
It is honest by construction: it tells you whether a subnet is structurally sound (auto), what your recorded thesis is (curated), and what you still have to go look at yourself (manual). It does not emit a verdict it can't justify.
---
1. Command grammar
Fits the existing `tao_bot_listener.py` (alluring-smile) pattern, which today does exact-string dispatch against a `HANDLERS` dict with a single global 30s cooldown. Two changes needed:
1.1 — Parse command + args (today it exact-matches the whole line, so `/brief 51` falls through):
```python
# was: text = message.get("text","").strip().lower().split("@")[0]; if text not in HANDLERS: return
raw   = message.get("text", "").strip().split("@")[0]
parts = raw.split()
cmd   = parts[0].lower() if parts else ""
args  = parts[1:]
if cmd not in HANDLERS:
    return
...
HANDLERS[cmd](args)          # all handlers take (args); existing ones ignore it
```
1.2 — Don't let the 30s cooldown block read-only briefs. The 30s gate exists to stop double-triggering the heavy `/status` scoring run. `/brief` runs no scoring, so scope the heavy cooldown to heavy commands and give light ones a small anti-flood throttle:
```python
HEAVY = {"/status"}                      # triggers a scoring run
cooldown = COMMAND_COOLDOWN if cmd in HEAVY else 3
```
1.3 — Grammar:
`/brief <netuid>` — primary. Alias `/b`.
`netuid`: integer, validate `0 <= n <= MAX_NETUID` (`[confirm]` current cap — ~128, heading to 256). Tolerate `sn51`, `#51` by stripping non-digits.
Invalid/missing → reply with usage: `Usage: /brief <netuid>  e.g. /brief 4`.
v1 = one netuid per call. (Multi-netuid `/brief 4 68` is a v2 nicety.)
Add to `/help`: `/brief <n> — fundamental brief for a subnet`.
---
2. Data sources per gate (the honest feasibility map)
Live gates come from the free Subtensor SDK/RPC — the same `gini_fetch.py` ladder (SDK → RPC → taostats), which means adding `bittensor` or `websocket-client` to `requirements.txt` (it isn't there today, which is exactly why Gini was falling through to paid taostats). Prefer `websocket-client` (light) unless the SDK is wanted for other things; `bittensor` is heavy and slows Railway builds.
Gate	Live source	v1?	Good / bad (reuse live constants)
Alpha price (τ)	SDK `subnet(netuid).price` `[confirm attr]`	✅ free	`< MAX_TOKEN_PRICE (0.15)` = upside room; `≥` = already large, capped
Pool depth (TAO reserve)	SDK `subnet(netuid).tao_in` `[confirm attr]`	✅ free	inside `[MIN_POOL_DEPTH 5000, MAX_POOL_DEPTH 500000]`; below = thin/illiquid exit; above = capped
Emission share	SDK `subnet(netuid).emission` `[confirm attr]`	✅ free (level)	level now; trend needs your own snapshot log (v2) — falling share = losing the market's vote
Registration age	SDK subnet info / reg block `[confirm]`	✅ free	older = survived selection; brand-new = noisier, not worse
Concentration (Gini)	existing `gini_fetch` SDK metagraph	✅ free	show as FYI, not a gate — per your call it's unreliable; surfaced for context only
Burn / recycle	taostats recycle field; SDK path unclear	⚠️ v2	`~0` = active; near-100% = subnet not running / owner gone (the Covenant tell)
Net staking flow (Taoflow)	EMA of flows — needs periodic snapshots or taostats	⚠️ v2	inflow good; sustained outflow = exit
Dev activity (0–100)	taostats git tracker — no clean API `[confirm]`	⚠️ manual	active commits = alive; dead repo = dying

External throughput	OpenRouter (inference subnets only)	⚠️ manual	measured usage vs the team's claim
v1 ships the four clean SDK gates (price, pool depth, emission level, age) + Gini-as-FYI. The rest go in the VERIFY checklist as prompts, not fake numbers.
---
3. Curated layer — read `fundamentals.json`
Schema is already in place (`schema: 1`): `subnets[<netuid>] = {name, what, team, verdict, conviction, real_customer, traction, why, reviewed}`, with `_meta.verdicts`, `_meta.conviction`, `_meta.real_customer`, `_meta.staleness_days (45)`.
`/brief` loads it (cache on mtime) and, for the requested netuid:
On file → show `verdict` · `conviction` · `real_customer` · `what` · `team` · `why` · `reviewed`. If `today − reviewed > staleness_days` → append `⚠️ read is stale, re-review`.
Not on file → `No conviction read on file — run the §1 evaluation (FUNDAMENTAL_METRICS.md).`
Divergence flag (reuses `_meta.note`, v2 once net-flow exists): if live contradicts the stored verdict — e.g. `AVOID` + rising inflow (promo pump) or `KEEP` + a gate breaking — prepend `⚠️ live data diverges from your read`.
---
4. Output template (Telegram HTML)
`send()` already uses HTML. Keep under ~3500 chars (one message). Layout:
```
🔎 <b>SN{n} — {name}</b>   {verdict_emoji}{verdict}

<b>LIVE</b> (on-chain, {source})
 price   {price:.4f}τ   {✅/⚠️ vs 0.15}
 pool    {tao_in:,.0f}τ {✅/⚠️ vs 5k–500k}
 emission {emission_pct:.2f}%
 age      {days}d
 gini     {gini:.2f}  (FYI — not a gate)

<b>ON FILE</b>  (reviewed {reviewed}{ ⚠️ stale})
 {conviction} conviction · real customer: {real_customer}
 {what}
 team: {team}
 {why}

<b>VERIFY</b> (not automated)
 • dev activity — taostats git tracker
 • usage — OpenRouter (if inference) / product
 • burn/flow — confirm not recycling
```
Worked — held name on file (`/brief 4`):
```
🔎 SN4 — Targon   🟢 KEEP

LIVE (on-chain, rpc)
 price   0.0620τ   ✅ < 0.15
 pool    XX,XXXτ   ✅ in range
 emission X.XX%
 age      XXXd
 gini     0.4X  (FYI)

ON FILE  (reviewed 2026-06-22)
 High conviction · real customer: partial
 Confidential GPU compute for regulated industries (TVM)
 team: Manifold Labs — Rob Myers, James Woodman (ex-OTF)
 Real team, real scale; upside capped by size.

VERIFY (not automated)
 • dev activity — taostats git tracker
 • usage — Targon product / enterprise pipeline
 • burn/flow — confirm not recycling
```
Worked — unknown name not on file (`/brief 51`):
```
🔎 SN51 — Lium   ⚪ no read

LIVE (on-chain, rpc)
 price   0.0XXXτ   ✅ < 0.15
 pool    XX,XXXτ   ✅ in range
 emission X.XX%
 age      XXXd
 gini     0.XX  (FYI)

ON FILE
 No conviction read on file — run the §1 evaluation.

VERIFY (not automated)
 • dev activity — taostats git tracker
 • usage — OpenRouter throughput (GPU/compute subnet)
 • funding/governance — operator, partners
```
If the live read fails (RPC down): send the ON FILE block + `live read unavailable — try again shortly`. Never block the whole brief on the live layer.
---
5. Build / file touch-map
`brief.py` (new, importable + unit-testable): `build_brief(netuid) -> str`. Does: load fundamentals (cached) → live gates via Subtensor (reuse `gini_fetch`'s connection/ladder; cache an `all_subnets()` snapshot ~60s so repeat briefs don't re-hit RPC) → format the HTML template. Pure read, no keys, no scoring.
`tao_bot_listener.py`: dispatch parse (1.1), cooldown scope (1.2), `handle_brief(args)`, register `/brief` + `/b` in `HANDLERS`, add to `/help`. Update existing handlers to accept `(args)` and ignore it.
`requirements.txt`: add `websocket-client` (or `bittensor`) to enable the free RPC path — same change that takes Gini off paid taostats.
No new env vars. No wallet keys (read-only). No taostats credits on the happy path.
---
6. Scope — v1 vs later (don't fake the rest)
v1: command + dispatch + cooldown; LIVE = price / pool / emission level / age (+ Gini FYI) via free RPC; ON FILE from `fundamentals.json` with staleness flag; VERIFY checklist. Live-read failure degrades to ON FILE only.
v2: net-flow + burn (needs a periodic snapshot log) → enables the divergence flag; multi-netuid; optional dev-activity pull if the git-tracker exposes an API.
Won't fake: external demand, funding, governance — these stay in VERIFY as prompts. The brief's value is that it's honest about the line between measured and assumed.
