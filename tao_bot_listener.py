"""
tao_bot_listener.py — On-demand Telegram command listener
===========================================================
Polls the Telegram Bot API for commands and triggers scoring runs on demand.
Runs as a persistent background process on Infinity8 (separate from cron).

Commands:
    /status   — run full scoring cycle and send results immediately
    /macro    — show current TAO macro regime from tao_macro.json
    /holdings — show current holdings pass/fail status
    /help     — list available commands

Start (background):
    nohup python3 tao_bot_listener.py >> /home/simar/tao-monitor/bot_listener.log 2>&1 &

Stop:
    pkill -f tao_bot_listener.py

Check if running:
    pgrep -f tao_bot_listener.py

Environment variables (loaded from .env):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
    TAOSTATS_API_KEY
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

from taostats_fetch import fetch_wallet_holdings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [bot_listener] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot_listener")

BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
API_KEY    = os.environ.get("TAOSTATS_API_KEY", "")
SCRIPT_DIR = Path(__file__).parent
MACRO_FILE = SCRIPT_DIR / "tao_macro.json"
STATE_FILE = SCRIPT_DIR / "scoring_state.json"

POLL_INTERVAL = 2   # seconds between Telegram getUpdates polls
COMMAND_COOLDOWN = 30  # seconds — ignore repeated *same* command within this window

_last_command_ts: dict[str, float] = {}
_last_update_id: int = 0


def send(text: str) -> None:
    """Send a message to the configured Telegram chat."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("No bot token/chat ID — cannot send")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Send failed: {e}")


def get_updates(offset: int) -> list[dict]:
    """Poll Telegram for new messages."""
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 20, "allowed_updates": ["message"]},
            timeout=25,
        )
        if resp.status_code == 200:
            return resp.json().get("result", [])
    except Exception as e:
        logger.error(f"getUpdates failed: {e}")
    return []


def _dashboard_url() -> str | None:
    """Derive the dashboard score endpoint from DASHBOARD_URL, or fall back to
    DASHBOARD_INGEST_URL by swapping /api/ingest-score -> /api/score (the same
    URL scheme run_scoring.py uses to POST). Returns None if neither is set."""
    url = os.environ.get("DASHBOARD_URL", "").strip()
    if url:
        return url.rstrip("/") + "/api/score"
    ingest = os.environ.get("DASHBOARD_INGEST_URL", "").strip()
    if ingest and "ingest-score" in ingest:
        return ingest.replace("ingest-score", "score")
    return None


def _fetch_latest_score() -> dict | None:
    """GET the last-ingested cron payload from serve.py. Uses BasicAuth
    (DASHBOARD_USER/PASS). Returns None on any failure — caller surfaces it."""
    url = _dashboard_url()
    if not url:
        return None
    user = os.environ.get("DASHBOARD_USER", "tao")
    pwd = os.environ.get("DASHBOARD_PASS", "bittensor")
    try:
        r = requests.get(url, auth=(user, pwd), timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "awaiting_first_scan":
            return None
        return data
    except Exception as e:
        logger.warning(f"Dashboard fetch failed: {e}")
        return None


def _format_status_from_payload(data: dict) -> str:
    """Render the on-demand /status message from the cron payload cached on
    serve.py. Uses the same layout as the 6h actionable digest so figures are
    consistent across on-demand and scheduled views."""
    # Local imports so a missing module in a legacy container still starts the
    # bot for /macro and /holdings (which don't need these).
    from types import SimpleNamespace
    from subnet_allocation import format_actionable_digest
    from spot_price import get_tao_prices

    macro = data.get("macro") or {}
    alloc = data.get("allocation") or {}

    # Reconstruct just enough of the AllocationPlan surface that the formatter
    # touches. We don't need the full dataclass — SimpleNamespace with the
    # attributes the formatter reads is sufficient (regime/signal/positions/
    # cut/deployed_fraction/sn0_target_weight).
    positions = []
    for p in alloc.get("positions") or []:
        positions.append(SimpleNamespace(
            subnet_id=int(p.get("subnet_id")),
            name=p.get("name") or "",
            action=p.get("action") or "hold",
            current_weight=p.get("current_weight"),
            target_weight=p.get("target_weight") or 0.0,
            markov_regime=p.get("markov_regime") or "",
            reason=p.get("reason") or "",
            pending_exit=bool(p.get("pending_exit")),
            pending_entry=bool(p.get("pending_entry")),
        ))
    plan = SimpleNamespace(
        macro_regime=macro.get("regime") or "Unknown",
        macro_signal=float(macro.get("signal") or 0.0),
        deployed_fraction=float(alloc.get("deployed_fraction") or 0.0),
        sn0_target_weight=float(alloc.get("sn0_target_weight") or 0.0),
        positions=positions,
        cut=alloc.get("cut") or [],
    )

    # Fundamentals for the ⚠️ Fund line — read the same file the cron reads.
    fundamentals = {}
    try:
        fp = SCRIPT_DIR / "fundamentals.json"
        if fp.exists():
            fundamentals = (json.loads(fp.read_text()) or {}).get("subnets", {}) or {}
    except Exception:
        pass

    prices = get_tao_prices() or {}
    # ts: cron timestamp is ISO; render HH:MM in the user's local zone if we can.
    ts_iso = data.get("timestamp") or ""
    ts_hhmm = "—"
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        ts_hhmm = dt.astimezone(ZoneInfo("Europe/London")).strftime("%H:%M")
    except Exception:
        ts_hhmm = ts_iso[11:16] if len(ts_iso) >= 16 else "—"

    account_total_tao = data.get("account_total_tao")
    free_tao = data.get("free_tao")
    # Root-stake TAO — added to the payload by run_scoring so /status can
    # render the same root/alpha/free split as the 6h cron digest. Older
    # payloads (pre-fix) won't have it → the tail gracefully degrades.
    root_tao = data.get("root_tao")

    # Prefer payload-cached TAO prices (cron already fetched them). Falls
    # back to a fresh get_tao_prices() call for older payloads without the
    # key, and again to empty when both fail (silent — the digest just
    # loses the ~£X / $Y tail rather than failing to render).
    payload_prices = data.get("tao_prices") or {}
    usd = payload_prices.get("usd") or prices.get("usd")
    gbp = payload_prices.get("gbp") or prices.get("gbp")

    msg = format_actionable_digest(
        plan,
        free_tao=free_tao,
        account_tao=account_total_tao,
        ts=ts_hhmm,
        fundamentals=fundamentals,
        root_tao=root_tao,
        tao_usd=usd,
        tao_gbp=gbp,
    )

    # Burn cascade footer — same one the 6h cron appends. All inputs already
    # in the payload, so no need to import burn_cascade here (would work
    # anyway since both services deploy from the same repo, but keeps the
    # listener honest about its "cache-served" contract).
    try:
        bc = data.get("burn_cost_tao")
        bd = data.get("burn_cost_delta_tao")
        br = data.get("burn_rate_per_day")
        bf = data.get("burn_forecasts") or {}
        if bc is not None:
            from burn_cascade import format_cascade_footer
            # burn_forecasts keys arrive as strings after JSON round-trip;
            # format_cascade_footer picks nearest by numeric value regardless,
            # but we cast keys back for robustness.
            bf_num = {}
            for k, v in bf.items():
                try:
                    bf_num[float(k)] = v
                except (TypeError, ValueError):
                    continue
            footer = format_cascade_footer(
                float(bc),
                (float(bd) if bd is not None else None),
                (float(br) if br is not None else None),
                bf_num,
            )
            if footer:
                msg += "\n" + footer
    except Exception as _bfe:
        logger.debug(f"/status burn footer skipped: {_bfe}")

    # Store-health footer — from payload (populated by cron). Same
    # rows/subnets/span line the cron carries.
    try:
        ss = data.get("store_stats") or {}
        if ss and "error" not in ss:
            msg += (f"\n\n\U0001F4CA store: {ss.get('rows', 0)} rows \u00b7 "
                    f"{ss.get('netuids', 0)} subnets \u00b7 "
                    f"{ss.get('span_days', 0)}d span")
    except Exception as _sse:
        logger.debug(f"/status store footer skipped: {_sse}")

    return msg


def handle_status() -> None:
    """Render the latest cron snapshot from serve.py — same data path as the
    dashboard. Replaces the old subprocess-run-scoring approach, which failed
    because the listener container has no persistent snapshot history."""
    data = _fetch_latest_score()
    if not data:
        send(
            "⚠️ /status unavailable — dashboard hasn't ingested a scoring run yet, "
            "or DASHBOARD_URL/DASHBOARD_INGEST_URL is unset. Next cron will populate it."
        )
        return
    try:
        send(_format_status_from_payload(data))
    except Exception as e:
        logger.exception("Status format failed")
        send(f"🔴 Status format failed: {e}")


def handle_macro() -> None:
    """Show current TAO macro regime — reads from tao_macro.json written by fetch_tao_macro cron.
    Falls back to running fetch_tao_macro.py directly if file missing."""
    send("⏳ Fetching TAO macro regime...")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "fetch_tao_macro.py")],
            capture_output=True, text=True, timeout=60,
            env={**os.environ}, cwd=str(SCRIPT_DIR),
        )
        # fetch_tao_macro.py writes tao_macro.json then exits
        macro_path = SCRIPT_DIR / "tao_macro.json"
        if not macro_path.exists():
            send(f"⚠️ Macro fetch failed:\n<pre>{result.stdout[-300:]}</pre>")
            return
        data = json.loads(macro_path.read_text())
        if data.get("unavailable_reason"):
            send(f"⚠️ TAO macro unavailable: {data['unavailable_reason']}")
            return
        regime = data.get("current_regime", "Unknown")
        signal = float(data.get("signal", 0))
        probs  = data.get("next_state_probabilities", {})
        bull_p = float(probs.get("bull", 0.33))
        bear_p = float(probs.get("bear", 0.33))
        emoji  = "🟢" if regime == "Bull" else ("🔴" if regime == "Bear" else "🟡")
        send(
            f"🌍 TAO Macro Regime\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} <b>{regime}</b>\n"
            f"Signal: {signal:+.3f}\n"
            f"Bull: {bull_p:.0%}  Bear: {bear_p:.0%}"
        )
    except Exception as e:
        send(f"🔴 Error fetching macro: {e}")


def handle_holdings() -> None:
    """Show current holdings status — fetches real positions from chain."""
    send("⏳ Checking holdings...")
    try:
        # Fetch real holdings from chain
        holdings = fetch_wallet_holdings(API_KEY)
        if not holdings:
            send("⚠️ Could not fetch wallet holdings from chain")
            return

        sys.path.insert(0, str(SCRIPT_DIR))
        from taostats_fetch import TaostatsClient, fetch_all_subnet_metrics
        from subnet_scoring_engine import run_scoring_cycle

        client = TaostatsClient(api_key=API_KEY)
        all_metrics = fetch_all_subnet_metrics(client, fetch_concentration=False)
        scoring_result = run_scoring_cycle(all_metrics, top_n=5)

        lines = [f"📋 <b>Holdings Status</b> ({len(holdings)} positions)\n━━━━━━━━━━━━━━━━━━━━"]
        scored = {s.subnet_id: s for s in scoring_result.ranked_by_health}
        failed = {f["subnet_id"]: f["reason"] for f in scoring_result.filtered_out}

        for sn_id in holdings:
            if sn_id in failed:
                lines.append(f"🔴 SN{sn_id} — {failed[sn_id]}")
            elif sn_id in scored:
                s = scored[sn_id]
                chg = f" 24h:{s.pct_change_24h:+.0%}" if s.pct_change_24h is not None else ""
                lines.append(f"✅ SN{sn_id} ({s.name}) H:{s.health_score:.0f}{chg}")
            else:
                lines.append(f"✅ SN{sn_id} — passing")
        send("\n".join(lines))
    except Exception as e:
        send(f"🔴 Error checking holdings: {e}")


def _format_brief_from_payload(data: dict, netuid: int) -> str:
    """Render /brief output from the cached cron payload.

    Reads the per-netuid maps that run_scoring embeds in the dashboard payload
    (metrics_by_netuid, identity_by_netuid, dereg_watchlist, bal/pnl/cost) plus
    fundamentals.json from disk. Same data path the cron uses — no chain reads
    on-demand, no scraping. Sub-second to render.
    """
    key = str(netuid)
    metrics = (data.get("metrics_by_netuid") or {}).get(key)
    identity = (data.get("identity_by_netuid") or {}).get(key) or {}

    if not metrics and not identity:
        return (f"⚠️ SN{netuid} not found in latest payload.\n"
                "Either the netuid doesn't exist, or the cron hasn't ingested "
                "a run since the payload schema expanded — try again after "
                "the next 6h digest.")

    # Fundamentals verdict (matches /status pattern)
    verdict, verdict_note = "", ""
    try:
        fp = SCRIPT_DIR / "fundamentals.json"
        if fp.exists():
            fnd = json.loads(fp.read_text()) or {}
            rec = ((fnd.get("subnets") or {}).get(key) or {})
            if isinstance(rec, dict):
                verdict = str(rec.get("verdict", "")).strip()
                verdict_note = str(rec.get("note", "") or rec.get("thesis", "")).strip()
            elif isinstance(rec, str):
                verdict = rec.strip()
    except Exception as _fe:
        logger.debug(f"fundamentals lookup failed: {_fe}")

    # Held state
    bal = float((data.get("bal_by_netuid") or {}).get(key, 0) or 0)
    cost = float((data.get("cost_by_netuid") or {}).get(key, 0) or 0)
    pnl_raw = (data.get("pnl_by_netuid") or {}).get(key)
    pnl_pct = (float(pnl_raw) * 100.0) if pnl_raw is not None else None

    # Dereg rank
    dereg_rank = None
    for entry in (data.get("dereg_watchlist") or []):
        if int(entry.get("netuid", -1)) == netuid:
            dereg_rank = int(entry.get("rank"))
            break

    # Preferred name: identity > metrics > fallback
    metrics = metrics or {}
    name = (identity.get("name") or metrics.get("name") or "").strip() or f"SN{netuid}"
    lines = [f"📋 <b>SN{netuid} · {name}</b>", "━" * 18]

    # Identity block
    if identity.get("url"):
        lines.append(f"🌐 {identity['url']}")
    if identity.get("github"):
        lines.append(f"💻 {identity['github']}")
    if identity.get("description"):
        lines.append(f"   {identity['description'][:140]}")
    if any(identity.get(k) for k in ("url", "github", "description")):
        lines.append("")

    # Market data
    if metrics:
        price = float(metrics.get("token_price") or 0.0)
        pool = float(metrics.get("pool_depth") or 0.0)
        gini = float(metrics.get("gini") or 0.0)
        ma = metrics.get("moving_price")
        vol = float(metrics.get("volume_24h") or 0.0)
        market = f"📊 price {price:.4f}τ · pool {pool:,.0f}τ · gini {gini:.2f}"
        if ma is not None:
            market += f" · MA {float(ma):.4f}τ"
        lines.append(market)
        if vol > 0:
            lines.append(f"📈 24h vol: {vol:,.0f}τ")

    # Dereg risk (only surface if in bottom 10)
    if dereg_rank is not None:
        icon = "⚠️" if dereg_rank <= 5 else "👀"
        lines.append(f"📉 Dereg rank: #{dereg_rank} {icon}")

    lines.append("")

    # Held
    if bal > 0.001:
        held = f"✅ Held: {bal:.2f}α"
        if cost > 0:
            held += f" ({cost:.2f}τ cost"
            if pnl_pct is not None:
                held += f", {pnl_pct:+.1f}%"
            held += ")"
        lines.append(held)
    else:
        lines.append("○ Not held")

    # Verdict
    if verdict:
        v_icon = {"KEEP": "✅", "WATCH": "👀", "AVOID": "🚫"}.get(verdict.upper(), "•")
        v_line = f"{v_icon} Verdict: <b>{verdict}</b>"
        if verdict_note:
            v_line += f" — {verdict_note[:100]}"
        lines.append(v_line)

    lines.append("")
    lines.append(f"🔗 https://tao.app/subnets/{netuid}")
    return "\n".join(lines)


def handle_brief(arg: str) -> None:
    """Per-subnet quick facts. Usage: /brief <netuid>"""
    arg = (arg or "").strip()
    if not arg:
        send("Usage: /brief &lt;netuid&gt;  (e.g. /brief 107)")
        return
    try:
        netuid = int(arg.split()[0])
    except (ValueError, IndexError):
        send(f"⚠️ '{arg}' is not a valid netuid. Usage: /brief &lt;netuid&gt;")
        return
    if netuid < 0 or netuid > 999:
        send(f"⚠️ SN{netuid} out of range (0-999)")
        return
    data = _fetch_latest_score()
    if not data:
        send("⚠️ /brief unavailable — dashboard hasn't ingested a scoring run "
             "yet, or auth env vars are missing. Try /status first.")
        return
    send(_format_brief_from_payload(data, netuid))


def _format_pnl_from_payload(data: dict) -> str:
    """Render book-level P&L from the cached cron payload.

    Reads per-netuid maps (bal, cost, pnl, metrics, identity) that run_scoring
    embeds in the dashboard payload. Same cache-served path as /brief and
    /hermes — no chain reads, no taostats calls.

    P&L per position uses pnl_by_netuid (compute_holdings_pnl output) when
    available — same source /brief uses so numbers agree. Falls back to
    raw `bal × price − cost` only if pnl_by_netuid is missing that netuid.

    Alpha book (traded positions) is treated separately from Root SN0
    (passive delegation) and Free (idle cash) because they respond to
    different dynamics — active P&L is what a trader wants to see.
    """
    metrics = data.get("metrics_by_netuid") or {}
    identity = data.get("identity_by_netuid") or {}
    bal = data.get("bal_by_netuid") or {}
    cost = data.get("cost_by_netuid") or {}
    pnl_map = data.get("pnl_by_netuid") or {}

    total_tao = data.get("account_total_tao")
    root_tao  = data.get("root_tao")
    free_tao  = data.get("free_tao")

    # Per-position rows
    rows = []
    total_value = 0.0
    total_cost = 0.0
    for nid_str in bal:
        try:
            nid = int(nid_str)
        except (TypeError, ValueError):
            continue
        if nid == 0:
            continue                       # SN0 root — handled separately
        bal_tao = float(bal.get(nid_str, 0) or 0)
        if bal_tao <= 0.001:
            continue
        m = metrics.get(nid_str) or {}
        price = float(m.get("token_price", 0) or 0)
        # bal_by_netuid is ALREADY spot-valued in TAO (alpha * price, done
        # upstream in chain_fetch.py / parse_stake_balances — matches
        # taostats' balance_as_tao). Do NOT multiply by price again here;
        # that was the source of the 14.7τ /pnl book-total gap.
        value_tao = bal_tao
        cost_tao = float(cost.get(nid_str, 0) or 0)
        name = ((identity.get(nid_str) or {}).get("name")
                or m.get("name") or f"SN{nid}").strip() or f"SN{nid}"
        # Prefer pnl_by_netuid (same source as /brief) → falls back to raw
        # value-minus-cost only if pnl_by_netuid doesn't have this netuid.
        # pnl_by_netuid is stored as a fraction (0.37 = +37%).
        pnl_raw = pnl_map.get(nid_str)
        if pnl_raw is not None:
            pnl_pct = float(pnl_raw) * 100.0
            # Derive τ P&L from pct + cost (consistent with pnl_by_netuid's
            # definition, not from raw bal × price which double-counts
            # realized trims for partially-exited positions).
            pnl_tao = cost_tao * float(pnl_raw) if cost_tao > 0 else None
        elif cost_tao > 0:
            pnl_tao = value_tao - cost_tao
            pnl_pct = (pnl_tao / cost_tao) * 100.0
        else:
            pnl_tao = None
            pnl_pct = None
        rows.append({
            "nid":       nid,
            "name":      name,
            "value_tao": value_tao,
            "cost_tao":  cost_tao,
            "pnl_tao":   pnl_tao,
            "pnl_pct":   pnl_pct,
        })
        total_value += value_tao
        if cost_tao > 0:
            total_cost += cost_tao

    # Format — largest position first
    rows.sort(key=lambda x: -x["value_tao"])

    lines = ["💰 <b>BOOK P&amp;L</b>", "━" * 18]

    if not rows:
        lines.append("No alpha positions held.")
    else:
        lines.append("<b>Positions:</b>")
        for r in rows:
            val = r["value_tao"]
            cost_r = r["cost_tao"]
            pnl_pct = r["pnl_pct"]
            pnl_tao = r["pnl_tao"]
            if pnl_pct is not None:
                arrow = "🟢" if pnl_pct >= 0 else "🔴"
                pnl_str = f"{arrow} {pnl_tao:+.2f}τ ({pnl_pct:+.1f}%)"
            else:
                pnl_str = "⚪ no cost basis"
            # Truncate name so line fits on mobile
            name_disp = r["name"][:10]
            lines.append(f"  SN{r['nid']:<3d} {name_disp:<10s}  "
                         f"{val:>5.2f}τ · cost {cost_r:>4.2f}τ  {pnl_str}")

        lines.append("")

        # Alpha book totals — sum realized+unrealized P&L from per-row source
        # (pnl_by_netuid if present, raw math otherwise). Consistent with
        # what /brief shows on each position.
        book_pnl_tao = sum(r["pnl_tao"] for r in rows if r["pnl_tao"] is not None)
        if total_cost > 0:
            book_pnl_pct = (book_pnl_tao / total_cost) * 100.0
            arrow = "🟢" if book_pnl_pct >= 0 else "🔴"
            lines.append(f"<b>Alpha book:</b> {total_value:.2f}τ "
                         f"(cost {total_cost:.2f}τ)")
            lines.append(f"<b>{arrow} P&amp;L: {book_pnl_tao:+.2f}τ "
                         f"({book_pnl_pct:+.1f}%)</b>")
        else:
            lines.append(f"<b>Alpha book:</b> {total_value:.2f}τ "
                         "(no cost basis available)")

    lines.append("")

    # Root + free (non-traded)
    if root_tao is not None:
        lines.append(f"🌱 Root SN0: {float(root_tao):.2f}τ  <i>(passive APY)</i>")
    if free_tao is not None:
        lines.append(f"💵 Free:     {float(free_tao):.2f}τ")
    if total_tao is not None:
        lines.append(f"━━━━━━━━━━━━━━━━━━")
        lines.append(f"<b>Account total: {float(total_tao):.2f}τ</b>")

    return "\n".join(lines)


def _format_pnl24h_from_payload(data: dict) -> str:
    """24h book P&L delta, cache-served (same payload as /pnl, /brief).

    Derived from bal_by_netuid (current TAO value — already spot-valued, see
    /pnl's fix) and the snapshot-history 24h price delta embedded in
    metrics_by_netuid[nid]["pct_24h"].

    Approximation: assumes alpha unit count unchanged over the trailing 24h
    (i.e. no trim/add in the window) — bal_then = bal_now / (1 + pct24h/100).
    A trim/add inside the window will skew that position's delta; fine for a
    quick pulse-check, not a substitute for /pnl's point-in-time truth.

    Positions with no 24h price point yet (store still accumulating, or a
    brand-new listing) are omitted from the total and listed separately
    rather than assumed flat — same "never fabricate a delta" contract as
    snapshot_history itself.
    """
    metrics = data.get("metrics_by_netuid") or {}
    identity = data.get("identity_by_netuid") or {}
    bal = data.get("bal_by_netuid") or {}

    rows = []
    missing = []
    total_now = 0.0
    total_then = 0.0
    for nid_str, bal_raw in bal.items():
        try:
            nid = int(nid_str)
        except (TypeError, ValueError):
            continue
        if nid == 0:
            continue                       # SN0 root — passive APY, not a trade
        bal_tao = float(bal_raw or 0)
        if bal_tao <= 0.001:
            continue
        m = metrics.get(nid_str) or {}
        name = ((identity.get(nid_str) or {}).get("name")
                or m.get("name") or f"SN{nid}").strip() or f"SN{nid}"
        pct24h = m.get("pct_24h")
        if pct24h is None:
            missing.append(name)
            continue
        pct24h = float(pct24h)
        bal_then = bal_tao / (1.0 + pct24h / 100.0)
        delta_tao = bal_tao - bal_then
        rows.append({"nid": nid, "name": name, "now": bal_tao,
                     "delta": delta_tao, "pct": pct24h})
        total_now += bal_tao
        total_then += bal_then

    lines = ["🕐 <b>24H BOOK P&amp;L</b>", "━" * 18]
    if not rows:
        lines.append("No priced positions with 24h history yet — "
                     "store still accumulating.")
    else:
        rows.sort(key=lambda r: -r["now"])
        for r in rows:
            arrow = "🟢" if r["delta"] >= 0 else "🔴"
            lines.append(f"  SN{r['nid']:<3d} {r['name'][:10]:<10s}  "
                         f"{arrow} {r['delta']:+.2f}τ ({r['pct']:+.1f}%)")
        lines.append("")
        total_delta = total_now - total_then
        total_pct = (total_delta / total_then * 100.0) if total_then > 0 else None
        arrow = "🟢" if total_delta >= 0 else "🔴"
        pct_str = f" ({total_pct:+.1f}%)" if total_pct is not None else ""
        lines.append(f"<b>{arrow} Alpha book 24h: {total_delta:+.2f}τ{pct_str}</b>")

    if missing:
        shown = ", ".join(missing[:6])
        extra = f" +{len(missing) - 6} more" if len(missing) > 6 else ""
        lines.append("")
        lines.append(f"⏳ accumulating (no 24h point yet): {shown}{extra}")

    return "\n".join(lines)


def handle_pnl24h() -> None:
    """/pnl24h — 24h book P&L delta from the cached payload."""
    data = _fetch_latest_score()
    if not data:
        send("⚠️ /pnl24h unavailable — dashboard hasn't ingested a scoring run "
             "yet, or auth env vars are missing. Try /status first.")
        return
    try:
        msg = _format_pnl24h_from_payload(data)
    except Exception as e:
        logger.warning(f"pnl24h format failed: {e}")
        msg = f"⚠️ /pnl24h: failed to format ({type(e).__name__}: {e})"
    send(msg)


def handle_pnl() -> None:
    """/pnl — book-level P&L snapshot from the cached payload."""
    data = _fetch_latest_score()
    if not data:
        send("⚠️ /pnl unavailable — dashboard hasn't ingested a scoring run "
             "yet, or auth env vars are missing. Try /status first.")
        return
    try:
        msg = _format_pnl_from_payload(data)
    except Exception as e:
        logger.warning(f"pnl format failed: {e}")
        msg = f"⚠️ /pnl: failed to format ({type(e).__name__}: {e})"
    send(msg)


def _format_hermes_report(r: dict) -> str:
    """Render hermes_lite calibration report for Telegram.

    Report shape produced by hermes_lite.run_full_report():
      generated_at, snapshot_span_days, snapshot_netuids,
      fwd_returns_filled: {shadow, outcome},
      stability: [{param, jitter, n_events, pct_changed_low/high}, ...],
      ic: {per_horizon: {h: {n, pooled_ic, ic_stability_iqr, per_subnet}}},
      verdict: {status, message, effective_ic_7d?, pooled_ic_7d?, horizon_caveat?}
    """
    lines = ["🧪 <b>HERMES LITE — calibration report</b>", "━" * 20]

    # Store span + backfill counts
    span = r.get("snapshot_span_days")
    n_uids = r.get("snapshot_netuids") or 0
    if span is not None:
        lines.append(f"📈 store: {n_uids} netuids · {span}d span")
    filled = r.get("fwd_returns_filled") or {}
    lines.append(f"🔄 backfilled: {filled.get('shadow', 0)} shadow · "
                 f"{filled.get('outcome', 0)} outcome")
    lines.append("")

    # Perturbation-stability
    lines.append("<b>Stops — perturbation stability:</b>")
    for s in (r.get("stability") or []):
        param = s.get("param", "?")
        if "error" in s:
            lines.append(f"  {param}: {s['error']}")
            continue
        lo = float(s.get("pct_changed_low", 0)) * 100
        hi = float(s.get("pct_changed_high", 0)) * 100
        n = int(s.get("n_events", 0))
        max_churn = max(lo, hi)
        if max_churn < 5:
            tag = "⚪ noise"
        elif max_churn > 40:
            tag = "🔴 chaotic"
        else:
            tag = "🟡 meaningful"
        j = s.get("jitter", 0)
        lines.append(f"  {param} ±{j}: −{lo:.0f}% / +{hi:.0f}% "
                     f"({n} events) {tag}")
    lines.append("")

    # IC per horizon
    lines.append("<b>Markov signal IC:</b>")
    per_h = (r.get("ic", {}) or {}).get("per_horizon", {}) or {}
    if not per_h:
        lines.append("  no scoring data yet")
    else:
        for h_str in sorted(per_h.keys(), key=lambda x: int(x)):
            block = per_h[h_str]
            n = int(block.get("n", 0))
            pooled = block.get("pooled_ic")
            stab = block.get("ic_stability_iqr")
            p_str = f"{pooled:+.3f}" if pooled is not None else "n/a"
            s_str = f"IQR {stab:.3f}" if stab is not None else "IQR n/a"
            lines.append(f"  {h_str}d: n={n} · pooled {p_str} · {s_str}")
    lines.append("")

    # Verdict
    v = r.get("verdict") or {}
    status = v.get("status", "unknown")
    icon = {
        "candidate_positive": "🟢",
        "anti_predictive":    "🔴",
        "not_significant":    "⚪",
        "insufficient_data":  "⏳",
    }.get(status, "•")
    lines.append(f"<b>Verdict:</b> {icon}")
    lines.append(f"  {v.get('message', '(none)')}")
    eff = v.get("effective_ic_7d")
    pooled_7 = v.get("pooled_ic_7d")
    if eff is not None:
        lines.append(f"  effective IC (7d): {eff:+.3f}  "
                     f"(pooled {pooled_7:+.3f})" if pooled_7 is not None
                     else f"  effective IC (7d): {eff:+.3f}")
    caveat = v.get("horizon_caveat")
    if caveat:
        lines.append(f"  ⚠️ {caveat}")

    ts = str(r.get("generated_at", ""))[:19]
    if ts:
        lines.append(f"\n<i>generated: {ts} UTC</i>")
    return "\n".join(lines)


def handle_hermes() -> None:
    """/hermes — read last hermes_lite calibration report from dashboard payload."""
    data = _fetch_latest_score()
    if not data:
        send("⚠️ /hermes unavailable — dashboard hasn't ingested a scoring run "
             "yet, or auth env vars are missing. Try /status first.")
        return
    report = data.get("hermes_report")
    if not report:
        send("⚠️ /hermes: no calibration report in latest payload.\n"
             "Cron may not have run hermes_lite yet — try again after the "
             "next cron cycle.")
        return
    try:
        msg = _format_hermes_report(report)
    except Exception as e:
        logger.warning(f"hermes report format failed: {e}")
        msg = f"⚠️ /hermes: failed to format report ({type(e).__name__}: {e})"
    send(msg)


def handle_help() -> None:
    send(
        "🤖 <b>Tao Seeker Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "/status         — run scoring now and send full update\n"
        "/macro          — show current TAO macro regime\n"
        "/holdings       — show holdings from chain with health scores\n"
        "/pnl            — book P&amp;L snapshot per position\n"
        "/pnl24h         — 24h book P&amp;L delta\n"
        "/brief &lt;netuid&gt; — per-subnet quick facts (e.g. /brief 107)\n"
        "/hermes         — calibration report (IC, stability, verdict)\n"
        "/help           — this message\n\n"
        "All on-demand — no automated messages."
    )


HANDLERS = {
    "/status":   handle_status,
    "/macro":    handle_macro,
    "/holdings": handle_holdings,
    "/pnl":      handle_pnl,
    "/pnl24h":   handle_pnl24h,
    "/hermes":   handle_hermes,
    "/help":     handle_help,
}


def process_update(update: dict) -> None:
    message = update.get("message", {})
    raw = (message.get("text") or "").strip()
    from_id = str(message.get("chat", {}).get("id", ""))

    # Only respond to the configured chat
    if from_id != str(CHAT_ID):
        logger.info(f"Ignoring message from chat {from_id}")
        return

    # Split first token (command) from the rest (args). Strip @botname suffix
    # from the command specifically — args like "107" must survive intact.
    # "/brief@TaoBot 107" → cmd="/brief", args="107"
    parts = raw.split(None, 1)
    if not parts:
        return
    cmd = parts[0].lower().split("@")[0]
    args = parts[1] if len(parts) > 1 else ""

    # /brief takes an argument — special-case before the exact-match table.
    if cmd == "/brief":
        now = time.time()
        last = _last_command_ts.get(cmd, 0)
        if now - last < COMMAND_COOLDOWN:
            logger.info(f"Command '{cmd}' ignored — cooldown active")
            return
        _last_command_ts[cmd] = now
        logger.info(f"Handling command: {cmd} {args}")
        handle_brief(args)
        return

    if cmd not in HANDLERS:
        return

    # Cooldown to prevent double-triggers — per command, so /macro then /holdings
    # back-to-back doesn't silently drop the second one.
    now = time.time()
    last = _last_command_ts.get(cmd, 0)
    if now - last < COMMAND_COOLDOWN:
        logger.info(f"Command '{cmd}' ignored — cooldown active")
        return

    _last_command_ts[cmd] = now
    logger.info(f"Handling command: {cmd}")
    HANDLERS[cmd]()


def main() -> None:
    global _last_update_id

    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    logger.info("TAO bot listener started — polling for commands")


    while True:
        try:
            updates = get_updates(_last_update_id + 1)
            for update in updates:
                _last_update_id = max(_last_update_id, update.get("update_id", 0))
                process_update(update)
        except Exception as e:
            logger.error(f"Poll loop error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
