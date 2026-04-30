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
    "Recommendations": ["RecID", "Date", "Time_KSA", "BriefType", "Ticker",
                        "Market", "Action", "Confidence", "Urgent",
                        "OneLinePlan", "PriceAtCall", "ActionPrice",
                        "StopLoss", "Target", "RiskScore", "HalalAI",
                        "NewsCount", "TopNewsHeadline", "Reasoning",
                        "RawJSON"],
    "MessageMap": ["MessageID", "Date", "Time_KSA", "BriefType", "RecIDs"],
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


def make_rec_id(brief_type: str, ticker: str, when: datetime = None) -> str:
    """Build a sortable, unique RecID for cross-referencing.

    Format: YYYYMMDD-HHMM-BRIEF-TICKER  e.g. 20260427-1530-PRE-SPWO
    """
    when = when or datetime.now(KSA_TZ)
    short_brief = {
        "premarket": "PRE", "midsession": "MID",
        "preclose": "CLO", "eod": "EOD",
        "scouting": "SCT", "routine": "RTN",
    }.get(brief_type, brief_type[:3].upper())
    return f"{when.strftime('%Y%m%d-%H%M')}-{short_brief}-{ticker}"


def append_recommendation(rec: dict) -> str:
    """Persist one recommendation row. Returns the RecID written.

    Called by analyst.analyze_ticker() right after parsing succeeds.
    Failures are logged but never raised — recommendation persistence must
    not break a brief.
    """
    ss = _get_spreadsheet()
    if ss is None:
        return ""
    try:
        when = datetime.now(KSA_TZ)
        rec_id = rec.get("rec_id") or make_rec_id(
            rec.get("brief_type", "routine"),
            rec.get("ticker", "?"),
            when,
        )
        analysis = rec.get("analysis", {}) or {}
        deep = analysis.get("deep_dive", {}) or {}
        snapshot = rec.get("data_snapshot", {}) or {}
        top_news = snapshot.get("top_news") or []
        first_headline = ""
        if top_news:
            first_headline = (top_news[0].get("title", "") or "")[:200]

        # Truncate JSON dump to keep cells reasonable. 1500 chars is plenty
        # for the parsed recommendation, well under Sheets' 50k cap.
        raw_json = json.dumps(analysis, ensure_ascii=False)[:1500]

        ws = ss.worksheet("Recommendations")
        ws.append_row([
            rec_id,
            when.strftime("%Y-%m-%d"),
            when.strftime("%H:%M"),
            rec.get("brief_type", ""),
            rec.get("ticker", ""),
            rec.get("market", "US"),
            analysis.get("action", ""),
            analysis.get("confidence", ""),
            "TRUE" if analysis.get("action_urgent") else "FALSE",
            (analysis.get("one_line_plan", "") or "")[:200],
            rec.get("price_at_call", ""),
            analysis.get("action_price", "") or "",
            deep.get("stop_loss", "") or "",
            deep.get("target", "") or "",
            analysis.get("risk_score", ""),
            analysis.get("halal_ai_signal", ""),
            snapshot.get("news_count", 0),
            first_headline,
            (deep.get("reasoning", "") or "")[:1000],
            raw_json,
        ])
        log_event("INFO", "sheets",
                  f"Logged recommendation {rec_id}: "
                  f"{analysis.get('action')} {rec.get('ticker')}")
        return rec_id
    except Exception as e:
        log_event("ERROR", "sheets", f"Append recommendation failed: {e}")
        return ""


def write_config(setting: str, value: str) -> bool:
    """Update a setting in the Config tab. Adds a new row if not found.

    Returns True on success, False on failure. Used by /settime command
    to persist brief schedule changes across Railway redeploys.
    """
    ss = _get_spreadsheet()
    if ss is None:
        return False
    try:
        ws = ss.worksheet("Config")
        cell = None
        try:
            cell = ws.find(setting, in_column=1)
        except Exception:
            cell = None
        if cell is not None:
            ws.update_cell(cell.row, 2, str(value))
        else:
            ws.append_row([setting, str(value), ""])
        return True
    except Exception as e:
        log_event("ERROR", "sheets", f"Write config failed: {e}")
        return False


def read_recommendation(rec_id: str) -> dict:
    """Look up a single recommendation by RecID. Returns dict or empty.

    Used by /ask and threaded-reply follow-ups, and by deep-dive-on-tap.
    """
    ss = _get_spreadsheet()
    if ss is None or not rec_id:
        return {}
    try:
        ws = ss.worksheet("Recommendations")
        cell = ws.find(rec_id, in_column=1)
        if cell is None:
            return {}
        # Read the entire row, then zip with headers
        headers = ws.row_values(1)
        row = ws.row_values(cell.row)
        # Pad row to header length in case trailing cells are empty
        row = row + [""] * (len(headers) - len(row))
        return dict(zip(headers, row))
    except Exception as e:
        log_event("WARN", "sheets",
                  f"read_recommendation({rec_id}) failed: {e}")
        return {}


def record_message_recids(message_id: int, brief_type: str,
                          rec_ids: list) -> bool:
    """Save which RecIDs a given Telegram message_id covers.

    Used so that when the user replies to a brief, we can look up the
    parent message and find which recommendations to give Claude as
    context for the follow-up question.
    """
    ss = _get_spreadsheet()
    if ss is None or not message_id:
        return False
    try:
        ws = ss.worksheet("MessageMap")
        when = datetime.now(KSA_TZ)
        # Comma-separated list of rec_ids in one cell (Sheets-friendly)
        rec_id_str = ",".join(r for r in rec_ids if r)
        ws.append_row([
            str(message_id),
            when.strftime("%Y-%m-%d"),
            when.strftime("%H:%M"),
            brief_type,
            rec_id_str,
        ])
        return True
    except Exception as e:
        log_event("WARN", "sheets",
                  f"record_message_recids({message_id}) failed: {e}")
        return False


def get_recids_for_message(message_id: int) -> list:
    """Look up RecIDs covered by a given message_id. Returns list of strs."""
    ss = _get_spreadsheet()
    if ss is None or not message_id:
        return []
    try:
        ws = ss.worksheet("MessageMap")
        cell = ws.find(str(message_id), in_column=1)
        if cell is None:
            return []
        # RecIDs is column 5 (1-indexed)
        rec_id_str = ws.cell(cell.row, 5).value or ""
        return [r.strip() for r in rec_id_str.split(",") if r.strip()]
    except Exception as e:
        log_event("WARN", "sheets",
                  f"get_recids_for_message({message_id}) failed: {e}")
        return []
