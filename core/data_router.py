"""
Data source router.
Single entry point for all data fetching.
Routes by mock-vs-live AND by market (US → Twelve Data + Marketaux + FRED;
SA → Sahm KSA + Marketaux with .SR suffix).
Every other file imports from here.
"""
from config import MOCK_MODE
from core import mock_sources


def get_price(ticker: str, market: str = "US") -> dict:
    """Get current price + metadata for a ticker."""
    if MOCK_MODE:
        return mock_sources.get_mock_price(ticker)
    if market == "SA":
        from core import sahmk_client
        return sahmk_client.get_quote(ticker)
    from core import live_sources
    return live_sources.get_live_price(ticker)


def get_price_history(ticker: str, days: int = 30,
                     market: str = "US") -> list:
    """Get daily candle history for a ticker."""
    if MOCK_MODE:
        return mock_sources.get_mock_price_history(ticker, days)
    if market == "SA":
        from core import sahmk_client
        return sahmk_client.get_history(ticker, days)
    from core import live_sources
    return live_sources.get_live_price_history(ticker, days)


def get_news(tickers: list = None, hours_back: int = 48,
             market: str = "US") -> list:
    """Get recent news, optionally filtered by tickers.

    For SA market, applies Marketaux's Saudi-symbol suffix to each
    ticker (default ".SR") before querying. Tweak the suffix in
    markets/saudi/config.py if Marketaux's Tadawul convention differs.
    """
    if MOCK_MODE:
        return mock_sources.get_mock_news(tickers, hours_back)
    from core import live_sources
    if market == "SA":
        from markets.saudi import config as sa_cfg
        suffix = getattr(sa_cfg, "MARKETAUX_SYMBOL_SUFFIX", ".SR")
        sa_symbols = [f"{t}{suffix}" for t in (tickers or []) if t]
        return live_sources.get_live_news(sa_symbols, hours_back)
    return live_sources.get_live_news(tickers, hours_back)


def get_macro(market: str = "US") -> dict:
    """Get latest macro indicators (rates, inflation, etc).

    SA macro is not yet wired; returns {} so the analyst handles
    'macro unavailable' gracefully.
    """
    if MOCK_MODE:
        return mock_sources.get_mock_macro()
    if market == "SA":
        return {}
    from core import live_sources
    return live_sources.get_live_macro()


def get_earnings(ticker: str, market: str = "US") -> str | None:
    """Get next earnings date for a ticker. SA: not exposed by SAHMK
    on the free tier — degrade quietly to None."""
    if MOCK_MODE:
        return mock_sources.get_mock_earnings(ticker)
    if market == "SA":
        return None
    from core import live_sources
    return live_sources.get_live_earnings(ticker)


def get_dividend(ticker: str, market: str = "US") -> dict | None:
    """Get upcoming dividend info for a ticker. SA: not wired — None."""
    if MOCK_MODE:
        return mock_sources.get_mock_dividend(ticker)
    if market == "SA":
        return None
    from core import live_sources
    return live_sources.get_live_dividend(ticker)
