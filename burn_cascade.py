"""Burn-cost cascade forecast — the tip-off layer.

Chain rule (LS35):
  * A new subnet registration deregisters the incumbent slot with the lowest
    moving_price EMA. All the loser's alpha is liquidated to TAO at the pool
    price and returned as free TAO.
  * Registration cost decays continuously between registrations, doubles on
    each new one. So a falling burn cost = a registration is being priced in
    = the dereg cascade is imminent.
  * The newly-registered slot starts on a fresh bonding curve — historically
    the two big spikes near the start of most subnet charts (Simon's read of
    ~30 charts). That's the entry opportunity.

This module gives us three read-only signals to act on IN ADVANCE:

  1. `estimate_daily_rate` — turns the noisy per-cron burn deltas into a
     single per-day rate via linear fit over a rolling window. The last
     6h/4min deltas alone are useless (LS35 showed 3.32τ in 6h then 0.34τ
     in 4 min); a fit smooths that out.

  2. `forecast_thresholds` — ETA to key burn levels (1000/500/250τ). Tells
     Simon "cascade nearer" as a number of days rather than vibes.

  3. `check_threshold_crossings` — a dedicated ⏳ alert when burn actually
     crosses one of the thresholds this cron. De-duplicated: each threshold
     only fires once per crossing direction.

Plus an immunity annotation for the dereg watchlist: subnets stamped as
newly-seen after the monitor started tracking are still in the 4-month
immunity window and CAN'T be deregistered, so surface that so we don't
alert on false rotation candidates.

All pure functions. No I/O. No network. Import into run_scoring.py.
"""

from __future__ import annotations

import os
import time

# Rolling window for the burn-cost history. Longer = more stable rate
# estimate, shorter = more responsive to acceleration. 48h @ 6h cron = 8
# samples, which is enough for a linear fit but tight enough to notice
# regime changes.
BURN_HISTORY_MAX_HOURS = float(os.environ.get("BURN_HISTORY_MAX_HOURS", "48"))

# The threshold ladder — burn levels we care about. Ordered high→low so
# crossings fire from the top down as the cascade develops.
BURN_THRESHOLDS_TAO = [
    float(x) for x in os.environ.get(
        "BURN_THRESHOLDS_TAO", "1000,500,250,100"
    ).split(",")
]

# 4-month immunity from LS35's chain spec. New subnets can't be dereg'd
# during this window so they shouldn't show up as rotation candidates.
IMMUNITY_DAYS = float(os.environ.get("IMMUNITY_DAYS", "120"))

# For the "genuinely new after tracking started" heuristic: any netuid whose
# first_seen is within this many seconds of the earliest first_seen in the
# map is treated as "legacy — birth unknown" (i.e. all first-seen stamps
# from the initial batch when tracking began). Everything later is a real
# new registration we can trust immunity-check.
LEGACY_STAMP_WINDOW_S = float(os.environ.get("LEGACY_STAMP_WINDOW_S", "300"))


def update_burn_history(
    prev_history: list,
    cost: float,
    ts: float,
    max_hours: float = BURN_HISTORY_MAX_HOURS,
) -> list:
    """Append (ts, cost), prune anything older than max_hours.

    prev_history: list of [ts, cost] pairs (json-friendly, not tuples).
    Returns a new list — never mutates the input.
    """
    cutoff = float(ts) - float(max_hours) * 3600.0
    out = [
        [float(t), float(c)]
        for t, c in (prev_history or [])
        if float(t) >= cutoff
    ]
    out.append([float(ts), float(cost)])
    return out


def estimate_daily_rate(history: list) -> float | None:
    """Simple linear-fit slope in τ per day. None if <2 points or degenerate.

    Uses the closed-form OLS slope so we don't drag numpy in for one number:
        slope = sum((x - xbar)(y - ybar)) / sum((x - xbar)^2)
    Then converts from τ/second to τ/day.
    """
    pts = [(float(t), float(c)) for t, c in (history or [])
           if t is not None and c is not None]
    if len(pts) < 2:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(pts)
    xbar = sum(xs) / n
    ybar = sum(ys) / n
    num = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    den = sum((x - xbar) ** 2 for x in xs)
    if den == 0:
        return None
    slope_per_sec = num / den
    return slope_per_sec * 86400.0


def forecast_thresholds(
    current: float | None,
    rate_per_day: float | None,
    thresholds: list | None = None,
) -> dict:
    """Days-until-crossing for each threshold below `current`.

    Only returns thresholds that are (a) BELOW current and (b) approach-able
    at the current rate (rate must be negative). Skipped thresholds are
    represented as None to distinguish "not applicable" from "0 days".

    Returns {threshold_tao: days_until | None}, ordered high→low.
    """
    thresholds = thresholds if thresholds is not None else BURN_THRESHOLDS_TAO
    out: dict = {}
    if current is None or rate_per_day is None or rate_per_day >= 0:
        # No forecast possible — either no current, no rate, or rate is flat/up
        return {float(t): None for t in sorted(thresholds, reverse=True)}
    cur = float(current)
    for t in sorted(thresholds, reverse=True):
        t = float(t)
        if t >= cur:
            out[t] = None  # already above current — not a future crossing
            continue
        # Days from now until burn hits t (rate is negative, so gap/rate flips sign)
        gap = cur - t
        days = gap / (-rate_per_day)
        out[t] = round(days, 2)
    return out


def check_threshold_crossings(
    current: float | None,
    prev: float | None,
    thresholds: list | None = None,
    already_alerted: dict | None = None,
) -> tuple[list, dict]:
    """Detect when burn just crossed one of the thresholds this cron.

    Direction: we care about downward crossings (cascade approaching). A
    subsequent upward crossing (registration happened → burn spikes back up)
    clears the latch so the next downward pass alerts again.

    Returns (crossed_thresholds, new_already_alerted) where crossed is a
    list of {threshold, direction: 'down'} events for this cron only.
    """
    thresholds = thresholds if thresholds is not None else BURN_THRESHOLDS_TAO
    alerted = {str(int(t)): bool(v)
               for t, v in (already_alerted or {}).items()}
    events = []
    if current is None or prev is None:
        return events, alerted
    cur = float(current)
    prv = float(prev)
    for t in sorted(thresholds, reverse=True):
        t = float(t)
        key = str(int(t))
        if prv > t and cur <= t:
            # Downward crossing this cron
            if not alerted.get(key):
                events.append({"threshold": t, "direction": "down"})
                alerted[key] = True
        elif prv <= t and cur > t:
            # Upward crossing — a registration just happened, clear the latch
            alerted[key] = False
    return events, alerted


def annotate_immunity(
    candidates: list,
    first_seen_map: dict | None,
    now_ts: float | None = None,
    immunity_days: float = IMMUNITY_DAYS,
    legacy_stamp_window_s: float = LEGACY_STAMP_WINDOW_S,
) -> list:
    """Annotate each dereg candidate with its immunity status.

    Three states:
      * "immune"          — genuinely new (stamped after tracking started)
                            AND within immunity_days of first_seen. Can't
                            be deregistered, so shouldn't appear as a
                            rotation candidate.
      * "legacy_unknown"  — first_seen matches the initial-batch stamp (i.e.
                            existed before tracking began). Real birth
                            unknown; treat as out of immunity since the
                            netuid presumably has enough history to have
                            fallen to the bottom of moving_price anyway.
      * "eligible"        — genuinely new AND past immunity_days. Dereg
                            candidate.

    candidates: list of {"netuid": int, ...} — augmented in place-safe
    fashion (returns new list of dicts). Extra fields added:
        days_seen: float | None       (from first_seen; None if unstamped)
        immunity_status: str          (one of above)
    """
    now_ts = now_ts if now_ts is not None else time.time()
    fs_map = {int(k): float(v)
              for k, v in (first_seen_map or {}).items()
              if v is not None}
    # Earliest stamp = when tracking effectively began (the initial batch).
    earliest = min(fs_map.values()) if fs_map else None

    out = []
    for c in (candidates or []):
        try:
            nid = int(c.get("netuid"))
        except (TypeError, ValueError):
            out.append(dict(c))
            continue
        fs = fs_map.get(nid)
        days_seen: float | None = None
        status = "eligible"
        if fs is None:
            status = "eligible"        # never stamped — treat as legacy
        else:
            days_seen = round((now_ts - fs) / 86400.0, 2)
            # Was this netuid part of the initial batch stamp?
            legacy_batch = (
                earliest is not None
                and (fs - earliest) <= legacy_stamp_window_s
            )
            if legacy_batch:
                status = "legacy_unknown"
            elif days_seen is not None and days_seen < immunity_days:
                status = "immune"
            else:
                status = "eligible"
        new = dict(c)
        new["days_seen"] = days_seen
        new["immunity_status"] = status
        out.append(new)
    return out


def format_cascade_footer(
    current: float | None,
    delta: float | None,
    rate_per_day: float | None,
    forecasts: dict,
) -> str:
    """One-line-ish burn footer for the digest.

    Extends the existing "🔥 burn: 1413.53τ (-3.32τ)" line with a rate and
    ETA to the nearest crossable threshold. Silent if any piece is missing.

    Example: "🔥 burn: 1413.53τ (-3.32τ) · ~120τ/d · 1000τ in 3.4d"
    """
    if current is None:
        return ""
    lines = [f"\U0001F525 burn: {float(current):.2f}\u03c4"]
    if delta is not None and abs(float(delta)) >= 0.01:
        lines.append(f"({float(delta):+.2f}\u03c4)")
    if rate_per_day is not None and rate_per_day < 0:
        lines.append(f"\u00b7 ~{abs(rate_per_day):.0f}\u03c4/d")
        # Nearest future crossing = smallest days_until > 0
        future = [(t, d) for t, d in (forecasts or {}).items()
                  if d is not None and d > 0]
        if future:
            nearest_t, nearest_d = min(future, key=lambda x: x[1])
            lines.append(f"\u00b7 {int(nearest_t)}\u03c4 in {nearest_d:.1f}d")
    return " ".join(lines)


def format_threshold_alert(
    events: list,
    current: float,
    rate_per_day: float | None,
    forecasts: dict,
) -> str:
    """Dedicated ⏳ ping when burn crosses a threshold this cron.

    Distinct from the dereg watchlist ping (which is about specific slots),
    this is about the CASCADE STATE: we've just entered a new regime where
    registration is meaningfully cheaper.
    """
    if not events:
        return ""
    lines = ["\u23F3 BURN THRESHOLD CROSSED"]
    for e in events:
        t = e["threshold"]
        lines.append(f"Burn now {current:.2f}\u03c4 \u2014 crossed {int(t)}\u03c4 "
                     "(registration cascade nearer)")
    if rate_per_day is not None and rate_per_day < 0:
        lines.append(f"Rate ~{abs(rate_per_day):.0f}\u03c4/d")
    # Show remaining ETAs to give context on how much further this can go
    future = [(t, d) for t, d in (forecasts or {}).items()
              if d is not None and d > 0]
    if future:
        future.sort(key=lambda x: x[1])
        lines.append("Next: " + " \u00b7 ".join(
            f"{int(t)}\u03c4 in {d:.1f}d" for t, d in future[:3]
        ))
    return "\n".join(lines)
