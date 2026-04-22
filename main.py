"""
Main entry point.
Called by GitHub Actions on schedule or manual trigger.
Reads BRIEF_TYPE env var to decide what to run.

BRIEF_TYPE values:
  premarket  - 3:30 PM KSA full pre-market brief + scouting
  midsession - 7:30 PM KSA check (only sends if material change)
  preclose   - 10:30 PM KSA pre-close verdict
  eod        - 11:00 PM KSA end-of-day summary
  commands   - Poll Telegram for commands (runs every 5 min)
  test       - Manual trigger for testing (runs premarket brief)
"""
import os
import sys
import traceback
from datetime import datetime

from config import (MOCK_MODE, DEBUG_MODE, ACTIVE_MARKETS, KSA_TZ,
                    DAILY_COST_ALERT_USD)
from core import (analyst, brief_composer, sheets, telegram_client,
                  data_router, commands, trades, scout)
from core.logger import log_event, get_log_buffer, get_run_summary, clear_log_buffer
from markets.us import config as us_cfg


PAUSE_FILE = "/tmp/bot_paused.txt"


def main():
    clear_log_buffer()
    brief_type = os.getenv("BRIEF_TYPE", "test").lower()
    log_event("INFO", "main", f"Starting: type={brief_type}, mock={MOCK_MODE}")

    try:
        sheets.initialize_sheet()

        # Commands mode - just poll and exit (no position briefs)
        if brief_type == "commands":
            count = commands.process_commands()
            log_event("INFO", "main", f"Processed {count} commands")
            _flush_logs_to_sheet()
            return 0

        # Check if user paused scheduled briefs
        if os.path.exists(PAUSE_FILE) and brief_type != "test":
            log_event("INFO", "main", "Briefs paused by user, skipping")
            return 0

        if brief_type in ("premarket", "test"):
            run_premarket_brief()
        elif brief_type == "midsession":
            run_midsession_check()
        elif brief_type == "preclose":
            run_preclose_verdict()
        elif brief_type == "eod":
            run_eod_summary()
        else:
            log_event("ERROR", "main", f"Unknown BRIEF_TYPE: {brief_type}")
            return 1

        _flush_logs_to_sheet()

        summary = get_run_summary()
        if summary["total_cost_usd"] > DAILY_COST_ALERT_USD:
            telegram_client.send_error_alert(
                f"Single run cost ${summary['total_cost_usd']:.2f} exceeded "
                f"${DAILY_COST_ALERT_USD} threshold."
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
    """Full pre-market brief: positions + focus + watchlist + scouting."""
    if "US" not in ACTIVE_MARKETS:
        log_event("INFO", "main", "US market not active, skipping")
        return

    positions = sheets.read_positions()
    focus = sheets.read_focus()
    us_positions = [p for p in positions if p.get("Market", "US") == "US"]
    us_focus = [f for f in focus if f.get("Market", "US") == "US"]

    position_analyses = []
    for pos in us_positions:
        ticker = pos.get("Ticker")
        if not ticker:
            continue
        result = analyst.analyze_ticker(ticker, "US", "premarket", position=pos)
        if "error" not in result:
            position_analyses.append(result)

    focus_analyses = []
    held_tickers = {p.get("Ticker") for p in us_positions}
    for f in us_focus:
        ticker = f.get("Ticker")
        if not ticker or ticker in held_tickers:
            continue
        result = analyst.analyze_ticker(ticker, "US", "premarket")
        if "error" not in result:
            focus_analyses.append(result)

    watchlist_analyses = []
    covered = held_tickers | {a["ticker"] for a in focus_analyses}
    watchlist_tickers_set = set()
    for ticker in us_cfg.DEFAULT_WATCHLIST:
        if ticker in covered:
            continue
        watchlist_tickers_set.add(ticker)
        result = analyst.analyze_ticker(ticker, "US", "premarket")
        if "error" not in result:
            watchlist_analyses.append(result)

    # SCOUTING - find new opportunities
    focus_tickers_set = {f.get("Ticker") for f in us_focus if f.get("Ticker")}
    scout_results = scout.scout_opportunities(
        held_tickers=held_tickers,
        focus_tickers=focus_tickers_set,
        watchlist_tickers=watchlist_tickers_set,
    )

    macro = data_router.get_macro()
    summary = get_run_summary()
    run_cost = summary["total_cost_usd"]

    message = brief_composer.compose_premarket_brief(
        position_analyses, watchlist_analyses, focus_analyses,
        scout_results, macro, run_cost
    )
    telegram_client.send_message(message)

    # Process any pending commands while we're awake
    try:
        commands.process_commands()
    except Exception as e:
        log_event("WARN", "main", f"Command processing failed: {e}")

    log_event("INFO", "main", "Pre-market brief sent")


def run_midsession_check():
    """Mid-session: only alert on material changes. Silent otherwise."""
    if "US" not in ACTIVE_MARKETS:
        return

    positions = sheets.read_positions()
    us_positions = [p for p in positions if p.get("Market", "US") == "US"]

    material_changes = []
    for pos in us_positions:
        ticker = pos.get("Ticker")
        if not ticker:
            continue
        current = data_router.get_price(ticker).get("price", 0)
        avg = float(pos.get("AvgCost_USD", 0) or 0)
        stop = float(pos.get("StopLoss", 0) or 0)
        target_str = pos.get("Target")
        target = float(target_str) if target_str else 0
        is_material = (
            (stop and abs(current - stop) / stop < 0.015) or
            (target and abs(current - target) / target < 0.015) or
            (avg and abs(current - avg) / avg > 0.03)
        )
        if is_material:
            result = analyst.analyze_ticker(ticker, "US", "midsession", position=pos)
            if "error" not in result:
                material_changes.append(result)

    if not material_changes:
        log_event("INFO", "main", "No material changes, no alert sent")
        return

    summary = get_run_summary()
    message = brief_composer.compose_midsession_check(material_changes, summary["total_cost_usd"])
    if message:
        telegram_client.send_message(message)
        log_event("INFO", "main", "Mid-session alert sent")


def run_preclose_verdict():
    """Pre-close decisions: hold overnight or exit."""
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
    """End of day: P&L + recap. Uses real trade log."""
    if "US" not in ACTIVE_MARKETS:
        return

    positions = sheets.read_positions()
    us_positions = [p for p in positions if p.get("Market", "US") == "US"]

    pnl = trades.calculate_pnl()
    trades_today = trades.get_todays_trades()

    summary = get_run_summary()
    message = brief_composer.compose_eod_summary(
        us_positions,
        pnl["realized_today_usd"],
        pnl["unrealized_usd"],
        pnl["realized_total_usd"],
        trades_today,
        summary["total_cost_usd"]
    )
    telegram_client.send_message(message)
    log_event("INFO", "main", "EOD summary sent")


def _flush_logs_to_sheet():
    try:
        buffer = get_log_buffer()
        if buffer:
            sheets.append_logs(buffer)
    except Exception as e:
        print(f"Could not flush logs: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
