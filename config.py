"""
Central configuration for the trading assistant.
All toggles, timing, and market settings live here.
Edit this file to change bot behavior - no other files need changes.
"""
import os
from zoneinfo import ZoneInfo

# ============================================================
# MODE FLAGS
# ============================================================
# When MOCK_MODE=True, bot uses fake data (for testing, free)
# When MOCK_MODE=False, bot uses live APIs (costs tokens, real data)
MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() == "true"

# When DEBUG_MODE=True, verbose logs, extra Telegram messages, prompt dumps
DEBUG_MODE = os.getenv("DEBUG_MODE", "true").lower() == "true"

# When DRY_RUN=True, bot generates recommendations but marks them [DRY RUN]
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# ============================================================
# MARKET TOGGLES
# ============================================================
# Comma-separated list of active markets: "US", "SAUDI", "US,SAUDI"
# Saudi is built but dormant until data source connected.
ACTIVE_MARKETS = [m.strip().upper() for m in os.getenv("ACTIVE_MARKETS", "US").split(",") if m.strip()]

# ============================================================
# TIMEZONES
# ============================================================
KSA_TZ = ZoneInfo("Asia/Riyadh")
US_TZ = ZoneInfo("America/New_York")

# ============================================================
# COST CONTROLS
# ============================================================
DAILY_COST_ALERT_USD = float(os.getenv("DAILY_COST_ALERT_USD", "1.00"))
MONTHLY_COST_CAP_USD = float(os.getenv("MONTHLY_COST_CAP_USD", "10.00"))

# ============================================================
# CURRENCY
# ============================================================
# SAR is pegged to USD at approximately 3.75
# Bot uses this for display; override via env var if needed
USD_TO_SAR = float(os.getenv("USD_TO_SAR", "3.75"))

# ============================================================
# CLAUDE MODEL CHOICE
# ============================================================
# Haiku for cheap relevance filtering (~$0.25 per million input tokens)
# Sonnet for analysis (~$3 per million input tokens)
CLAUDE_FILTER_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_ANALYST_MODEL = "claude-sonnet-4-6"

# Max tokens per response - keeps outputs tight and cheap
MAX_OUTPUT_TOKENS_FILTER = 200
MAX_OUTPUT_TOKENS_ANALYST = 1500

# ============================================================
# PROMPT VERSIONS
# ============================================================
# Change these to swap prompt versions without code changes.
# Old prompts stay in prompts/ folder for easy rollback.
ACTIVE_PROMPTS = {
    "analyst": "v2_analyst.txt",
    "filter": "v1_filter.txt",
    "weekly_review": "v1_weekly_review.txt",
    "trade_report": "v1_trade_report.txt",
    # Phase D-ish additions:
    "followup": "v1_followup.txt",       # Haiku call answering /ask + threaded replies
    "deepdive": "v1_deepdive.txt",       # Sonnet call when user taps "Deep dive" button
    # Phase D.5 — always-on watcher:
    "watcher_filter": "v1_watcher_filter.txt",  # Haiku per-ticker materiality screen
    "watcher_alert": "v1_watcher_alert.txt",    # Sonnet alert when Haiku flags material
}

# ============================================================
# RISK RULES (can be overridden in Config tab of Sheet)
# ============================================================
DEFAULT_RULES = {
    "max_position_pct": 25,       # Max % of portfolio in one stock
    "max_sector_pct": 60,         # Max % in one sector
    "max_open_positions": 5,      # Max concurrent positions
    "earnings_buffer_days": 3,    # Warn if holding into earnings within X days
    "stop_loss_is_avg_cost": True,  # Your core rule
}

# ============================================================
# PHASE D.5 — ALWAYS-ON WATCHER
# ============================================================
# Python defaults for the watcher. Live values come from the Config tab
# in Google Sheets (keys: WATCHER_ENABLED, WATCHER_PRICE_INTERVAL_MIN,
# WATCHER_NEWS_INTERVAL_MIN, WATCHER_INCLUDE_WATCHLIST,
# WATCHER_DAILY_ALERT_CAP). Sheet wins; these are the fallback when the
# row is missing or unparsable. Env-var-overrideable too, matching the
# rest of this file's convention.
WATCHER_ENABLED = os.getenv("WATCHER_ENABLED", "true").lower() == "true"
WATCHER_PRICE_INTERVAL_MIN = int(os.getenv("WATCHER_PRICE_INTERVAL_MIN", "30"))
WATCHER_NEWS_INTERVAL_MIN = int(os.getenv("WATCHER_NEWS_INTERVAL_MIN", "60"))
WATCHER_INCLUDE_WATCHLIST = os.getenv("WATCHER_INCLUDE_WATCHLIST", "false").lower() == "true"
WATCHER_DAILY_ALERT_CAP = int(os.getenv("WATCHER_DAILY_ALERT_CAP", "3"))

# ============================================================
# API KEYS (from GitHub Secrets at runtime)
# ============================================================
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TWELVE_DATA_KEY = os.getenv("TWELVE_DATA_KEY", "")
MARKETAUX_KEY = os.getenv("MARKETAUX_KEY", "")
FRED_KEY = os.getenv("FRED_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GOOGLE_SHEET_URL = os.getenv("GOOGLE_SHEET_URL", "")
# Service account JSON for writing to Google Sheets (added Phase B)
GOOGLE_SA_JSON = os.getenv("GOOGLE_SA_JSON", "")
# Sahm KSA — Saudi (Tadawul) market data provider (Phase F).
# Free tier: 100 requests/day, 15-min delayed prices.
SAHMK_API_KEY = os.getenv("SAHMK_API_KEY", "")
SAHMK_BASE_URL = "https://app.sahmk.sa/api/v1"
# Polygon.io — US intraday aggregates provider (Phase G prep).
# Stocks Starter plan: real-time bars on /v2/aggs/ticker.
# Still in use for the AI watcher / data_router; the option-method
# runner moved to Databento ES futures for 24h coverage.
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
# Databento — CME futures (ES) for the option-method runner. ES trades
# nearly 24h on Globex so the bot can analyze pre/post US session, and
# ES is the underlying behind TradingView's US500 chart that the
# friend's setup is read on. (Trade execution is still SPX options on
# IBKR — Databento is purely chart-data.)
DATABENTO_API_KEY = os.getenv("DATABENTO_API_KEY", "")
# CME Globex MDP3 — covers ES, NQ, RTY, YM, CL, GC, etc. Override via
# env var if a different feed is needed (e.g. XCME.IFUT, OPRA).
DATABENTO_DATASET = os.getenv("DATABENTO_DATASET", "GLBX.MDP3")
# DATABENTO_SYMBOL_FORMAT — informational. Three ways to name a futures
# contract on Databento; databento_client._stype_in_for() picks
# stype_in automatically based on the shape:
#   parent      "ES.FUT"   umbrella over all open ES contracts
#   continuous  "ES.c.0"   synthetic front-month, auto-rolled at expiry
#   raw_symbol  "ESM6"     specific contract (June 2026 ES here)
# Default METHOD_TICKER below uses parent symbology — Databento returns
# all open ES contracts; the runner picks the one with highest volume
# as the de-facto front month.

# ============================================================
# PHASE G.2 — OPTION-METHOD RULE ENGINE
# ============================================================
# Pure rule engine on ES futures (or another ticker) that fires
# Telegram alerts when the friend's setup forms. Defaults to dormant —
# flip METHOD_ENABLED to true on Railway (or via /method on) to
# activate. Sheet wins for METHOD_ENABLED at runtime; the env var is a
# fallback.
METHOD_ENABLED = os.getenv("METHOD_ENABLED", "false").lower() == "true"
METHOD_INTERVAL_SEC = int(os.getenv("METHOD_INTERVAL_SEC", "60"))
# ES front-month, parent symbology (see DATABENTO_SYMBOL_FORMAT above).
METHOD_TICKER = os.getenv("METHOD_TICKER", "ES.FUT")
# Options-contract price band — read but unused in this phase. Wired
# into the contract picker in Phase G.3.
METHOD_OPTION_PRICE_MIN = float(os.getenv("METHOD_OPTION_PRICE_MIN", "3.00"))
METHOD_OPTION_PRICE_MAX = float(os.getenv("METHOD_OPTION_PRICE_MAX", "3.90"))
METHOD_MAX_DAILY_SIGNALS = int(os.getenv("METHOD_MAX_DAILY_SIGNALS", "20"))
