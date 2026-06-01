"""Patches format_telegram_alert in subnet_scoring_engine.py in-place."""
import re
from pathlib import Path

TARGET = Path("/home/simar/tao-monitor/subnet_scoring_engine.py")

NEW_FUNC = '''def format_telegram_alert(
    result,
    current_holdings=None,
    macro_header: str | None = None,
) -> str:
    """Format scoring result as a Telegram message."""
    lines = [
        "📊 TAO MONITOR v4 — Scoring Update",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if macro_header:
        lines.append(macro_header)
        lines.append("")

    lines.append(f"🟢 {result.passed_filters}/{result.total_subnets} subnets passing filters")
    lines.append("")

    alert_lines = []
    if current_holdings:
        for f in result.filtered_out:
            if f["subnet_id"] in current_holdings:
                alert_lines.append(
                    f"🔴 SN{f[\'subnet_id\']} ({f[\'name\']}) — {f[\'reason\']}"
                )
        for s in result.ranked:
            if s.subnet_id in current_holdings and s.alert_flags:
                for flag in s.alert_flags:
                    if flag in ("MARKOV_BEAR_REGIME", "BELOW_EMA_DOWNTREND", "GENIE_APPROACHING_THRESHOLD"):
                        alert_lines.append(f"⚠️ SN{s.subnet_id} ({s.name}) — {flag}")

    if alert_lines:
        lines.append("🚨 ALERTS:")
        lines.extend(alert_lines)
        lines.append("")

    top = result.ranked[:result.top_n]
    real_gini_count = sum(1 for s in top if s.genie_score_raw != 0.5)
    if top:
        gini_note = "" if real_gini_count == len(top) else f" (⚠️ {len(top)-real_gini_count} Genie est.)"
        lines.append(f"📈 TOP ENTRY OPPORTUNITIES{gini_note}:")
        for i, s in enumerate(top, 1):
            held = " 📌" if current_holdings and s.subnet_id in current_holdings else ""
            markov_tag = f" [{s.markov_regime}]" if s.markov_available else ""
            mom = f" 24h:{s.pct_change_24h:+.0%}" if s.pct_change_24h is not None else ""
            ema_dist = ""
            if s.trend_score > 60:
                ema_dist = f" EMA+{(s.trend_score-50)*0.4:.0f}%"
            elif s.trend_score < 40:
                ema_dist = f" EMA-{(50-s.trend_score)*0.4:.0f}%"
            lines.append(
                f"{i}. SN{s.subnet_id} ({s.name}) — "
                f"Entry: {s.composite_score:.0f}/100{markov_tag} "
                f"📉{mom}{ema_dist} | Genie:{s.genie_score_raw:.2f}{held}"
            )
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"⏰ {result.timestamp}")

    return "\\n".join(lines)

'''

src = TARGET.read_text()

# Find start and end of the function to replace
start_marker = "def format_telegram_alert("
end_marker = "\n\n# ─────"

start_idx = src.find(start_marker)
if start_idx == -1:
    print("ERROR: Could not find format_telegram_alert in file")
    exit(1)

end_idx = src.find(end_marker, start_idx)
if end_idx == -1:
    print("ERROR: Could not find end marker after function")
    exit(1)

new_src = src[:start_idx] + NEW_FUNC + src[end_idx:]

TARGET.write_text(new_src)
print(f"Patched OK — file is {len(new_src)} bytes")
print(f"Replaced chars {start_idx}-{end_idx} ({end_idx-start_idx} bytes removed)")
