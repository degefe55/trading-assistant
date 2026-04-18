"""
Brief composer v2 - layered output.
Design: 3-second scan → 30-second read → 3-minute deep dive.
Actions first. Details below. Colors/emojis for scanability.
"""
from datetime import datetime
from config import KSA_TZ, USD_TO_SAR, MOCK_MODE, DRY_RUN


DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━"
SUBDIV = "─────────────"


def compose_premarket_brief(positions_analyses: list, watchlist_analyses: list,
                            focus_analyses: list, macro: dict,
                            run_cost: float) -> str:
    """Pre-market brief - layered format."""
    now = datetime.now(KSA_TZ)
    all_analyses = positions_analyses + focus_analyses + watchlist_analyses

    parts = []
    parts.append(f"<b>🔔 PRE-MARKET BRIEF</b>")
    parts.append(f"<i>{now.strftime('%A %b %d · %H:%M KSA')}</i>")
    if MOCK_MODE:
        parts.append(f"<i>⚠️ MOCK MODE</i>")
    if DRY_RUN:
        parts.append(f"<i>[DRY RUN]</i>")
    parts.append("")

    # ━━━ TOP LAYER: URGENT ACTIONS ━━━
    urgent = [a for a in all_analyses if a.get("analysis", {}).get("action_urgent")]
    if urgent:
        parts.append(f"<b>🚨 URGENT ACTIONS</b>")
        parts.append(SUBDIV)
        for a in urgent:
            parts.append(_urgent_line(a))
        parts.append("")

    # ━━━ LAYER 2: YOUR MONEY ━━━
    if positions_analyses:
        parts.append(f"<b>💼 YOUR POSITIONS</b>")
        parts.append(SUBDIV)
        total_pnl_usd = 0.0
        for a in positions_analyses:
            line, pnl = _position_line(a)
            parts.append(line)
            total_pnl_usd += pnl
        if len(positions_analyses) > 1:
            pnl_emoji = "🟢" if total_pnl_usd >= 0 else "🔴"
            parts.append(f"{pnl_emoji} <b>Total unrealized:</b> "
                         f"<code>${total_pnl_usd:+.2f}</code> "
                         f"(SAR {total_pnl_usd * USD_TO_SAR:+.2f})")
        parts.append("")

    # ━━━ LAYER 3: MARKET PULSE ━━━
    parts.append(f"<b>📊 MARKET PULSE</b>")
    parts.append(SUBDIV)
    parts.append(_market_pulse_line(macro))
    parts.append("")

    # ━━━ LAYER 4: TODAY'S PLAN ━━━
    parts.append(f"<b>⚡ TODAY'S PLAN</b>")
    parts.append(SUBDIV)
    for a in all_analyses:
        parts.append(_plan_line(a))
    parts.append("")

    # ━━━ DEEP DIVES (scroll past if you don't care) ━━━
    parts.append(DIVIDER)
    parts.append(f"<b>🔍 DEEP DIVES</b>")
    parts.append(f"<i>(scroll past if you got what you needed)</i>")
    parts.append(DIVIDER)
    parts.append("")

    for a in all_analyses:
        parts.append(_deep_dive(a))
        parts.append("")

    # ━━━ FOOTER ━━━
    parts.append(DIVIDER)
    parts.append(f"<i>💰 Run cost: ${run_cost:.4f}</i>")
    return "\n".join(parts)


def compose_midsession_check(material_changes: list, run_cost: float) -> str:
    """Mid-session alert - only sent if something material changed."""
    if not material_changes:
        return ""
    now = datetime.now(KSA_TZ)
    parts = []
    parts.append(f"<b>⚡ MID-SESSION ALERT</b>")
    parts.append(f"<i>{now.strftime('%H:%M KSA')}</i>")
    if MOCK_MODE:
        parts.append(f"<i>⚠️ MOCK MODE</i>")
    parts.append("")

    urgent = [a for a in material_changes if a.get("analysis", {}).get("action_urgent")]
    non_urgent = [a for a in material_changes if not a.get("analysis", {}).get("action_urgent")]

    if urgent:
        parts.append(f"<b>🚨 ACTION NOW</b>")
        parts.append(SUBDIV)
        for a in urgent:
            parts.append(_urgent_line(a))
        parts.append("")

    if non_urgent:
        parts.append(f"<b>📋 Notable</b>")
        parts.append(SUBDIV)
        for a in non_urgent:
            parts.append(_plan_line(a))
        parts.append("")

    parts.append(DIVIDER)
    parts.append(f"<b>🔍 Details</b>")
    parts.append(DIVIDER)
    for a in material_changes:
        parts.append(_deep_dive(a))
        parts.append("")

    parts.append(f"<i>💰 ${run_cost:.4f}</i>")
    return "\n".join(parts)


def compose_preclose_verdict(positions_analyses: list, run_cost: float) -> str:
    """Pre-close: hold overnight or exit?"""
    now = datetime.now(KSA_TZ)
    parts = []
    parts.append(f"<b>🔔 PRE-CLOSE VERDICT</b>")
    parts.append(f"<i>{now.strftime('%H:%M KSA')} · 30 min to close</i>")
    if MOCK_MODE:
        parts.append(f"<i>⚠️ MOCK MODE</i>")
    parts.append("")

    if not positions_analyses:
        parts.append("<i>No open positions to review.</i>")
        parts.append("")
        parts.append(f"<i>💰 ${run_cost:.4f}</i>")
        return "\n".join(parts)

    parts.append(f"<b>💼 POSITIONS</b>")
    parts.append(SUBDIV)
    for a in positions_analyses:
        line, _ = _position_line(a)
        parts.append(line)
    parts.append("")

    parts.append(f"<b>⚡ OVERNIGHT DECISION</b>")
    parts.append(SUBDIV)
    for a in positions_analyses:
        parts.append(_plan_line(a))
    parts.append("")

    parts.append(DIVIDER)
    parts.append(f"<b>🔍 Reasoning</b>")
    parts.append(DIVIDER)
    for a in positions_analyses:
        parts.append(_deep_dive(a))
        parts.append("")

    parts.append(f"<i>💰 ${run_cost:.4f}</i>")
    return "\n".join(parts)


def compose_eod_summary(positions: list, realized_today: float, unrealized: float,
                       cumulative_realized: float, trades_today: list,
                       run_cost: float) -> str:
    """End of day P&L summary."""
    now = datetime.now(KSA_TZ)
    total_today = realized_today + unrealized
    emoji = "🟢" if total_today >= 0 else "🔴"

    parts = []
    parts.append(f"<b>📈 END OF DAY</b>")
    parts.append(f"<i>{now.strftime('%A %b %d')}</i>")
    if MOCK_MODE:
        parts.append(f"<i>⚠️ MOCK MODE</i>")
    parts.append("")

    # Headline P&L
    parts.append(f"<b>{emoji} Today: <code>${total_today:+.2f}</code> "
                 f"(SAR {total_today * USD_TO_SAR:+.2f})</b>")
    parts.append(f"  Realized: <code>${realized_today:+.2f}</code>")
    parts.append(f"  Unrealized: <code>${unrealized:+.2f}</code>")
    parts.append("")

    # Cumulative
    c_emoji = "🟢" if cumulative_realized >= 0 else "🔴"
    parts.append(f"{c_emoji} <b>Cumulative realized:</b> "
                 f"<code>${cumulative_realized:+.2f}</code>")
    parts.append("")

    # Trades
    if trades_today:
        parts.append(f"<b>Today's Trades</b>")
        parts.append(SUBDIV)
        for t in trades_today:
            parts.append(f"  {t.get('Action')} {t.get('Shares')} {t.get('Ticker')} "
                         f"@ ${t.get('Price_USD')}")
    else:
        parts.append("<i>No trades executed today.</i>")
    parts.append("")

    # Open positions
    if positions:
        parts.append(f"<b>Open Positions</b>")
        parts.append(SUBDIV)
        for p in positions:
            parts.append(f"  {p.get('Ticker')}: {p.get('Shares')} sh @ "
                         f"${p.get('AvgCost_USD')} avg")
    parts.append("")

    parts.append(f"<i>💰 ${run_cost:.4f}</i>")
    return "\n".join(parts)


# ============================================================
# LINE BUILDERS
# ============================================================
def _urgent_line(a: dict) -> str:
    """Single urgent action line."""
    ticker = a.get("ticker", "?")
    analysis = a.get("analysis", {})
    action = analysis.get("action", "?")
    price = analysis.get("action_price") or a.get("price", "?")
    plan = analysis.get("one_line_plan", "")
    icon = _action_icon(action)
    return f"{icon} <b>{action} {ticker}</b> @ <code>${price}</code> — {plan}"


def _position_line(a: dict) -> tuple[str, float]:
    """Single position summary line. Returns (line, pnl_usd)."""
    ticker = a.get("ticker", "?")
    position = a.get("position") or {}
    price_now = a.get("price", 0)
    shares = float(position.get("Shares", 0))
    avg = float(position.get("AvgCost_USD", 0))
    pnl = (price_now - avg) * shares if shares and avg else 0
    pnl_pct = ((price_now - avg) / avg * 100) if avg else 0
    emoji = "🟢" if pnl >= 0 else "🔴"

    # Halal badges
    badges = []
    if a.get("halal_listed"):
        badges.append("✅")
    ai_sig = a.get("analysis", {}).get("halal_ai_signal")
    if ai_sig and ai_sig in ("🟢", "🟡", "🔴"):
        badges.append(ai_sig)
    badge_str = " " + "".join(badges) if badges else ""

    line = (f"{emoji} <b>{ticker}</b>{badge_str}  "
            f"<code>{int(shares)}sh · ${avg}→${price_now}</code>  "
            f"<b>{pnl:+.2f}</b> ({pnl_pct:+.1f}%)")
    return line, pnl


def _plan_line(a: dict) -> str:
    """Single plan line - ticker + action + one-line plan."""
    ticker = a.get("ticker", "?")
    analysis = a.get("analysis", {})
    action = analysis.get("action", "?")
    plan = analysis.get("one_line_plan", "")
    confidence = analysis.get("confidence", "").upper()[:1]  # H/M/L
    risk = analysis.get("risk_score", 3)
    icon = _action_icon(action)
    return (f"{icon} <b>{ticker}</b>: {plan} "
            f"<i>[{confidence}·risk {risk}/5]</i>")


def _market_pulse_line(macro: dict) -> str:
    """One-line market summary."""
    bits = []
    # Show rate + oil + CPI + 10yr
    if "FEDFUNDS" in macro:
        bits.append(f"Fed <code>{macro['FEDFUNDS']['value']}%</code>")
    if "DGS10" in macro:
        bits.append(f"10Y <code>{macro['DGS10']['value']}%</code>")
    if "DCOILWTICO" in macro:
        oil = macro["DCOILWTICO"]
        arrow = "↑" if oil.get("value", 0) > oil.get("prev", 0) else "↓"
        bits.append(f"Oil <code>${oil['value']}</code>{arrow}")
    if "CPIAUCSL" in macro:
        bits.append(f"CPI <code>{macro['CPIAUCSL']['label']}</code>")
    return " · ".join(bits) if bits else "<i>macro data unavailable</i>"


def _deep_dive(a: dict) -> str:
    """Full reasoning block for a ticker."""
    ticker = a.get("ticker", "?")
    analysis = a.get("analysis", {})
    deep = analysis.get("deep_dive", {}) or {}
    price = a.get("price")
    change = a.get("change_pct", 0)

    lines = []
    lines.append(f"<b>🔍 {ticker}</b> @ <code>${price}</code> ({change:+.1f}%)")

    if deep.get("technical"):
        lines.append(f"• <b>Chart:</b> {deep['technical']}")
    if deep.get("news"):
        lines.append(f"• <b>News:</b> {deep['news']}")
    if deep.get("macro"):
        lines.append(f"• <b>Macro:</b> {deep['macro']}")
    if deep.get("reasoning"):
        lines.append(f"• <b>Reasoning:</b> {deep['reasoning']}")

    # Stop/target
    sl = deep.get("stop_loss")
    tgt = deep.get("target")
    if sl or tgt:
        lines.append(f"• <b>Levels:</b> stop <code>${sl}</code> · target <code>${tgt}</code>")

    # Warnings
    warnings = deep.get("warnings") or []
    if warnings:
        lines.append(f"• ⚠️ <b>Warnings:</b> {'; '.join(warnings)}")

    return "\n".join(lines)


def _action_icon(action: str) -> str:
    """Emoji for action type."""
    a = (action or "").upper()
    return {
        "BUY": "🟢",
        "SELL": "🔴",
        "HOLD": "🟡",
        "WAIT": "⚪",
    }.get(a, "⚪")
