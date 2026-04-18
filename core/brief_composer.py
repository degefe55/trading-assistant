"""
Brief composer.
Takes analysis results and assembles a complete Telegram message.
"""
from datetime import datetime
from config import KSA_TZ, USD_TO_SAR, MOCK_MODE, DRY_RUN


def compose_premarket_brief(positions_analyses: list, watchlist_analyses: list,
                            focus_analyses: list, macro: dict,
                            run_cost: float) -> str:
    """Pre-market brief: 30 min before US open."""
    now = datetime.now(KSA_TZ)
    header = _header("📊 PRE-MARKET BRIEF", now)
    parts = [header]

    # Mock mode warning
    if MOCK_MODE:
        parts.append("<i>⚠️ MOCK MODE — using simulated data</i>\n")
    if DRY_RUN:
        parts.append("<i>[DRY RUN] — recommendations are informational only</i>\n")

    # Macro snapshot
    parts.append("<b>🌍 Market Context</b>")
    for code, m in list(macro.items())[:5]:
        trend = "→"
        if m.get("prev") and m.get("value") != m.get("prev"):
            trend = "↑" if m["value"] > m["prev"] else "↓"
        parts.append(f"  {m.get('label', code)}: {m.get('value')}{m.get('unit', '')} {trend}")
    parts.append("")

    # Held positions
    if positions_analyses:
        parts.append("<b>💼 Your Positions</b>")
        for analysis in positions_analyses:
            parts.append(analysis["recommendation"])
            parts.append("")
    else:
        parts.append("<b>💼 No open positions</b>\n")

    # Focus stocks (if any)
    if focus_analyses:
        parts.append("<b>🔍 Focus Stocks</b>")
        for analysis in focus_analyses:
            parts.append(analysis["recommendation"])
            parts.append("")

    # Watchlist (briefer)
    if watchlist_analyses:
        parts.append("<b>👀 Watchlist</b>")
        for analysis in watchlist_analyses:
            parts.append(analysis["recommendation"])
            parts.append("")

    # Footer with cost
    parts.append(_footer(run_cost))
    return "\n".join(parts)


def compose_midsession_check(material_changes: list, run_cost: float) -> str:
    """Mid-session: only sent if something material changed."""
    now = datetime.now(KSA_TZ)
    if not material_changes:
        return ""  # don't send anything if nothing changed

    parts = [_header("⚡ MID-SESSION UPDATE", now)]
    if MOCK_MODE:
        parts.append("<i>⚠️ MOCK MODE</i>\n")

    for change in material_changes:
        parts.append(change["recommendation"])
        parts.append("")

    parts.append(_footer(run_cost))
    return "\n".join(parts)


def compose_preclose_verdict(positions_analyses: list, run_cost: float) -> str:
    """Pre-close: hold-overnight or exit decisions."""
    now = datetime.now(KSA_TZ)
    parts = [_header("🔔 PRE-CLOSE VERDICT (30 min to close)", now)]

    if MOCK_MODE:
        parts.append("<i>⚠️ MOCK MODE</i>\n")

    if not positions_analyses:
        parts.append("No open positions to review.")
    else:
        for a in positions_analyses:
            parts.append(a["recommendation"])
            parts.append("")

    parts.append(_footer(run_cost))
    return "\n".join(parts)


def compose_eod_summary(positions: list, realized_today: float, unrealized: float,
                        cumulative_realized: float, trades_today: list,
                        run_cost: float) -> str:
    """End of day: P&L + recap. Sent at 11:00 PM KSA."""
    now = datetime.now(KSA_TZ)
    parts = [_header("📈 END OF DAY SUMMARY", now)]

    if MOCK_MODE:
        parts.append("<i>⚠️ MOCK MODE</i>\n")

    # P&L block
    total_today = realized_today + unrealized
    total_today_sar = total_today * USD_TO_SAR
    emoji = "🟢" if total_today >= 0 else "🔴"
    parts.append(f"<b>Today's P&amp;L</b>: {emoji} ${total_today:+.2f} ({total_today_sar:+.2f} SAR)")
    parts.append(f"  Realized: ${realized_today:+.2f}")
    parts.append(f"  Unrealized: ${unrealized:+.2f}")
    parts.append("")

    parts.append(f"<b>Cumulative realized</b>: ${cumulative_realized:+.2f} "
                 f"({cumulative_realized * USD_TO_SAR:+.2f} SAR)")
    parts.append("")

    # Trades today
    if trades_today:
        parts.append("<b>Today's Trades</b>")
        for t in trades_today:
            parts.append(f"  {t.get('Action')} {t.get('Shares')} {t.get('Ticker')} "
                         f"@ ${t.get('Price_USD')} — {t.get('Reason', '')}")
    else:
        parts.append("<i>No trades executed today.</i>")
    parts.append("")

    # Open positions status
    if positions:
        parts.append("<b>Open Positions</b>")
        for p in positions:
            parts.append(f"  {p.get('Ticker')}: {p.get('Shares')} sh @ "
                         f"${p.get('AvgCost_USD')} avg")
    parts.append("")

    parts.append(_footer(run_cost))
    return "\n".join(parts)


def _header(title: str, now: datetime) -> str:
    """Standard brief header."""
    return (f"<b>{title}</b>\n"
            f"<i>{now.strftime('%A, %B %d, %Y — %H:%M KSA')}</i>\n"
            f"{'─' * 25}\n")


def _footer(run_cost: float) -> str:
    """Cost footer."""
    return (f"{'─' * 25}\n"
            f"<i>Run cost: ${run_cost:.4f}</i>")
