"""
Main entry point.
Called by GitHub Actions on schedule.
Reads BRIEF_TYPE env var to decide what to run.

BRIEF_TYPE values:
  premarket  - 3:30 PM KSA full pre-market brief
  midsession - 7:30 PM KSA check (only sends if material change)
  preclose   - 10:30 PM KSA pre-close verdict
  eod        - 11:00 PM KSA end-of-day summary
  test       - Manual trigger for testing (runs premarket brief)
"""
import os
import sys
import traceback
from datetime import datetime

from config import (MOCK_MODE, DEBUG_MODE, ACTIVE_MARKETS, KSA_TZ,
                    DAILY_COST_ALERT_USD)
from core import (analyst, brief_composer, sheets, telegram_client,
                  data_router)
from core.logger import log_event, get_log_buffer, get_run_summary, clear_log_buffer
from markets.us import config as us_cfg


def main():
    """Main execution. Dispatches based on BRIEF_TYPE."""
    clear_log_buffer()
    brief_type = os.getenv("BRIEF_TYPE", "test").lower()
    log_event("INFO", "main", f"Starting run: type={brief_type}, mock={MOCK_MODE}, debug={DEBUG_MODE}")

    try:
        # Ensure Sheet tabs exist (safe no-op if already created)
        sheets.initialize_sheet()

        if brief_type == "premarket":
            run_premarket_brief()
        elif brief_type == "midsession":
            run_midsession_check()
        elif brief_type == "preclose":
            run_preclose_verdict()
        elif brief_type == "eod":
            run_eod_summary()
        elif brief_type == "test":
            run_premarket_brief()  # test defaults to premarket
        else:
            log_event("ERROR", "main", f"Unknown BRIEF_TYPE: {brief_type}")
            return 1

        # Flush logs to Sheet at end of run
        _flush_logs_to_sheet()

        # Check cost threshold
        summary = get_run_summary()
        if summary["total_cost_usd"] > DAILY_COST_ALERT_USD:
            telegram_client.send_error_alert(
                f"Single run cost ${summary['total_cost_usd']:.2f} exceeded "
                f"${DAILY_COST_ALERT_USD} threshold. Investigate."
            )
        return 0

    except Exception as e:
        err = f"Bot crashed: {e}\n\n{traceback.format_exc()}"
        log_event("ERROR", "main", err)
        try:
            telegram_client.send_error_alert(err[:1500])
            _flush_logs_to_sheet()
        except Exception:
            pass
        return 1


def run_premarket_brief():
    """Full pre-market brief. Analyzes positions + focus + watchlist + macro."""
    if "US" not in ACTIVE_MARKETS:
        log_event("INFO", "main", "US market not active, skipping")
        return

    # Read state from Sheet
    positions = sheets.read_positions()
    focus = sheets.read_focus()
    us_positions = [p for p in positions if p.get("Market", "US") == "US"]
    us_focus = [f for f in focus if f.get("Market", "US") == "US"]

    # Analyze each held position
    position_analyses = []
    for pos in us_positions:
        ticker = pos.get("Ticker")
        if not ticker:
            continue
        result = analyst.analyze_ticker(ticker, "US", "premarket", position=pos)
        if "error" not in result:
            position_analyses.append(result)

    # Analyze focus stocks
    focus_analyses = []
    held_tickers = {p.get("Ticker") for p in us_positions}
    for f in us_focus:
        ticker = f.get("Ticker")
        if not ticker or ticker in held_tickers:
            continue
        result = analyst.analyze_ticker(ticker, "US", "premarket")
        if "error" not in result:
            focus_analyses.append(result)

    # Analyze watchlist (default: SPWO/SPUS/IBIT if not already covered)
    watchlist_analyses = []
    covered = held_tickers | {a["ticker"] for a in focus_analyses}
    for ticker in us_cfg.DEFAULT_WATCHLIST:
        if ticker in covered:
            continue
        result = analyst.analyze_ticker(ticker, "US", "premarket")
        if "error" not in result:
            watklist_entry = result
            watchlist_analyses.append(watklist_entry)

    # Fetch macro for display
    macro = data_router.get_macro()

    # Calculate run cost
    summary = get_run_summary()
    run_cost = summary["total_cost_usd"]

    # Compose and send
    message = brief_composer.compose_premarket_brief(
        position_analyses, watchlist_analyses, focus_analyses, macro, run_cost
    )
    telegram_client.send_message(message)
    log_event("INFO", "main", "Pre-market brief sent")


def run_midsession_check():
    """Mid-session: only run analysis, send alert only if material change."""
    if "US" not in ACTIVE_MARKETS:
        return

    positions = sheets.read_positions()
    us_positions = [p for p in positions if p.get("Market", "US") == "US"]

    material_changes = []
    for pos in us_positions:
        ticker = pos.get("Ticker")
        if not ticker:
            continue
        # Trigger criteria: price near stop loss, target, or 52-wk high
        current_price = data_router.get_price(ticker).get("price", 0)
        avg_cost = float(pos.get("AvgCost_USD", 0))
        stop = float(pos.get("StopLoss", 0))
        target = float(pos.get("Target", 0))
        is_material = (
            (stop and abs(current_price - stop) / stop < 0.015) or
            (target and abs(current_price - target) / target < 0.015) or
            (avg_cost and abs(current_price - avg_cost) / avg_cost > 0.03)
        )
        if is_material:
            result = analyst.analyze_ticker(ticker, "US", "midsession", position=pos)
            if "error" not in result:
                material_changes.append(result)

    if not material_changes:
        log_event("INFO", "main", "No material changes mid-session, no alert sent")
        return

    summary = get_run_summary()
    message = brief_composer.compose_midsession_check(material_changes, summary["total_cost_usd"])
    if message:
        telegram_client.send_message(message)
        log_event("INFO", "main", "Mid-session alert sent")


def run_preclose_verdict():
    """Pre-close: recommendation to hold overnight or exit."""
    if "US" not in ACTIVE_MARKETS:
        return

    positions = sheets.read_positions()
    us_positions = [p for p in positions if p.get("Market", "US") == "US"]

    analyses = []
    for pos in us_positions:
        ticker = pos.get("Ticker")
        if not ticker:
            continue
        result = analyst.analyze_ticker(ticker, "US", "preclose", position=pos)
        if "error" not in result:
            analyses.append(result)

    summary = get_run_summary()
    message = brief_composer.compose_preclose_verdict(analyses, summary["total_cost_usd"])
    telegram_client.send_message(message)
    log_event("INFO", "main", "Pre-close verdict sent")


def run_eod_summary():
    """End of day: P&L + recap. No Claude calls needed."""
    if "US" not in ACTIVE_MARKETS:
        return

    positions = sheets.read_positions()
    us_positions = [p for p in positions if p.get("Market", "US") == "US"]

    # Calculate unrealized P&L
    unrealized = 0.0
    for pos in us_positions:
        ticker = pos.get("Ticker")
        if not ticker:
            continue
        current_price = data_router.get_price(ticker).get("price", 0)
        shares = float(pos.get("Shares", 0))
        avg_cost = float(pos.get("AvgCost_USD", 0))
        unrealized += (current_price - avg_cost) * shares

    # Realized today + cumulative - read from TradeLog
    # Phase A simplification: placeholder zero until trade logging is built (Phase C)
    realized_today = 0.0
    cumulative_realized = 0.0
    trades_today = []

    summary = get_run_summary()
    message = brief_composer.compose_eod_summary(
        us_positions, realized_today, unrealized, cumulative_realized,
        trades_today, summary["total_cost_usd"]
    )
    telegram_client.send_message(message)
    log_event("INFO", "main", "EOD summary sent")


def _flush_logs_to_sheet():
    """Write all collected logs to the Sheet Logs tab."""
    try:
        buffer = get_log_buffer()
        if buffer:
            sheets.append_logs(buffer)
    except Exception as e:
        print(f"Could not flush logs to sheet: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
