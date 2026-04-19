"""
Saudi (Tadawul) market configuration.
DORMANT MODULE - structure ready, data source not yet connected.
When ready to activate: add data source in data_source.py and
add "SAUDI" to ACTIVE_MARKETS env var.
"""
from datetime import time

MARKET_CODE = "SAUDI"
MARKET_NAME = "Saudi Arabia (Tadawul)"
CURRENCY = "SAR"
ENABLED = False  # flip to True when data source connected

# Tadawul hours in KSA local time (KSA = local, so no tz math needed)
OPEN_TIME_KSA = time(10, 0)    # 10:00 AM
CLOSE_TIME_KSA = time(15, 0)   # 3:00 PM

# Saudi weekend is Fri-Sat, trading Sun-Thu
TRADING_DAYS = [6, 0, 1, 2, 3]  # Sun=6, Mon=0, ..., Thu=3

# Bot schedule for Saudi (different from US)
SCHEDULE = {
    "premarket_brief": time(9, 30),
    "midsession_check": time(12, 30),
    "preclose_verdict": time(14, 30),
    "eod_summary": time(15, 15),
}

# Known halal-compliant Saudi tickers (most Saudi stocks are compliant by default,
# but some are explicitly excluded - banks, conventional insurance)
HALAL_APPROVED = {
    # Energy / Petrochemicals (most are halal)
    "2222",  # Aramco
    "2010",  # SABIC
    "2020",  # SABIC Agri-Nutrients
    # Telecom
    "7010",  # STC
    "7020",  # Etihad Etisalat (Mobily)
    "7030",  # Zain KSA
    # Retail / Consumer
    "4001",  # Almarai
    "4013",  # Sulaiman Al Habib
    "4190",  # Jarir
    # Islamic banks (compliant)
    "1120",  # Al Rajhi Bank
    "1111",  # Saudi National Bank (partially compliant)
    "1180",  # Al Rajhi Capital
    # Materials
    "1211",  # Maaden
}

HALAL_EXCLUDED_SECTORS = {
    "Conventional Banking", "Conventional Insurance",
    "Gambling", "Alcohol", "Tobacco",
}

DEFAULT_WATCHLIST = []  # add when activating
MACRO_INDICATORS = ["TASI", "NOMUC"]  # Saudi indexes
