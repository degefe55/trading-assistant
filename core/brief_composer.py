"""
Brief composer v3 - layered output with scouting.
Design: 3-second scan → 30-second read → tap for deep dive.

As of the deep-dive-on-tap change: deep-dive blocks are no longer rendered
into the message. The analyst still generates them (the model needs the
working space to reason its way to the action), but the rendered text
omits them. Tap "📖 Deep dive: TICKER" on the brief to see the stored
deep dive instantly. Set INCLUDE_DEEP_DIVE_IN_BRIEFS=True to revert.
"""
from datetime import datetime
from config import KSA_TZ, USD_TO_SAR, MOCK_MODE, DRY_RUN


DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━"
SUBDIV = "─────────────"

# Toggle for verbose briefs. Default False = clean briefs + tap-for-detail.
# Flip True to restore old verbose format (deep dive inline, no buttons).
INCLUDE_DEEP_DIVE_IN_BRIEFS = False

# Hint shown in the message footer reminding the user about tap + reply
TAP_HINT = "<i>📖 Tap a ticker below for deep dive · reply to this msg to ask</i>"


def build_brief_keyboard(analyses: list) -> list:
    """Inline keyboard for a brief: one '📖 Deep dive: TICKER' row per
    analysis with a rec_id.

    Returns list of rows (each row is a list of one button dict), or
    None if no analyses had RecIDs.
    """
    rows = []
    seen = set()
    for a in analyses:
        rec_id = a.get("rec_id")
        ticker = a.get("ticker")
        if not rec_id or not ticker or ticker in seen:
            continue
        seen.add(ticker)
        rows.append([{
            "text": f"📖 Deep dive: {ticker}",
            "callback_data": f"deepdive:{rec_id}",
        }])
    return rows or None


def compose_premarket_brief(positions_analyses: list, watchlist_analyses: list,
                            focus_analyses: list, scout_analyses: list,
                            macro: dict, run_cost: float) -> str:
    """Pre-market brief with scouting."""
    now = datetime.now(KSA_TZ)
    all_main = positions_analyses + focus_analyses + watchlist_analyses

    parts = []
    parts.append(f"<b>🔔 PRE-MARKET BRIEF</b>")
    parts.append(f"<i>{now.strftime('%A %b %d · %H:%M KSA')}</i>")
    if MOCK_MODE:
        parts.append(f"<i>⚠️ MOCK MODE</i>")
    if DRY_RUN:
        parts.append(f"<i>[DRY RUN]</i>")
    # Warn if running before US market open (US opens 16:30 KSA, accounting for DST may shift to 17:30)
    if now.hour < 16 or (now.hour == 16 and now.minute < 30):
        parts.append(f"<i>⏰ Pre-market: prices may lag actual open</i>")
    parts.append("")

    # URGENT ACTIONS
    urgent = [a for a in all_main + scout_analyses
              if a.get("analysis", {}).get("action_urgent")]
    if urgent:
        parts.append(f"<b>🚨 URGENT</b>")
        parts.append(SUBDIV)
        for a in urgent:
            parts.append(_urgent_line(a))
        parts.append("")

    # YOUR POSITIONS
    if positions_analyses:
        parts.append(f"<b>💼 POSITIONS</b>")
        parts.append(SUBDIV)
        total_pnl = 0.0
        for a in positions_analyses:
            line, pnl = _position_line(a)
            parts.append(line)
            total_pnl += pnl
        if len(positions_analyses) > 1:
            emoji = "🟢" if total_pnl >= 0 else "🔴"
            parts.append(f"{emoji} <b>Total unrealized:</b> "
                         f"<code>${total_pnl:+.2f}</code>")
        parts.append("")

    # MARKET PULSE
    parts.append(f"<b>📊 MARKET</b>")
    parts.append(SUBDIV)
    parts.append(_market_pulse_line(macro))
    parts.append("")

    # TODAY'S PLAN
    parts.append(f"<b>⚡ PLAN</b>")
    parts.append(SUBDIV)
    for a in all_main:
        parts.append(_plan_line(a))
    parts.append("")

    # SCOUTING - the new section
    parts.append(f"<b>🔭 SCOUTING</b>")
    parts.append(SUBDIV)
    if not scout_analyses:
        parts.append("<i>No new opportunities worth your attention today.</i>")
    else:
        for a in scout_analyses:
            parts.append(_scout_line(a))
    parts.append("")

    # DEEP DIVES (gated — see INCLUDE_DEEP_DIVE_IN_BRIEFS at top)
    if INCLUDE_DEEP_DIVE_IN_BRIEFS and all_main:
        parts.append(DIVIDER)
        parts.append(f"<b>🔍 DEEP DIVES</b>")
        parts.append(f"<i>(scroll past if done reading)</i>")
        parts.append(DIVIDER)
        parts.append("")

        for a in all_main:
            parts.append(_deep_dive(a))
            parts.append("")

        if scout_analyses:
            parts.append(DIVIDER)
            parts.append(f"<b>🔭 SCOUTING DETAILS</b>")
            parts.append(DIVIDER)
            parts.append("")
            for a in scout_analyses:
                parts.append(_deep_dive(a, show_catalyst=True))
                parts.append("")

    parts.append(DIVIDER)
    if not INCLUDE_DEEP_DIVE_IN_BRIEFS and (all_main or scout_analyses):
        parts.append(TAP_HINT)
    parts.append(f"<i>💰 Run cost: ${run_cost:.4f}</i>")
    return "\n".join(parts)


def compose_midsession_check(material_changes: list, run_cost: float) -> str:
    """Mid-session alert - only sent if material change. Silent otherwise."""
    if not material_changes:
        return ""
    now = datetime.now(KSA_TZ)
    parts = [f"<b>⚡ MID-SESSION</b>", f"<i>{now.strftime('%H:%M KSA')}</i>"]
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
    if INCLUDE_DEEP_DIVE_IN_BRIEFS:
        for a in material_changes:
            parts.append(_deep_dive(a))
            parts.append("")
    elif material_changes:
        parts.append(TAP_HINT)
        parts.append("")

    parts.append(f"<i>💰 ${run_cost:.4f}</i>")
    return "\n".join(parts)


def compose_preclose_verdict(positions_analyses: list, run_cost: float) -> str:
    """Pre-close: hold overnight or exit."""
    now = datetime.now(KSA_TZ)
    parts = [f"<b>🔔 PRE-CLOSE</b>", f"<i>{now.strftime('%H:%M KSA')} · 30 min to close</i>"]
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

    parts.append(f"<b>⚡ OVERNIGHT</b>")
    parts.append(SUBDIV)
    for a in positions_analyses:
        parts.append(_plan_line(a))
    parts.append("")

    if INCLUDE_DEEP_DIVE_IN_BRIEFS:
        parts.append(DIVIDER)
        parts.append(f"<b>🔍 Reasoning</b>")
        parts.append(DIVIDER)
        for a in positions_analyses:
            parts.append(_deep_dive(a))
            parts.append("")
    else:
        parts.append(DIVIDER)
        parts.append(TAP_HINT)
        parts.append("")

    parts.append(f"<i>💰 ${run_cost:.4f}</i>")
    return "\n".join(parts)


def compose_eod_summary(positions, realized_today, unrealized,
                        cumulative_realized, trades_today, run_cost) -> str:
    """End of day P&L."""
    now = datetime.now(KSA_TZ)
    total_today = realized_today + unrealized
    emoji = "🟢" if total_today >= 0 else "🔴"

    parts = [f"<b>📈 END OF DAY</b>", f"<i>{now.strftime('%A %b %d')}</i>"]
    if MOCK_MODE:
        parts.append(f"<i>⚠️ MOCK MODE</i>")
    parts.append("")

    parts.append(f"<b>{emoji} Today: <code>${total_today:+.2f}</code> "
                 f"(SAR {total_today * USD_TO_SAR:+.2f})</b>")
    parts.append(f"  Realized: <code>${realized_today:+.2f}</code>")
    parts.append(f"  Unrealized: <code>${unrealized:+.2f}</code>")
    parts.append("")

    c_emoji = "🟢" if cumulative_realized >= 0 else "🔴"
    parts.append(f"{c_emoji} <b>Cumulative realized:</b> "
                 f"<code>${cumulative_realized:+.2f}</code>")
    parts.append("")

    if trades_today:
        parts.append(f"<b>Today's Trades</b>")
        parts.append(SUBDIV)
        for t in trades_today:
            parts.append(f"  {t.get('Action')} {t.get('Shares')} {t.get('Ticker')} "
                         f"@ ${t.get('Price_USD')}")
    else:
        parts.append("<i>No trades today.</i>")
    parts.append("")

    if positions:
        parts.append(f"<b>Open Positions</b>")
        parts.append(SUBDIV)
        for p in positions:
            parts.append(f"  {p.get('Ticker')}: {p.get('Shares')} sh @ "
                         f"${p.get('AvgCost_USD')}")
    parts.append("")

    parts.append(f"<i>💰 ${run_cost:.4f}</i>")
    return "\n".join(parts)


# ============================================================
# Line builders
# ============================================================
def _urgent_line(a: dict) -> str:
    ticker = a.get("ticker", "?")
    an = a.get("analysis", {})
    action = an.get("action", "?")
    price = an.get("action_price") or a.get("price", "?")
    plan = an.get("one_line_plan", "")
    return f"{_action_icon(action)} <b>{action} {ticker}</b> @ <code>${price}</code> — {plan}"


def _position_line(a: dict):
    ticker = a.get("ticker", "?")
    position = a.get("position") or {}
    price_now = a.get("price", 0)
    shares = float(position.get("Shares", 0))
    avg = float(position.get("AvgCost_USD", 0))
    pnl = (price_now - avg) * shares if shares and avg else 0
    pnl_pct = ((price_now - avg) / avg * 100) if avg else 0
    emoji = "🟢" if pnl >= 0 else "🔴"

    badges = []
    if a.get("halal_listed"):
        badges.append("✅")
    ai_sig = a.get("analysis", {}).get("halal_ai_signal")
    if ai_sig in ("🟢", "🟡", "🔴"):
        badges.append(ai_sig)
    badge_str = " " + "".join(badges) if badges else ""

    return (f"{emoji} <b>{ticker}</b>{badge_str}  "
            f"<code>{int(shares)}sh · ${avg}→${price_now}</code>  "
            f"<b>{pnl:+.2f}</b> ({pnl_pct:+.1f}%)", pnl)


def _plan_line(a: dict) -> str:
    ticker = a.get("ticker", "?")
    an = a.get("analysis", {})
    action = an.get("action", "?")
    plan = an.get("one_line_plan", "")
    confidence = an.get("confidence", "").upper()[:1]
    risk = an.get("risk_score", 3)
    return (f"{_action_icon(action)} <b>{ticker}</b>: {plan} "
            f"<i>[{confidence}·risk {risk}/5]</i>")


def _scout_line(a: dict) -> str:
    """Scout finalist - tighter format, emphasizes catalyst."""
    ticker = a.get("ticker", "?")
    an = a.get("analysis", {})
    action = an.get("action", "?")
    plan = an.get("one_line_plan", "")
    catalyst = a.get("scout_catalyst", "")
    score = a.get("scout_score", 0)
    risk = an.get("risk_score", 3)
    return (f"{_action_icon(action)} <b>{ticker}</b> "
            f"<i>(score {score}/10 · risk {risk}/5)</i>\n"
            f"    💡 {catalyst}\n"
            f"    → {plan}")


def _market_pulse_line(macro: dict) -> str:
    bits = []
    if "FEDFUNDS" in macro:
        bits.append(f"Fed <code>{macro['FEDFUNDS']['value']}%</code>")
    if "DGS10" in macro:
        bits.append(f"10Y <code>{macro['DGS10']['value']}%</code>")
    if "DCOILWTICO" in macro:
        oil = macro["DCOILWTICO"]
        arrow = "↑" if oil.get("value", 0) > oil.get("prev", 0) else "↓"
        bits.append(f"Oil <code>${oil['value']}</code>{arrow}")
    if "CPIAUCSL" in macro:
        # Label may already contain "CPI" prefix - don't duplicate
        cpi_label = macro["CPIAUCSL"].get("label", "")
        if "CPI" in cpi_label:
            bits.append(f"<code>{cpi_label}</code>")
        else:
            bits.append(f"CPI <code>{macro['CPIAUCSL']['value']}</code>")
    return " · ".join(bits) if bits else "<i>macro data unavailable</i>"


def _deep_dive(a: dict, show_catalyst: bool = False) -> str:
    ticker = a.get("ticker", "?")
    analysis = a.get("analysis", {})
    deep = analysis.get("deep_dive", {}) or {}
    price = a.get("price")
    change = a.get("change_pct", 0)

    lines = [f"<b>🔍 {ticker}</b> @ <code>${price}</code> ({change:+.1f}%)"]

    if show_catalyst and a.get("scout_catalyst"):
        lines.append(f"• <b>Catalyst:</b> {a['scout_catalyst']}")
    if deep.get("technical"):
        lines.append(f"• <b>Chart:</b> {deep['technical']}")
    if deep.get("news"):
        lines.append(f"• <b>News:</b> {deep['news']}")
    if deep.get("macro"):
        lines.append(f"• <b>Macro:</b> {deep['macro']}")
    if deep.get("reasoning"):
        lines.append(f"• <b>Reasoning:</b> {deep['reasoning']}")
    sl = deep.get("stop_loss")
    tgt = deep.get("target")
    if sl or tgt:
        lines.append(f"• <b>Levels:</b> stop <code>${sl}</code> · target <code>${tgt}</code>")
    warnings = deep.get("warnings") or []
    if warnings:
        lines.append(f"• ⚠️ <b>Warnings:</b> {'; '.join(warnings)}")
    return "\n".join(lines)


def _action_icon(action: str) -> str:
    a = (action or "").upper()
    return {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡", "WAIT": "⚪"}.get(a, "⚪")
