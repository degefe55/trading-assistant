"""
Trade management module.
Handles /buy, /sell, /pnl, /status commands from Telegram.
Updates Positions tab + TradeLog tab.
Calculates avg cost on buys, realized P&L on sells.
"""
from datetime import datetime
from config import KSA_TZ, USD_TO_SAR
from core import sheets, data_router
from core.logger import log_event


def record_buy(ticker: str, shares: float, price_usd: float,
               market: str = "US", reason: str = "") -> dict:
    """
    Record a buy. Updates Positions (new avg cost) + appends TradeLog.
    Returns result dict for Telegram reply.
    """
    ticker = ticker.upper()
    positions = sheets.read_positions()
    existing = next((p for p in positions if p.get("Ticker") == ticker
                    and p.get("Market", "US") == market), None)

    if existing:
        # Average up/down: (old_shares*old_avg + new_shares*new_price) / total_shares
        old_shares = float(existing.get("Shares", 0))
        old_avg = float(existing.get("AvgCost_USD", 0))
        new_total_shares = old_shares + shares
        new_avg = (old_shares * old_avg + shares * price_usd) / new_total_shares
        new_stop = existing.get("StopLoss") if existing.get("StopLoss") else new_avg
        # Keep user stop if they had a custom one below new avg, else use avg (core rule)
        try:
            stop_val = float(new_stop)
            if stop_val > new_avg:
                stop_val = new_avg
        except (TypeError, ValueError):
            stop_val = new_avg

        _update_position(ticker, market, {
            "Shares": new_total_shares,
            "AvgCost_USD": round(new_avg, 4),
            "AvgCost_SAR": round(new_avg * USD_TO_SAR, 2),
            "StopLoss": round(stop_val, 4),
        })
        action_summary = (f"Added to existing position. "
                         f"New: {new_total_shares} sh @ avg ${new_avg:.4f}")
    else:
        # New position
        _insert_position(ticker, market, shares, price_usd)
        action_summary = f"New position opened: {shares} sh @ ${price_usd}"

    # Log trade
    sheets.append_trade({
        "ticker": ticker,
        "market": market,
        "action": "BUY",
        "shares": shares,
        "price_usd": price_usd,
        "price_sar": round(price_usd * USD_TO_SAR, 2),
        "reason": reason or "Manual /buy",
    })

    log_event("INFO", "trades", f"BUY {shares} {ticker} @ ${price_usd}")

    return {
        "success": True,
        "action": "BUY",
        "ticker": ticker,
        "shares": shares,
        "price_usd": price_usd,
        "price_sar": round(price_usd * USD_TO_SAR, 2),
        "summary": action_summary,
    }


def record_sell(ticker: str, shares: float, price_usd: float,
                market: str = "US", reason: str = "") -> dict:
    """
    Record a sell. Updates Positions (reduce shares or delete) + TradeLog.
    Calculates realized P&L.
    """
    ticker = ticker.upper()
    positions = sheets.read_positions()
    existing = next((p for p in positions if p.get("Ticker") == ticker
                    and p.get("Market", "US") == market), None)

    if not existing:
        return {"success": False, "error": f"No position in {ticker} to sell"}

    held_shares = float(existing.get("Shares", 0))
    avg_cost = float(existing.get("AvgCost_USD", 0))

    if shares > held_shares:
        return {"success": False,
                "error": f"Can't sell {shares} - only hold {held_shares} of {ticker}"}

    # Realized P&L on this sale
    pnl_usd = (price_usd - avg_cost) * shares
    pnl_sar = pnl_usd * USD_TO_SAR

    remaining = held_shares - shares
    if remaining < 0.0001:
        # Fully closed - remove position
        _delete_position(ticker, market)
        action_summary = f"Position closed. Realized P&L: ${pnl_usd:+.2f}"
    else:
        _update_position(ticker, market, {"Shares": remaining})
        action_summary = (f"Reduced to {remaining} sh @ avg ${avg_cost:.4f}. "
                         f"Realized P&L on sale: ${pnl_usd:+.2f}")

    sheets.append_trade({
        "ticker": ticker,
        "market": market,
        "action": "SELL",
        "shares": shares,
        "price_usd": price_usd,
        "price_sar": round(price_usd * USD_TO_SAR, 2),
        "reason": reason or "Manual /sell",
        "pnl_usd": round(pnl_usd, 2),
        "pnl_sar": round(pnl_sar, 2),
    })

    log_event("INFO", "trades", f"SELL {shares} {ticker} @ ${price_usd}, P&L ${pnl_usd:+.2f}")

    return {
        "success": True,
        "action": "SELL",
        "ticker": ticker,
        "shares": shares,
        "price_usd": price_usd,
        "price_sar": round(price_usd * USD_TO_SAR, 2),
        "pnl_usd": round(pnl_usd, 2),
        "pnl_sar": round(pnl_sar, 2),
        "summary": action_summary,
    }


def calculate_pnl() -> dict:
    """Calculate total portfolio P&L (realized + unrealized)."""
    positions = sheets.read_positions()

    unrealized_usd = 0.0
    position_details = []
    for pos in positions:
        ticker = pos.get("Ticker")
        if not ticker:
            continue
        shares = float(pos.get("Shares", 0))
        avg = float(pos.get("AvgCost_USD", 0))
        current = data_router.get_price(ticker).get("price", 0)
        pnl = (current - avg) * shares
        unrealized_usd += pnl
        position_details.append({
            "ticker": ticker,
            "shares": shares,
            "avg_cost": avg,
            "current_price": current,
            "pnl_usd": round(pnl, 2),
            "pnl_pct": round((current - avg) / avg * 100, 2) if avg else 0,
        })

    # Realized from TradeLog
    realized_today = 0.0
    realized_total = 0.0
    trades_today_count = 0
    today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    try:
        trade_log = _read_trade_log()
        for t in trade_log:
            pnl = _safe_float(t.get("PnL_USD", 0))
            if pnl:
                realized_total += pnl
                if t.get("Date") == today_str:
                    realized_today += pnl
                    trades_today_count += 1
    except Exception as e:
        log_event("ERROR", "trades", f"Trade log read failed: {e}")

    return {
        "unrealized_usd": round(unrealized_usd, 2),
        "unrealized_sar": round(unrealized_usd * USD_TO_SAR, 2),
        "realized_today_usd": round(realized_today, 2),
        "realized_today_sar": round(realized_today * USD_TO_SAR, 2),
        "realized_total_usd": round(realized_total, 2),
        "realized_total_sar": round(realized_total * USD_TO_SAR, 2),
        "trades_today": trades_today_count,
        "positions": position_details,
    }


def get_todays_trades() -> list:
    """Return trades executed today from TradeLog."""
    today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    try:
        return [t for t in _read_trade_log() if t.get("Date") == today_str]
    except Exception:
        return []


# ============================================================
# Low-level helpers
# ============================================================
def _update_position(ticker: str, market: str, updates: dict):
    """Modify existing position row."""
    ss = sheets._get_spreadsheet()
    if ss is None:
        return
    try:
        ws = ss.worksheet("Positions")
        records = ws.get_all_records()
        # Row index in sheet (1-indexed, +1 for header)
        for i, r in enumerate(records, start=2):
            if r.get("Ticker") == ticker and r.get("Market", "US") == market:
                headers = SCHEMAS_POSITIONS_HEADERS
                for field, val in updates.items():
                    if field in headers:
                        col = headers.index(field) + 1
                        ws.update_cell(i, col, val)
                return
    except Exception as e:
        log_event("ERROR", "trades", f"Update position failed: {e}")


def _insert_position(ticker: str, market: str, shares: float, price_usd: float):
    """Add new position row."""
    ss = sheets._get_spreadsheet()
    if ss is None:
        return
    try:
        ws = ss.worksheet("Positions")
        today = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
        row = [
            ticker,
            market,
            shares,
            round(price_usd, 4),
            round(price_usd * USD_TO_SAR, 2),
            round(price_usd, 4),  # stop loss = avg cost per your rule
            "",  # target - bot will propose on next brief
            today,
            "",  # sector
            "FYI",  # halal status - informational only
        ]
        ws.append_row(row)
    except Exception as e:
        log_event("ERROR", "trades", f"Insert position failed: {e}")


def _delete_position(ticker: str, market: str):
    """Remove closed position row."""
    ss = sheets._get_spreadsheet()
    if ss is None:
        return
    try:
        ws = ss.worksheet("Positions")
        records = ws.get_all_records()
        for i, r in enumerate(records, start=2):
            if r.get("Ticker") == ticker and r.get("Market", "US") == market:
                ws.delete_rows(i)
                return
    except Exception as e:
        log_event("ERROR", "trades", f"Delete position failed: {e}")


def _read_trade_log() -> list:
    """Read all rows from TradeLog."""
    ss = sheets._get_spreadsheet()
    if ss is None:
        return []
    try:
        ws = ss.worksheet("TradeLog")
        return ws.get_all_records()
    except Exception:
        return []


def _safe_float(v, default=0.0) -> float:
    try:
        if v == "" or v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


# Imported here to avoid circular
SCHEMAS_POSITIONS_HEADERS = ["Ticker", "Market", "Shares", "AvgCost_USD",
                              "AvgCost_SAR", "StopLoss", "Target", "DateOpened",
                              "Sector", "HalalStatus"]
