"""
Data source router.
Single entry point for all data fetching.
Swaps between mock and live based on config.MOCK_MODE.
Every other file in the codebase imports from here.
"""
from config import MOCK_MODE
from core import mock_sources


def get_price(ticker: str) -> dict:
    """Get current price + metadata for a ticker."""
    if MOCK_MODE:
        return mock_sources.get_mock_price(ticker)
    from core import live_sources
    return live_sources.get_live_price(ticker)


def get_price_history(ticker: str, days: int = 30) -> list:
    """Get daily candle history for a ticker."""
    if MOCK_MODE:
        return mock_sources.get_mock_price_history(ticker, days)
    from core import live_sources
    return live_sources.get_live_price_history(ticker, days)


def get_news(tickers: list = None, hours_back: int = 48) -> list:
    """Get recent news, optionally filtered by tickers."""
    if MOCK_MODE:
        return mock_sources.get_mock_news(tickers, hours_back)
    from core import live_sources
    return live_sources.get_live_news(tickers, hours_back)


def get_macro() -> dict:
    """Get latest macro indicators (rates, inflation, etc)."""
    if MOCK_MODE:
        return mock_sources.get_mock_macro()
    from core import live_sources
    return live_sources.get_live_macro()


def get_earnings(ticker: str) -> str | None:
    """Get next earnings date for a ticker."""
    if MOCK_MODE:
        return mock_sources.get_mock_earnings(ticker)
    from core import live_sources
    return live_sources.get_live_earnings(ticker)


def get_dividend(ticker: str) -> dict | None:
    """Get upcoming dividend info for a ticker."""
    if MOCK_MODE:
        return mock_sources.get_mock_dividend(ticker)
    from core import live_sources
    return live_sources.get_live_dividend(ticker)
