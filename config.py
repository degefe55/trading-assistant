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
    "analyst": "v1_analyst.txt",
    "filter": "v1_filter.txt",
    "weekly_review": "v1_weekly_review.txt",
    "trade_report": "v1_trade_report.txt",
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
