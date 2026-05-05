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
                        "RawJSON", "Source"],
    "MessageMap": ["MessageID", "Date", "Time_KSA", "BriefType", "RecIDs"],
    "WatcherCooldown": ["Ticker", "Date", "AlertCount"],
    "Config": ["Setting", "Value", "Description"],
    # Phase G.2 — option-method state machine. One row per signal,
    # upserted as the signal moves PRE_SIGNAL → TRACKING → DONE.
    "MethodSignals": ["SignalID", "Date", "Time_KSA", "Direction", "State",
                      "TriggerPrice", "StopPrice", "TP1", "TP2", "TP3",
                      "TP1Hit", "TP2Hit", "TP3Hit", "InvalidatedAt",
                      "StateUpdatedAt", "Source"],
    "MethodCooldown": ["Date", "Direction", "LastPreSignalAt",
                       "LastEntryAt", "SetupCount"],
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
def read_positions(market: str = None) -> list:
    """Return list of position dicts, optionally filtered to one market.
    market=None → all markets (legacy behavior). Otherwise filters the
    Market column case-insensitively, defaulting unset cells to 'US'."""
    ss = _get_spreadsheet()
    if ss is None:
        return []
    try:
        ws = ss.worksheet("Positions")
        records = ws.get_all_records()
        if market:
            m = market.upper()
            return [r for r in records
                    if str(r.get("Market", "US") or "US").upper() == m]
        return records
    except Exception as e:
        log_event("ERROR", "sheets", f"Read positions failed: {e}")
        return []


def read_focus(market: str = None) -> list:
    """Return focus stock list, optionally filtered to one market."""
    ss = _get_spreadsheet()
    if ss is None:
        return []
    try:
        ws = ss.worksheet("Focus")
        records = ws.get_all_records()
        if market:
            m = market.upper()
            return [r for r in records
                    if str(r.get("Market", "US") or "US").upper() == m]
        return records
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
            rec.get("source", ""),
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


# ----- Watchlist (stored in Config tab as comma-separated rows) -----
#
# US watchlist lives at Setting=WATCHLIST (legacy key, unchanged for
# backwards compat). SA watchlist lives at Setting=WATCHLIST_SA. Adding
# a new market would mean a new key here and a thin lookup branch
# below — no schema migration needed.

WATCHLIST_KEY = "WATCHLIST"


def _watchlist_key(market: str) -> str:
    m = (market or "US").upper()
    if m == "US":
        return WATCHLIST_KEY
    return f"{WATCHLIST_KEY}_{m}"


def read_watchlist(default: list = None, market: str = "US") -> list:
    """Return the user's customized watchlist for the given market, or
    `default` if unset. US uses the legacy WATCHLIST key; other markets
    use WATCHLIST_<MARKET> (e.g. WATCHLIST_SA).
    """
    cfg = read_config() or {}
    raw = (cfg.get(_watchlist_key(market)) or "").strip()
    if not raw:
        return list(default or [])
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    return parts


def write_watchlist(tickers: list, market: str = "US") -> bool:
    """Persist the watchlist for the given market to the Config tab."""
    clean = [t.strip().upper() for t in tickers if t and t.strip()]
    seen = set()
    deduped = []
    for t in clean:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return write_config(_watchlist_key(market), ",".join(deduped))


# ----- Focus tab writers (max 3 entries, drops oldest) -----

FOCUS_LIMIT = 3


def add_focus(ticker: str, market: str = "US") -> dict:
    """Add a ticker to Focus tab. If at FOCUS_LIMIT, drops the oldest.

    Returns {"ok": bool, "added": str, "dropped": str|None, "error": str}
    """
    ss = _get_spreadsheet()
    if ss is None:
        return {"ok": False, "error": "Sheet not available"}
    ticker = ticker.strip().upper()
    try:
        ws = ss.worksheet("Focus")
        records = ws.get_all_records()
        # Filter to same market for limit-counting purposes
        same_market = [r for r in records if r.get("Market", "US") == market]
        # Already there?
        if any(r.get("Ticker", "").upper() == ticker for r in records):
            return {"ok": False, "error": f"{ticker} is already in focus"}
        dropped = None
        if len(same_market) >= FOCUS_LIMIT:
            # Find the OLDEST same-market entry (top of the list, after header)
            target = same_market[0]
            target_ticker = target.get("Ticker", "")
            # Find its row in the actual sheet to delete
            cell = ws.find(target_ticker, in_column=1)
            if cell is not None:
                ws.delete_rows(cell.row)
                dropped = target_ticker
        # Append the new entry
        ws.append_row([ticker, market,
                       datetime.now(KSA_TZ).strftime("%Y-%m-%d")])
        return {"ok": True, "added": ticker, "dropped": dropped}
    except Exception as e:
        log_event("ERROR", "sheets", f"add_focus({ticker}) failed: {e}")
        return {"ok": False, "error": str(e)}


def remove_focus(ticker: str) -> dict:
    """Remove a ticker from Focus tab. Returns {"ok": bool, "removed": str}."""
    ss = _get_spreadsheet()
    if ss is None:
        return {"ok": False, "error": "Sheet not available"}
    ticker = ticker.strip().upper()
    try:
        ws = ss.worksheet("Focus")
        cell = ws.find(ticker, in_column=1)
        if cell is None:
            return {"ok": False, "error": f"{ticker} is not in focus"}
        ws.delete_rows(cell.row)
        return {"ok": True, "removed": ticker}
    except Exception as e:
        log_event("ERROR", "sheets", f"remove_focus({ticker}) failed: {e}")
        return {"ok": False, "error": str(e)}


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

    Note on type-handling: Google Sheets auto-numericizes string values
    that look like numbers, so even if we write str(message_id) it gets
    stored as a number. The lookup function compensates by trying both.
    """
    ss = _get_spreadsheet()
    if ss is None or not message_id:
        return False
    try:
        ws = ss.worksheet("MessageMap")
        when = datetime.now(KSA_TZ)
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


# ============================================================
# WATCHER COOLDOWN (Phase D.5)
# ============================================================
# Per-ticker per-day alert counter. Increments only when a Telegram
# alert is actually sent (not on every Haiku flag) so a noisy news day
# can't lock the user out before any alert reaches them.
#
# Schema: Ticker | Date | AlertCount  (date format: YYYY-MM-DD KSA)
#
# Race notes: read_cooldown + bump_cooldown is read-modify-write, not
# atomic. Acceptable here because (a) Railway runs single-process and
# (b) the watcher's per-tick lock in webhook/app.py serializes ticks.

def read_cooldown(ticker: str, date_str: str) -> int:
    """Return AlertCount for (ticker, date_str). 0 if no row yet."""
    ss = _get_spreadsheet()
    if ss is None or not ticker or not date_str:
        return 0
    ticker_u = ticker.strip().upper()
    try:
        ws = ss.worksheet("WatcherCooldown")
        records = ws.get_all_records()
        for r in records:
            row_ticker = str(r.get("Ticker", "")).strip().upper()
            row_date = str(r.get("Date", "")).strip()
            if row_ticker == ticker_u and row_date == date_str:
                try:
                    return int(r.get("AlertCount", 0) or 0)
                except (ValueError, TypeError):
                    return 0
        return 0
    except Exception as e:
        log_event("WARN", "sheets",
                  f"read_cooldown({ticker},{date_str}) failed: {e}")
        return 0


def bump_cooldown(ticker: str, date_str: str) -> int:
    """Increment AlertCount for (ticker, date_str). Insert if missing.
    Returns the new count, or 0 if write failed."""
    ss = _get_spreadsheet()
    if ss is None or not ticker or not date_str:
        return 0
    ticker_u = ticker.strip().upper()
    try:
        ws = ss.worksheet("WatcherCooldown")
        records = ws.get_all_records()
        # Row 1 is header; get_all_records starts at row 2.
        for idx, r in enumerate(records, start=2):
            row_ticker = str(r.get("Ticker", "")).strip().upper()
            row_date = str(r.get("Date", "")).strip()
            if row_ticker == ticker_u and row_date == date_str:
                try:
                    cur = int(r.get("AlertCount", 0) or 0)
                except (ValueError, TypeError):
                    cur = 0
                new_count = cur + 1
                ws.update_cell(idx, 3, new_count)
                return new_count
        ws.append_row([ticker_u, date_str, 1])
        return 1
    except Exception as e:
        log_event("WARN", "sheets",
                  f"bump_cooldown({ticker},{date_str}) failed: {e}")
        return 0


def read_cooldowns_for_date(date_str: str) -> list:
    """Return list of {Ticker, AlertCount} dicts for a given date.
    Used by /watcher status to show today's per-ticker counts."""
    ss = _get_spreadsheet()
    if ss is None or not date_str:
        return []
    try:
        ws = ss.worksheet("WatcherCooldown")
        records = ws.get_all_records()
        out = []
        for r in records:
            if str(r.get("Date", "")).strip() == date_str:
                out.append({
                    "Ticker": str(r.get("Ticker", "")).strip().upper(),
                    "AlertCount": int(r.get("AlertCount", 0) or 0),
                })
        return out
    except Exception as e:
        log_event("WARN", "sheets",
                  f"read_cooldowns_for_date({date_str}) failed: {e}")
        return []


def get_recids_for_message(message_id: int) -> list:
    """Look up RecIDs covered by a given message_id. Returns list of strs.

    gspread's `find()` matches by exact value type, but Google Sheets
    silently numericizes integer-shaped strings — so a message_id stored
    via append_row() as "12345" comes back as 12345 (int). We try both
    forms, and fall back to scanning the column manually if both fail.
    """
    ss = _get_spreadsheet()
    if ss is None or not message_id:
        return []
    try:
        ws = ss.worksheet("MessageMap")
        # Try string form first, then numeric, before giving up.
        cell = None
        try:
            cell = ws.find(str(message_id), in_column=1)
        except Exception:
            cell = None
        if cell is None:
            try:
                cell = ws.find(int(message_id), in_column=1)
            except Exception:
                cell = None
        if cell is None:
            # Last-ditch: scan column 1 manually. Protects against
            # gspread quirks across versions and against Sheets storing
            # values in unexpected types.
            target_str = str(message_id)
            try:
                col = ws.col_values(1) or []
            except Exception:
                col = []
            for idx, val in enumerate(col, start=1):
                if str(val).strip() == target_str:
                    rec_id_str = ws.cell(idx, 5).value or ""
                    return [r.strip() for r in rec_id_str.split(",")
                            if r.strip()]
            log_event("INFO", "sheets",
                      f"get_recids_for_message: no match for {message_id}")
            return []
        rec_id_str = ws.cell(cell.row, 5).value or ""
        return [r.strip() for r in rec_id_str.split(",") if r.strip()]
    except Exception as e:
        log_event("WARN", "sheets",
                  f"get_recids_for_message({message_id}) failed: {e}")
        return []


# ============================================================
# METHOD SIGNALS + COOLDOWN (Phase G.2)
# ============================================================
# One row per signal lifecycle in MethodSignals. State transitions
# (PRE_SIGNAL → TRACKING → DONE) upsert by SignalID. MethodCooldown
# is one row per (Date, Direction) tracking last timestamps and the
# setup count for daily-cap enforcement.
#
# Same single-process / single-tick-lock guarantees as
# WatcherCooldown — read-modify-write is acceptable because the
# webhook scheduler serializes method ticks via _method_lock.

def _col_letter(idx: int) -> str:
    """1-indexed column number → A1 letter (A, B, ..., Z, AA, ...)."""
    s = ""
    n = idx
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def read_method_state(signal_id: str) -> dict:
    """Return the MethodSignals row for `signal_id` as a dict, or {}."""
    ss = _get_spreadsheet()
    if ss is None or not signal_id:
        return {}
    try:
        ws = ss.worksheet("MethodSignals")
        records = ws.get_all_records()
        for r in records:
            if str(r.get("SignalID", "")).strip() == signal_id:
                return dict(r)
        return {}
    except Exception as e:
        log_event("WARN", "sheets",
                  f"read_method_state({signal_id}) failed: {e}")
        return {}


def write_method_state(signal_id: str, state: dict) -> bool:
    """Upsert a MethodSignals row keyed by SignalID. `state` keys map
    to the schema headers; missing keys are written as empty cells."""
    ss = _get_spreadsheet()
    if ss is None or not signal_id:
        return False
    headers = SCHEMAS["MethodSignals"]
    row = [state.get(h, "") for h in headers]
    # Make sure SignalID is correct even if caller forgot it
    row[0] = signal_id
    try:
        ws = ss.worksheet("MethodSignals")
        cell = None
        try:
            cell = ws.find(signal_id, in_column=1)
        except Exception:
            cell = None
        if cell is not None:
            end_col = _col_letter(len(headers))
            ws.update(values=[row],
                      range_name=f"A{cell.row}:{end_col}{cell.row}")
        else:
            ws.append_row(row)
        return True
    except Exception as e:
        log_event("WARN", "sheets",
                  f"write_method_state({signal_id}) failed: {e}")
        return False


def read_active_method_signals() -> list:
    """All MethodSignals rows in PRE_SIGNAL or TRACKING state.

    Used by the runner on startup to rehydrate the in-memory tracker
    so a Railway restart mid-trade keeps tracking the same signal
    (instead of forgetting it and emitting a fresh PRE_SIGNAL when the
    setup is still alive).
    """
    ss = _get_spreadsheet()
    if ss is None:
        return []
    try:
        ws = ss.worksheet("MethodSignals")
        records = ws.get_all_records()
        return [dict(r) for r in records
                if str(r.get("State", "")).strip().upper()
                in ("PRE_SIGNAL", "TRACKING")]
    except Exception as e:
        log_event("WARN", "sheets",
                  f"read_active_method_signals failed: {e}")
        return []


def read_method_signals(limit: int = 10) -> list:
    """Most-recent-first list of MethodSignals rows. Used by
    /method history."""
    ss = _get_spreadsheet()
    if ss is None or limit <= 0:
        return []
    try:
        ws = ss.worksheet("MethodSignals")
        records = ws.get_all_records()
        # Rows are append-ordered — newest is at the end. Reverse to
        # get most-recent first.
        return list(reversed(records))[:limit]
    except Exception as e:
        log_event("WARN", "sheets",
                  f"read_method_signals failed: {e}")
        return []


def read_method_cooldown(date_str: str, direction: str) -> dict:
    """Return the cooldown row for (date_str, direction), or {}."""
    ss = _get_spreadsheet()
    if ss is None or not date_str or not direction:
        return {}
    direction = direction.lower().strip()
    try:
        ws = ss.worksheet("MethodCooldown")
        records = ws.get_all_records()
        for r in records:
            if (str(r.get("Date", "")).strip() == date_str
                    and str(r.get("Direction", "")).strip().lower()
                    == direction):
                return dict(r)
        return {}
    except Exception as e:
        log_event("WARN", "sheets",
                  f"read_method_cooldown({date_str},{direction}) failed: {e}")
        return {}


def bump_method_cooldown(date_str: str, direction: str,
                         field: str) -> bool:
    """Update one field on the (date_str, direction) cooldown row,
    inserting the row if missing.

    Behavior by field:
      - LastPreSignalAt / LastEntryAt → set to now (KSA HH:MM:SS).
      - SetupCount → increment by 1.

    Returns True on success, False otherwise. Caller may need to call
    twice (e.g. on PRE_SIGNAL: bump LastPreSignalAt and SetupCount).
    """
    ss = _get_spreadsheet()
    if ss is None or not date_str or not direction:
        return False
    direction = direction.lower().strip()
    if field not in ("LastPreSignalAt", "LastEntryAt", "SetupCount"):
        log_event("WARN", "sheets",
                  f"bump_method_cooldown unknown field: {field}")
        return False
    try:
        ws = ss.worksheet("MethodCooldown")
        records = ws.get_all_records()
        headers = SCHEMAS["MethodCooldown"]
        # Find row index in sheet (records start at row 2)
        target_idx = None
        target_row = None
        for idx, r in enumerate(records, start=2):
            if (str(r.get("Date", "")).strip() == date_str
                    and str(r.get("Direction", "")).strip().lower()
                    == direction):
                target_idx = idx
                target_row = r
                break

        now_str = datetime.now(KSA_TZ).strftime("%H:%M:%S")
        if field == "SetupCount":
            new_val = int(target_row.get("SetupCount", 0) or 0) + 1 \
                if target_row else 1
        else:
            new_val = now_str

        if target_idx is None:
            # Insert: build row with this field set, others empty (or 1
            # for SetupCount on insert).
            blank = {h: "" for h in headers}
            blank["Date"] = date_str
            blank["Direction"] = direction
            blank[field] = new_val
            if field != "SetupCount":
                blank["SetupCount"] = 0
            ws.append_row([blank[h] for h in headers])
            return True

        col = headers.index(field) + 1
        ws.update_cell(target_idx, col, new_val)
        return True
    except Exception as e:
        log_event("WARN", "sheets",
                  f"bump_method_cooldown({date_str},{direction},"
                  f"{field}) failed: {e}")
        return False


def reset_method_cooldown(date_str: str = None) -> bool:
    """Wipe all rows in MethodCooldown (date_str=None) or just rows for
    a specific date. Used by /method reset."""
    ss = _get_spreadsheet()
    if ss is None:
        return False
    try:
        ws = ss.worksheet("MethodCooldown")
        records = ws.get_all_records()
        # Walk from the bottom so deletions don't shift remaining indices
        for idx in range(len(records) + 1, 1, -1):
            r = records[idx - 2]
            if date_str is None or str(r.get("Date", "")).strip() == date_str:
                ws.delete_rows(idx)
        return True
    except Exception as e:
        log_event("WARN", "sheets",
                  f"reset_method_cooldown({date_str}) failed: {e}")
        return False


def reset_method_signals_active() -> bool:
    """Force-DONE all rows currently in PRE_SIGNAL or TRACKING. Used by
    /method reset to clear in-flight state without losing history."""
    ss = _get_spreadsheet()
    if ss is None:
        return False
    try:
        ws = ss.worksheet("MethodSignals")
        records = ws.get_all_records()
        headers = SCHEMAS["MethodSignals"]
        state_col = headers.index("State") + 1
        updated_col = headers.index("StateUpdatedAt") + 1
        now_str = datetime.now(KSA_TZ).strftime("%H:%M:%S")
        for idx, r in enumerate(records, start=2):
            if str(r.get("State", "")).strip().upper() in ("PRE_SIGNAL",
                                                           "TRACKING"):
                ws.update_cell(idx, state_col, "DONE")
                ws.update_cell(idx, updated_col, now_str)
        return True
    except Exception as e:
        log_event("WARN", "sheets",
                  f"reset_method_signals_active failed: {e}")
        return False
