"""
Saudi (Tadawul) market configuration — Phase F.

Activated by adding "SA" to the ACTIVE_MARKETS env var. Without that,
all Saudi briefs and watcher ticks are scheduled but no-op silently
(the gate lives in webhook/app.py and the brief functions in main.py).
"""
from datetime import time

MARKET_CODE = "SA"
MARKET_NAME = "Saudi Arabia (Tadawul)"
CURRENCY = "SAR"

# Tadawul hours in KSA local time (KSA has no DST, no tz math required)
OPEN_TIME_KSA = time(10, 0)    # 10:00 AM
CLOSE_TIME_KSA = time(15, 0)   # 3:00 PM

# Saudi weekend is Fri-Sat. Trading days: Sun-Thu.
# Python weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6.
TRADING_DAYS = [6, 0, 1, 2, 3]

# Bot schedule for Saudi (KSA times). EOD intentionally offset to 15:25
# (5 minutes before US premarket at 15:30) so the two markets do NOT
# fire concurrent Sonnet bursts on Mon-Thu when their calendars overlap.
SCHEDULE = {
    "premarket_sa":  time(9, 30),
    "midsession_sa": time(12, 30),
    "preclose_sa":   time(14, 30),
    "eod_sa":        time(15, 25),
}

# Halal screening is intentionally NOT applied for the Saudi market.
# Tadawul-listed equities are screened at the exchange level for
# compliance with KSA frameworks; brief alerts include a footer
# reminding the user to verify compliance themselves.
# See DEPLOY notes for the rationale.
HALAL_SCREENING_ENABLED = False
HALAL_APPROVED = set()  # intentionally empty — see comment above

# Initial focus tickers (Tadawul codes). User can add/remove via
# /focus and /unfocus once activated. These act as defaults when the
# Focus tab has no SA entries.
DEFAULT_FOCUS = ["2222", "7010", "1120"]   # Aramco, STC, Al Rajhi Bank

# Watchlist defaults — start empty; user adds via /watch.
DEFAULT_WATCHLIST = []

# Macro indicators. Saudi macro source not yet wired; data_router
# returns empty dict for SA macro and the analyst handles "macro
# unavailable" gracefully.
MACRO_INDICATORS = ["TASI", "NOMUC"]

# Marketaux ticker symbol suffix for Saudi entities. Tweak here if
# Marketaux's Tadawul convention turns out to be different (e.g. ".SAU"
# or no suffix). The data_router applies this suffix when querying
# news for SA tickers.
MARKETAUX_SYMBOL_SUFFIX = ".SR"
