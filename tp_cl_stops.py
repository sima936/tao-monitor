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

OUTCOME_LOG_PATH = Path(
    os.environ.get(
        "OUTCOME_LOG_PATH",
        str(Path(__file__).parent / "outcome_log.csv"),
    )
)

# Schema is the spec's outcome-log contract. fwd_return_* are blank at write
# time and joined per-event later (no lookahead), exactly like score_log.csv.
OUTCOME_FIELDS = [
    "event_ts", "netuid", "name", "event_type",
    "entry_cost_tao", "peak_value_tao", "exit_value_tao", "pnl_pct",
    "regime_at_event", "health_at_event", "trail_pct_used", "stop_pct_used",
    "fwd_return_1d", "fwd_return_7d", "fwd_return_14d",
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
    """Append event rows to the outcome log. Non-fatal on failure (advisory)."""
    if not rows:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
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
