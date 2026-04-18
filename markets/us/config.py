"""
US Market configuration.
Active market. All times in KSA.
"""
from datetime import time
from config import KSA_TZ

MARKET_CODE = "US"
MARKET_NAME = "United States"
CURRENCY = "USD"
ENABLED = True  # toggled via ACTIVE_MARKETS env var

# Market hours in KSA time (accounts for EST/EDT shift automatically via tz)
# US markets: 9:30 AM - 4:00 PM EST = 4:30 PM - 11:00 PM KSA (summer: 5:30 PM - 12:00 AM KSA)
OPEN_TIME_KSA = time(16, 30)   # 4:30 PM KSA (standard time)
CLOSE_TIME_KSA = time(23, 0)   # 11:00 PM KSA

# Trading days: Mon-Fri in US means Mon-Fri in KSA
TRADING_DAYS = [0, 1, 2, 3, 4]  # Monday=0

# Bot schedule (KSA time, 24h format)
SCHEDULE = {
    "premarket_brief": time(15, 30),      # 3:30 PM - 1h before US open
    "midsession_check": time(19, 30),     # 7:30 PM - mid-session
    "preclose_verdict": time(22, 30),     # 10:30 PM - 30 min before close
    "eod_summary": time(23, 0),           # 11:00 PM - at close
}

# Known halal-approved tickers (S&P Shariah / AAOIFI lists)
# Bot cross-references against this. Others fall through to AI analysis.
HALAL_APPROVED = {
    # Tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "TSLA", "ORCL",
    "ADBE", "CRM", "CSCO", "INTC", "AMD", "QCOM", "AVGO", "TXN",
    "META",  # debatable but typically on lists
    # Shariah ETFs
    "SPUS", "SPWO", "SPSK", "SPRE", "HLAL", "UMMA",
    # Semis / industrials
    "TSM", "ASML", "LRCX", "KLAC", "AMAT",
    # Healthcare
    "JNJ", "UNH", "PFE", "MRK", "ABT", "TMO", "LLY",
    # Consumer
    "PG", "KO", "PEP", "WMT", "COST", "MCD", "NKE",
    # Bitcoin ETFs (ruling varies; listed for convenience, flag for user)
    "IBIT", "FBTC",
    # Energy
    "XOM", "CVX", "COP",
}

# Non-halal categories (for AI flagging)
HALAL_EXCLUDED_SECTORS = {
    "Banks", "Insurance", "Investment Banking", "Gambling",
    "Alcohol", "Tobacco", "Adult Entertainment", "Conventional REITs",
    "Defense/Weapons",
}

# Default watchlist tickers the bot tracks even without positions
DEFAULT_WATCHLIST = ["SPWO", "SPUS", "IBIT"]

# Macro indicators to pull each run (context for analysis)
MACRO_INDICATORS = [
    "SPY",    # S&P 500
    "QQQ",    # Nasdaq
    "VIX",    # Volatility index
    "DXY",    # Dollar index
    "USO",    # Oil
    "GLD",    # Gold
]
