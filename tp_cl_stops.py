"""Take-profit / cut-loss stop layer — STEP 1 (advisory only, no signing keys).

Drop-in module imported by run_scoring.py. Does four things, all read-only:

  1. evaluate_stops(...)   peak high-water tracking + trailing-stop + hard-stop
                           calc per holding. Pure function — no I/O, no network.
  2. append_outcome_log()  one CSV row per stop/TP/exit/entry EVENT. This is the
                           Hermes feed (generalises score_log.csv, which stays
                           the per-cycle snapshot). fwd_return_* backfilled later.
  3. format_stop_alert()   the dedicated 🚨 Telegram ping, separate from the
                           digest, with the exact unstake amount.
  4. (constants)           TRAIL_PCT / STOP_PCT from env — placeholders, Simon's
                           risk call pre-Hermes. Migrate to strategy.yaml when
                           Hermes comes online (these are the Hermes-tunable
                           surface, nothing else in here is).

Design decisions (see STRATEGY_take_profit_cut_loss.md):
  * Peak is tracked on token_price = peak value PER UNIT. This is the
    sizing-invariant form of the spec's "peak value since entry": adding to a
    position doesn't move price so the high-water is unaffected; trimming
    doesn't false-trigger the trail (raw TAO position value would).
  * Hard stop is measured on P&L vs cost basis (already computed on the cron),
    so it catches gap-downs the trail can miss.
  * Hard stop takes priority over the trail (it's the backstop).
  * Both BYPASS the 18h cut_since gate — a stop is a same-cycle hard exit.
  * A `stop_fired` latch de-dups: we alert ONCE per standing breach, not every
    6h while Simon hasn't acted. The latch clears on recovery or exit.
  * SN0 (root/cash leg) is skipped — house money, no cost basis, undefined P&L.
"""

from __future__ import annotations

import csv
import os
import time
from pathlib import Path

# ── Hermes-tunable surface (the ONLY tunable knobs here). Placeholders until
#    calibrated — wide, because Bittensor alpha swings 10-20% intraday and a
#    tight trail whipsaws. Simon owns these pre-Hermes; they move to
#    strategy.yaml's risk_management block when Hermes goes live. ──────────────
TRAIL_PCT = float(os.environ.get("TRAIL_PCT", "0.25"))   # exit if >25% off peak
STOP_PCT = float(os.environ.get("STOP_PCT", "0.30"))     # exit if >30% vs cost

# ── LAUNCH_SCOUT (Part C of the STRATEGY spec's take-profit ladder).
# Flags a position as a scout entry — meant to catch bonding-curve inception
# plays on brand-new subnets. Only launch_scout positions run the TP ladder;
# normal ADDs use the trailing stop alone. Env-tunable per Simon's calibration
# authority; Hermes-tunable once forward data accumulates. ───────────────────
LAUNCH_SCOUT_MAX_ENTRY_TAO = float(os.environ.get("LAUNCH_SCOUT_MAX_ENTRY_TAO", "1.0"))
LAUNCH_SCOUT_WINDOW_DAYS = float(os.environ.get("LAUNCH_SCOUT_WINDOW_DAYS", "7"))
TP_TRIM_LADDER = [50.0, 100.0]   # pnl_pct rungs (percent)
TP_TRIM_FRACTION = float(os.environ.get("TP_TRIM_FRACTION", "0.25"))  # 25% per rung

def _default_log_path(filename: str) -> Path:
    """Prefer Railway persistent volume (/data) if it's writable, fall back
    to the script's directory. Files on /app get wiped between cron invocations
    on Railway; files on /data persist. Local dev works either way."""
    data = Path("/data")
    if data.exists() and os.access(data, os.W_OK):
        return data / filename
    return Path(__file__).parent / filename


OUTCOME_LOG_PATH = Path(
    os.environ.get(
        "OUTCOME_LOG_PATH",
        str(_default_log_path("outcome_log.csv")),
    )
)

# Schema is the spec's outcome-log contract. fwd_return_* are blank at write
# time and joined per-event later (no lookahead), exactly like score_log.csv.
# The trailing three columns (trim_rung_pct, trim_size_tao, is_launch_scout)
# are only populated on TP_TRIM / ENTRY events — legacy STOP rows leave them
# blank. DictWriter's extrasaction="ignore" means old rows stay parseable.
OUTCOME_FIELDS = [
    "event_ts", "netuid", "name", "event_type",
    "entry_cost_tao", "peak_value_tao", "exit_value_tao", "pnl_pct",
    "regime_at_event", "health_at_event", "trail_pct_used", "stop_pct_used",
    "fwd_return_1d", "fwd_return_7d", "fwd_return_14d",
    "trim_rung_pct", "trim_size_tao", "is_launch_scout",
]

EVENT_TYPES = {"TRAIL_STOP", "HARD_STOP", "REGIME_EXIT", "TP_TRIM", "ENTRY"}


def evaluate_stops(
    holdings,
    price_by_id: dict,        # {netuid: current token_price in TAO}
    bal_by_id: dict,          # {netuid: current stake value in TAO}
    cost_by_id: dict,         # {netuid: tao_invested (cost basis)}
    pnl_by_id: dict | None,   # {netuid: P&L fraction vs cost basis} or None
    regime_by_id: dict,       # {netuid: regime label}
    health_by_id: dict,       # {netuid: health score}
    name_by_id: dict,         # {netuid: name}
    peak_price: dict,         # {netuid: high-water token_price} — not mutated
    stop_fired: dict,         # {netuid: latched event_type} — not mutated
    trail_pct: float = TRAIL_PCT,
    stop_pct: float = STOP_PCT,
    skip_ids=(0,),            # SN0 root / cash leg
    now_ts: float | None = None,
):
    """Pure stop calc. Returns (events, new_peak_price, new_stop_fired).

    Inputs are not mutated — copies are returned so the caller persists them to
    state. `events` is a list of outcome-log rows ready for append_outcome_log.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    held = [h for h in holdings if h not in set(skip_ids)]
    held_set = set(held)

    # Start from prior state, but prune anything no longer held so re-entry
    # starts with a fresh high-water and no stale latch.
    peak = {int(k): float(v) for k, v in (peak_price or {}).items() if int(k) in held_set}
    fired = {int(k): v for k, v in (stop_fired or {}).items() if int(k) in held_set}
    pnl_by_id = pnl_by_id or {}

    events = []
    for h in held:
        price = price_by_id.get(h)
        if not price or price <= 0:
            continue  # no real price this cycle — leave peak/latch untouched

        # High-water update (per-unit peak — invariant to adds/trims).
        cur_peak = max(peak.get(h, price), price)
        peak[h] = cur_peak

        pnl = pnl_by_id.get(h)
        trail_dd = (cur_peak - price) / cur_peak if cur_peak else 0.0
        trail_breach = trail_dd >= trail_pct
        hard_breach = (pnl is not None) and (pnl <= -stop_pct)

        # Hard stop is the backstop → priority over the trail.
        breach = "HARD_STOP" if hard_breach else ("TRAIL_STOP" if trail_breach else None)
        latched = fired.get(h)

        if breach is None:
            fired.pop(h, None)   # recovered → clear latch, re-arm for next time
            continue
        if latched == breach:
            continue             # already alerted this standing breach — no spam

        # New (or escalated TRAIL→HARD) breach → emit an event + latch it.
        bal = bal_by_id.get(h)
        cost = cost_by_id.get(h)
        peak_value = (bal * cur_peak / price) if (bal and price) else None
        events.append({
            "event_ts": round(now_ts, 0),
            "netuid": h,
            "name": name_by_id.get(h, f"SN{h}"),
            "event_type": breach,
            "entry_cost_tao": round(cost, 6) if cost is not None else "",
            "peak_value_tao": round(peak_value, 6) if peak_value is not None else "",
            "exit_value_tao": round(bal, 6) if bal is not None else "",
            "pnl_pct": round(pnl * 100, 2) if pnl is not None else "",
            "regime_at_event": str(regime_by_id.get(h, "")),
            "health_at_event": round(float(health_by_id[h]), 1) if h in health_by_id else "",
            "trail_pct_used": trail_pct,
            "stop_pct_used": stop_pct,
            "fwd_return_1d": "",
            "fwd_return_7d": "",
            "fwd_return_14d": "",
        })
        fired[h] = breach

    return events, peak, fired


def append_outcome_log(rows: list[dict], path: Path = OUTCOME_LOG_PATH) -> None:
    """Append event rows to the outcome log. Non-fatal on failure (advisory).

    Schema-migration-safe: if the file exists with an OLD header (fewer
    columns than the current OUTCOME_FIELDS), the old file is renamed to
    `.legacy-<timestamp>` and a fresh log is started with the new header.
    This is a one-time cost the first time the expanded schema deploys —
    subsequent runs append normally. Legacy data is preserved on disk.
    """
    if not rows:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Header-mismatch migration.
        if path.exists() and path.stat().st_size > 0:
            try:
                with path.open("r", newline="") as fh:
                    first_line = fh.readline()
                existing_header = [c.strip() for c in first_line.rstrip("\n").split(",")]
                # Only migrate when the existing header is a strict prefix /
                # subset of OUTCOME_FIELDS (i.e. an older, shorter schema).
                # Different orderings or unknown columns are left alone to
                # avoid clobbering an unexpected file.
                if set(existing_header) < set(OUTCOME_FIELDS):
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    legacy = path.with_suffix(path.suffix + f".legacy-{stamp}")
                    path.rename(legacy)
            except Exception:
                pass  # if the header check itself fails, fall through and append
        new_file = (not path.exists()) or path.stat().st_size == 0
        with path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=OUTCOME_FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in OUTCOME_FIELDS})
    except Exception:
        pass  # never let the log kill the cycle


_ICON = {"HARD_STOP": "🛑", "TRAIL_STOP": "🔻", "REGIME_EXIT": "⚠️", "TP_TRIM": "✂️"}


# ── Dial log — one row per cron capturing the macro signal → deployed-fraction
#    decision. Separate from outcome_log (which is stop/TP/exit EVENTS); the dial
#    is a continuous per-cron exposure call, not an event. fwd_return_* blank at
#    write time, backfilled later (no lookahead) so Hermes can score the signal's
#    information coefficient on the deploy ramp. ──────────────────────────────
DIAL_LOG_PATH = Path(
    os.environ.get(
        "DIAL_LOG_PATH",
        str(Path(__file__).parent / "dial_log.csv"),
    )
)

DIAL_FIELDS = [
    "dial_ts", "regime", "signal", "deployed_fraction", "sn0_target_weight",
    "account_tao_staked", "free_tao",
    "fwd_return_1d", "fwd_return_7d", "fwd_return_14d",
]


def append_dial_log(row: dict, path: Path = DIAL_LOG_PATH) -> None:
    """Append one per-cron dial decision for pre-Hermes calibration. Non-fatal."""
    if not row:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = (not path.exists()) or path.stat().st_size == 0
        with path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=DIAL_FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in DIAL_FIELDS})
    except Exception:
        pass  # never let the log kill the cycle


def detect_entries(
    holdings,
    bal_by_id: dict,
    cost_by_id: dict,
    name_by_id: dict,
    prev_entries: dict,               # {netuid: {entry_ts, entry_cost, launch_scout, trims_fired}}
    first_seen_ts_by_id: dict,        # {netuid: first_seen_epoch} from new-subnet detector
    now_ts: float | None = None,
    launch_scout_max_entry: float = LAUNCH_SCOUT_MAX_ENTRY_TAO,
    launch_scout_window_days: float = LAUNCH_SCOUT_WINDOW_DAYS,
    skip_ids=(0,),
):
    """Track new position entries + flag launch_scout eligibility.

    A new position is a netuid we now hold that wasn't in prev_entries. Records
    entry_ts, entry_cost, and sets launch_scout=True iff BOTH:
      • entry_cost ≤ launch_scout_max_entry (small position — a scout, not a bet)
      • subnet netuid was first seen ≤ launch_scout_window_days ago (fresh slot,
        catches bonding-curve inception; misses no-op adds on established names)

    A legacy netuid with no first_seen_ts (i.e. one we saw before the detector
    started stamping timestamps) never qualifies — correct: it's not a "launch".

    Returns (entries, entry_events). Exited positions are pruned from entries
    so a re-entry starts fresh. Inputs are not mutated.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    skip = set(skip_ids)
    held_set = {int(h) for h in holdings
                if int(h) not in skip and (bal_by_id.get(h, 0) or 0) > 0.001}
    entries = {int(k): dict(v) for k, v in (prev_entries or {}).items()}

    # Prune exited positions.
    for nid in list(entries.keys()):
        if int(nid) not in held_set:
            entries.pop(nid, None)

    events = []
    for nid in held_set:
        if int(nid) in entries:
            continue                 # already tracked
        cost = float(cost_by_id.get(nid, 0.0) or 0.0)
        first_seen = float(first_seen_ts_by_id.get(nid) or first_seen_ts_by_id.get(str(nid)) or 0.0)
        launch_scout = False
        if cost > 0 and cost <= launch_scout_max_entry:
            if first_seen > 0 and (now_ts - first_seen) <= launch_scout_window_days * 86400:
                launch_scout = True
        entries[int(nid)] = {
            "entry_ts": round(now_ts, 0),
            "entry_cost": round(cost, 6),
            "launch_scout": bool(launch_scout),
            "trims_fired": [],
        }
        events.append({
            "event_ts": round(now_ts, 0),
            "netuid": int(nid),
            "name": name_by_id.get(nid, f"SN{nid}"),
            "event_type": "ENTRY",
            "entry_cost_tao": round(cost, 6),
            "peak_value_tao": "",
            "exit_value_tao": "",
            "pnl_pct": "",
            "regime_at_event": "",
            "health_at_event": "",
            "trail_pct_used": "",
            "stop_pct_used": "",
            "fwd_return_1d": "",
            "fwd_return_7d": "",
            "fwd_return_14d": "",
            "trim_rung_pct": "",
            "trim_size_tao": "",
            "is_launch_scout": int(launch_scout),
        })
    return entries, events


def evaluate_tp_trims(
    holdings,
    bal_by_id: dict,
    pnl_by_id: dict | None,
    regime_by_id: dict,
    health_by_id: dict,
    name_by_id: dict,
    entries: dict,                    # from detect_entries — updated in-place-safe copy
    now_ts: float | None = None,
    trim_ladder: list | None = None,
    trim_fraction: float = TP_TRIM_FRACTION,
    skip_ids=(0,),
):
    """Emit TP_TRIM events on launch_scout positions crossing ladder rungs.

    Only positions with launch_scout=True run the ladder. Others rely on the
    trailing stop (evaluate_stops) as their harvest rule. On crossing each
    rung, emits ONE event and marks the rung fired — no re-fires on the same
    rung across cycles even if P&L wobbles.

    Returns (updated_entries, tp_events). Inputs not mutated.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    trim_ladder = trim_ladder if trim_ladder is not None else TP_TRIM_LADDER
    entries = {int(k): dict(v) for k, v in (entries or {}).items()}
    skip = set(skip_ids)
    pnl_by_id = pnl_by_id or {}
    events = []

    for nid, rec in entries.items():
        if int(nid) in skip:
            continue
        if not rec.get("launch_scout"):
            continue
        pnl = pnl_by_id.get(nid)
        if pnl is None:
            continue                 # can't evaluate without cost basis this cycle
        pnl_pct = pnl * 100.0
        already = set(rec.get("trims_fired") or [])
        bal = float(bal_by_id.get(nid, 0.0) or 0.0)
        for rung in trim_ladder:
            if pnl_pct >= rung and rung not in already:
                trim_tao = bal * trim_fraction
                events.append({
                    "event_ts": round(now_ts, 0),
                    "netuid": int(nid),
                    "name": name_by_id.get(nid, f"SN{nid}"),
                    "event_type": "TP_TRIM",
                    "entry_cost_tao": round(rec.get("entry_cost", 0.0) or 0.0, 6),
                    "peak_value_tao": "",
                    "exit_value_tao": round(bal, 6),
                    "pnl_pct": round(pnl_pct, 2),
                    "regime_at_event": str(regime_by_id.get(nid, "")),
                    "health_at_event": round(float(health_by_id[nid]), 1) if nid in health_by_id else "",
                    "trail_pct_used": "",
                    "stop_pct_used": "",
                    "fwd_return_1d": "",
                    "fwd_return_7d": "",
                    "fwd_return_14d": "",
                    "trim_rung_pct": rung,
                    "trim_size_tao": round(trim_tao, 6),
                    "is_launch_scout": 1,
                })
                already.add(rung)
        rec["trims_fired"] = sorted(already)
        entries[nid] = rec
    return entries, events


def format_tp_trim_alert(events: list[dict]) -> str:
    """Harvest alert — separate tone from stop alert (opportunity, not action).

    A stop is 'exit now'; a TP_TRIM is 'lock in — 25% off the top'. Different
    icon, different framing, same outcome-log path.
    """
    if not events:
        return ""
    lines = ["✂️ TP TRIM — launch scout ladder", ""]
    for e in events:
        rung = e.get("trim_rung_pct")
        trim = e.get("trim_size_tao")
        pnl = e.get("pnl_pct")
        rung_txt = f"+{int(rung)}%" if isinstance(rung, (int, float)) else "?"
        pnl_txt = f"{pnl:+}%" if isinstance(pnl, (int, float)) else f"{pnl}%"
        lines.append(f"✂️ SN{e['netuid']} {e['name']} — rung {rung_txt} hit (P&L {pnl_txt})")
        lines.append(f"   → unstake {trim}τ (25% of current)")
        lines.append("")
    lines.append("Advisory. Trailing stop handles the residual.")
    return "\n".join(lines)


def format_stop_alert(events: list[dict]) -> str:
    """The dedicated 🚨 ping — separate from the digest, exact unstake shown."""
    if not events:
        return ""
    lines = ["🚨 STOP TRIGGERED — action required", ""]
    for e in events:
        icon = _ICON.get(e["event_type"], "🚨")
        bal = e.get("exit_value_tao", "")
        pnl = e.get("pnl_pct", "")
        health = e.get("health_at_event", "")
        regime = e.get("regime_at_event", "")
        if e["event_type"] == "HARD_STOP":
            detail = f"{pnl}% vs cost" if pnl != "" else "vs cost basis"
        else:  # TRAIL_STOP
            detail = f"off peak (P&L {pnl:+}% )" if isinstance(pnl, (int, float)) else "off peak"
        bits = [detail]
        if health != "":
            bits.append(f"health {health}")
        if regime:
            bits.append(str(regime))
        lines.append(f"{icon} SN{e['netuid']} {e['name']} — {e['event_type']}")
        lines.append(f"   {' · '.join(bits)}")
        lines.append(f"   → unstake {bal}τ" if bal != "" else "   → unstake (size unknown)")
        lines.append("")
    lines.append("Advisory only — no auto-execution. Confirm before unstaking.")
    return "\n".join(lines)
