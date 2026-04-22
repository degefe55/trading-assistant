"""
Google Sheets integration.
Reads: Positions, Focus, Config tabs
Writes: TradeLog, Logs, Reports, Patterns tabs
Uses service account JSON stored in GOOGLE_SA_JSON secret.

Phase A: read-only + write logs
Phase B: full trade log writes triggered by /buy /sell commands
"""
import json
from datetime import datetime
from config import GOOGLE_SHEET_URL, GOOGLE_SA_JSON, KSA_TZ
from core.logger import log_event


_client = None
_spreadsheet = None


def _get_client():
    """Lazy-init gspread client. Returns None if not configured."""
    global _client, _spreadsheet
    if _client is not None:
        return _client
    if not GOOGLE_SA_JSON:
        log_event("WARN", "sheets", "GOOGLE_SA_JSON not set - sheet writes disabled")
        return None
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        info = json.loads(GOOGLE_SA_JSON)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        _client = gspread.authorize(creds)
        _spreadsheet = _client.open_by_url(GOOGLE_SHEET_URL)
        log_event("INFO", "sheets", "Client initialized")
        return _client
    except Exception as e:
        log_event("ERROR", "sheets", f"Client init failed: {e}")
        return None


def _get_spreadsheet():
    """Return the opened spreadsheet."""
    if _spreadsheet is None:
        _get_client()
    return _spreadsheet


# ============================================================
# SCHEMA DEFINITIONS
# ============================================================
SCHEMAS = {
    "Positions": ["Ticker", "Market", "Shares", "AvgCost_USD",
                  "AvgCost_SAR", "StopLoss", "Target", "DateOpened",
                  "Sector", "HalalStatus"],
    "Focus": ["Ticker", "Market", "DateAdded", "CustomNotes"],
    "TradeLog": ["Date", "Time", "Ticker", "Market", "Action", "Shares",
                 "Price_USD", "Price_SAR", "Reason", "LinkedRecID",
                 "PnL_USD", "PnL_SAR", "RunningTotal_USD", "RunningTotal_SAR"],
    "Logs": ["Timestamp", "Level", "Module", "Event", "Details",
             "Tokens", "Cost_USD"],
    "Halal_Approved": ["Ticker", "Market", "Source", "LastVerified"],
    "Patterns": ["Date", "Pattern", "Evidence", "SuggestedAction", "Status"],
    "Reports": ["Date", "Ticker", "RecID", "CallQuality", "WhatHappened",
                "KeyFactor", "LessonLearned"],
    "Config": ["Setting", "Value", "Description"],
}

DEFAULT_CONFIG_ROWS = [
    ["MAX_POSITION_PCT", "25", "Max % of portfolio in one stock"],
    ["MAX_SECTOR_PCT", "60", "Max % in one sector"],
    ["MAX_OPEN_POSITIONS", "5", "Max concurrent positions"],
    ["EARNINGS_BUFFER_DAYS", "3", "Warn if holding into earnings within X days"],
    ["RISK_TOLERANCE", "medium", "low/medium/high - affects bot suggestions"],
    ["USD_TO_SAR", "3.75", "Currency conversion (SAR is pegged)"],
]


def initialize_sheet():
    """
    One-time setup: create all required tabs with headers.
    Safe to run multiple times - skips tabs that exist.
    Called automatically on first run.
    """
    ss = _get_spreadsheet()
    if ss is None:
        log_event("ERROR", "sheets", "Cannot initialize - no spreadsheet access")
        return False

    existing_tabs = {ws.title for ws in ss.worksheets()}

    for tab_name, headers in SCHEMAS.items():
        if tab_name in existing_tabs:
            log_event("INFO", "sheets", f"Tab '{tab_name}' exists, skipping")
            continue
        try:
            ws = ss.add_worksheet(title=tab_name, rows=500, cols=len(headers) + 2)
            ws.append_row(headers)
            log_event("INFO", "sheets", f"Created tab '{tab_name}'")
            if tab_name == "Config":
                for row in DEFAULT_CONFIG_ROWS:
                    ws.append_row(row)
                log_event("INFO", "sheets", "Seeded default config rows")
        except Exception as e:
            log_event("ERROR", "sheets", f"Failed creating '{tab_name}': {e}")

    # Remove the _init placeholder tab if it exists
    if "_init" in existing_tabs:
        try:
            ss.del_worksheet(ss.worksheet("_init"))
            log_event("INFO", "sheets", "Removed _init placeholder tab")
        except Exception as e:
            log_event("WARN", "sheets", f"Could not delete _init: {e}")
    return True


# ============================================================
# READERS
# ============================================================
def read_positions() -> list:
    """Return list of position dicts."""
    ss = _get_spreadsheet()
    if ss is None:
        return []
    try:
        ws = ss.worksheet("Positions")
        records = ws.get_all_records()
        return records
    except Exception as e:
        log_event("ERROR", "sheets", f"Read positions failed: {e}")
        return []


def read_focus() -> list:
    """Return focus stock list."""
    ss = _get_spreadsheet()
    if ss is None:
        return []
    try:
        ws = ss.worksheet("Focus")
        return ws.get_all_records()
    except Exception as e:
        log_event("ERROR", "sheets", f"Read focus failed: {e}")
        return []


def read_config() -> dict:
    """Return config as {setting: value} dict."""
    ss = _get_spreadsheet()
    if ss is None:
        return {}
    try:
        ws = ss.worksheet("Config")
        records = ws.get_all_records()
        return {r["Setting"]: r["Value"] for r in records}
    except Exception as e:
        log_event("ERROR", "sheets", f"Read config failed: {e}")
        return {}


# ============================================================
# WRITERS
# ============================================================
def append_logs(entries: list):
    """Write log entries to Logs tab."""
    ss = _get_spreadsheet()
    if ss is None or not entries:
        return
    try:
        ws = ss.worksheet("Logs")
        rows = [[e["timestamp"], e["level"], e["module"], e["event"],
                 e["data"], e["tokens"], e["cost_usd"]] for e in entries]
        ws.append_rows(rows)
    except Exception as e:
        log_event("ERROR", "sheets", f"Append logs failed: {e}")


def append_trade(trade: dict):
    """Record a buy/sell in TradeLog. Used by /buy /sell commands."""
    ss = _get_spreadsheet()
    if ss is None:
        return
    try:
        ws = ss.worksheet("TradeLog")
        now = datetime.now(KSA_TZ)
        ws.append_row([
            now.strftime("%Y-%m-%d"),
            now.strftime("%H:%M:%S"),
            trade.get("ticker", ""),
            trade.get("market", "US"),
            trade.get("action", ""),
            trade.get("shares", 0),
            trade.get("price_usd", 0),
            trade.get("price_sar", 0),
            trade.get("reason", ""),
            trade.get("linked_rec_id", ""),
            trade.get("pnl_usd", ""),
            trade.get("pnl_sar", ""),
            trade.get("running_total_usd", ""),
            trade.get("running_total_sar", ""),
        ])
        log_event("INFO", "sheets", f"Logged trade: {trade.get('action')} {trade.get('shares')} {trade.get('ticker')}")
    except Exception as e:
        log_event("ERROR", "sheets", f"Append trade failed: {e}")


def append_pattern(pattern: dict):
    """Save a pattern from weekly review."""
    ss = _get_spreadsheet()
    if ss is None:
        return
    try:
        ws = ss.worksheet("Patterns")
        ws.append_row([
            datetime.now(KSA_TZ).strftime("%Y-%m-%d"),
            pattern.get("pattern", ""),
            pattern.get("evidence", ""),
            pattern.get("suggested_action", ""),
            "pending",
        ])
    except Exception as e:
        log_event("ERROR", "sheets", f"Append pattern failed: {e}")


def append_report(report: dict):
    """Save an excellent/terrible call analysis."""
    ss = _get_spreadsheet()
    if ss is None:
        return
    try:
        ws = ss.worksheet("Reports")
        ws.append_row([
            datetime.now(KSA_TZ).strftime("%Y-%m-%d"),
            report.get("ticker", ""),
            report.get("rec_id", ""),
            report.get("call_quality", ""),
            report.get("what_happened", ""),
            report.get("key_factor", ""),
            report.get("lesson", ""),
        ])
    except Exception as e:
        log_event("ERROR", "sheets", f"Append report failed: {e}")
