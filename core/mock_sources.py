"""
Mock data sources.
Returns realistic fake data when MOCK_MODE=True.
Lets us build and test the whole pipeline without hitting live APIs.
Same interface as real sources - swap by flipping config.MOCK_MODE.
"""
from datetime import datetime, timedelta
import random

# Deterministic seed so repeated runs give same mock data (easier to debug)
random.seed(42)

# ============================================================
# MOCK PRICES
# ============================================================
MOCK_PRICES = {
    "SPWO": {"price": 31.35, "open": 30.92, "high": 31.45, "low": 30.80,
             "volume": 72000, "prev_close": 30.85, "change_pct": 1.62,
             "52w_high": 31.68, "52w_low": 20.06},
    "SPUS": {"price": 52.48, "open": 51.95, "high": 52.55, "low": 51.80,
             "volume": 610000, "prev_close": 51.90, "change_pct": 1.12,
             "52w_high": 52.55, "52w_low": 35.05},
    "IBIT": {"price": 42.18, "open": 41.85, "high": 42.45, "low": 41.60,
             "volume": 38500000, "prev_close": 41.75, "change_pct": 1.03,
             "52w_high": 71.82, "52w_low": 35.30},
    "AAPL": {"price": 268.40, "open": 265.10, "high": 269.20, "low": 264.80,
             "volume": 48000000, "prev_close": 264.95, "change_pct": 1.30,
             "52w_high": 275.50, "52w_low": 168.20},
    "NVDA": {"price": 142.80, "open": 140.20, "high": 143.50, "low": 139.90,
             "volume": 210000000, "prev_close": 140.10, "change_pct": 1.93,
             "52w_high": 158.30, "52w_low": 94.50},
    "MSFT": {"price": 416.20, "open": 412.40, "high": 417.80, "low": 411.50,
             "volume": 22000000, "prev_close": 411.80, "change_pct": 1.07,
             "52w_high": 469.45, "52w_low": 362.90},
    "SPY": {"price": 702.15, "open": 698.50, "high": 703.20, "low": 698.10,
            "volume": 85000000, "prev_close": 698.80, "change_pct": 0.48,
            "52w_high": 702.40, "52w_low": 510.30},
    "QQQ": {"price": 584.60, "open": 580.10, "high": 586.20, "low": 579.50,
            "volume": 38000000, "prev_close": 580.40, "change_pct": 0.72,
            "52w_high": 584.80, "52w_low": 420.10},
    "VIX": {"price": 14.85, "open": 15.20, "high": 15.40, "low": 14.70,
            "volume": 0, "prev_close": 15.15, "change_pct": -1.98,
            "52w_high": 38.20, "52w_low": 12.50},
    "USO": {"price": 82.40, "open": 82.80, "high": 83.20, "low": 81.90,
            "volume": 3200000, "prev_close": 82.95, "change_pct": -0.66,
            "52w_high": 95.40, "52w_low": 65.80},
    "DXY": {"price": 101.35, "open": 101.50, "high": 101.65, "low": 101.20,
            "volume": 0, "prev_close": 101.52, "change_pct": -0.17,
            "52w_high": 107.20, "52w_low": 98.40},
    "GLD": {"price": 338.20, "open": 336.40, "high": 339.10, "low": 335.80,
            "volume": 8500000, "prev_close": 336.60, "change_pct": 0.48,
            "52w_high": 341.50, "52w_low": 245.60},
}


def get_mock_price(ticker: str) -> dict:
    """Return mock price data for a ticker. Falls back to generated if unknown."""
    if ticker in MOCK_PRICES:
        return MOCK_PRICES[ticker].copy()
    # Fallback for unknown tickers
    base = random.uniform(50, 300)
    return {
        "price": round(base, 2),
        "open": round(base * 0.99, 2),
        "high": round(base * 1.02, 2),
        "low": round(base * 0.98, 2),
        "volume": random.randint(100000, 5000000),
        "prev_close": round(base * 0.995, 2),
        "change_pct": round(random.uniform(-2, 2), 2),
        "52w_high": round(base * 1.25, 2),
        "52w_low": round(base * 0.75, 2),
    }


def get_mock_price_history(ticker: str, days: int = 30) -> list:
    """Return mock daily candles for technical analysis."""
    current = MOCK_PRICES.get(ticker, {"price": 100})["price"]
    history = []
    price = current * 0.90  # start 10% below current, climb up
    for i in range(days):
        daily_move = random.uniform(-0.02, 0.025)
        price = price * (1 + daily_move)
        high = price * (1 + abs(random.uniform(0, 0.015)))
        low = price * (1 - abs(random.uniform(0, 0.015)))
        history.append({
            "date": (datetime.now() - timedelta(days=days - i)).strftime("%Y-%m-%d"),
            "open": round(price * 0.998, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(price, 2),
            "volume": random.randint(50000, 500000),
        })
    return history


# ============================================================
# MOCK NEWS
# ============================================================
MOCK_NEWS = [
    {
        "headline": "US and Iran Weigh Ceasefire Extension as Hormuz Standoff Continues",
        "source": "Bloomberg",
        "published": (datetime.now() - timedelta(hours=3)).isoformat(),
        "tickers": ["SPWO", "USO", "SPY"],
        "sentiment": 0.6,
        "summary": "Mediators pushing for 2-week truce extension to allow deeper negotiations.",
        "credibility": 0.95,
    },
    {
        "headline": "TSMC Raises 2026 Capex Guidance to High End of $52-56B Range",
        "source": "Reuters",
        "published": (datetime.now() - timedelta(hours=5)).isoformat(),
        "tickers": ["NVDA", "SPWO", "AAPL"],
        "sentiment": 0.8,
        "summary": "TSMC cites AI demand, pushing semiconductor capex to upper bound.",
        "credibility": 0.95,
    },
    {
        "headline": "S&P 500 Sets New All-Time High on AI Momentum and Peace Hopes",
        "source": "WSJ",
        "published": (datetime.now() - timedelta(hours=8)).isoformat(),
        "tickers": ["SPY", "QQQ", "SPUS"],
        "sentiment": 0.75,
        "summary": "Index crosses 7,022 as Nasdaq logs 11-day winning streak.",
        "credibility": 0.95,
    },
    {
        "headline": "Fed Officials Signal Patience on Rate Cuts Despite Softer Inflation",
        "source": "Reuters",
        "published": (datetime.now() - timedelta(hours=12)).isoformat(),
        "tickers": ["SPY", "QQQ"],
        "sentiment": -0.1,
        "summary": "Several Fed members urge caution before cutting, citing geopolitical uncertainty.",
        "credibility": 0.95,
    },
    {
        "headline": "Nvidia Unveils Next-Gen AI Chip Roadmap at GTC Conference",
        "source": "CNBC",
        "published": (datetime.now() - timedelta(hours=18)).isoformat(),
        "tickers": ["NVDA", "AMD", "AVGO"],
        "sentiment": 0.7,
        "summary": "New architecture promises 2x performance gains for data center workloads.",
        "credibility": 0.9,
    },
    {
        "headline": "Oil Prices Fall as Supply Concerns Ease on Diplomatic Progress",
        "source": "Bloomberg",
        "published": (datetime.now() - timedelta(hours=22)).isoformat(),
        "tickers": ["USO", "XOM", "CVX"],
        "sentiment": -0.3,
        "summary": "Brent crude slips below $92 on ceasefire optimism.",
        "credibility": 0.95,
    },
    {
        "headline": "Apple iPhone 17 Pre-Orders Exceed Wall Street Expectations",
        "source": "Bloomberg",
        "published": (datetime.now() - timedelta(hours=30)).isoformat(),
        "tickers": ["AAPL"],
        "sentiment": 0.65,
        "summary": "Strong demand in China and Europe drives above-consensus pre-order numbers.",
        "credibility": 0.9,
    },
    {
        "headline": "Microsoft Azure Revenue Growth Accelerates on AI Workloads",
        "source": "WSJ",
        "published": (datetime.now() - timedelta(hours=36)).isoformat(),
        "tickers": ["MSFT"],
        "sentiment": 0.75,
        "summary": "Azure cloud grows 34% YoY, topping estimates; AI services drive expansion.",
        "credibility": 0.9,
    },
]


def get_mock_news(tickers: list = None, hours_back: int = 48) -> list:
    """Return mock news, optionally filtered by tickers."""
    cutoff = datetime.now() - timedelta(hours=hours_back)
    results = []
    for article in MOCK_NEWS:
        published_dt = datetime.fromisoformat(article["published"])
        if published_dt < cutoff:
            continue
        if tickers is None:
            results.append(article.copy())
        elif any(t in article["tickers"] for t in tickers):
            results.append(article.copy())
    return results


# ============================================================
# MOCK MACRO (FRED-style data)
# ============================================================
MOCK_MACRO = {
    "FEDFUNDS": {"value": 4.25, "prev": 4.25, "unit": "%", "label": "Fed Funds Rate"},
    "CPIAUCSL": {"value": 314.2, "prev": 313.1, "unit": "index", "label": "CPI (YoY +3.2%)"},
    "UNRATE": {"value": 4.1, "prev": 4.2, "unit": "%", "label": "Unemployment Rate"},
    "DGS10": {"value": 4.32, "prev": 4.35, "unit": "%", "label": "10-Year Treasury"},
    "DCOILWTICO": {"value": 91.40, "prev": 92.80, "unit": "$/bbl", "label": "WTI Crude Oil"},
}


def get_mock_macro() -> dict:
    """Return mock macro indicators."""
    return {k: v.copy() for k, v in MOCK_MACRO.items()}


# ============================================================
# MOCK EARNINGS CALENDAR
# ============================================================
MOCK_EARNINGS = {
    "AAPL": (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d"),
    "MSFT": (datetime.now() + timedelta(days=12)).strftime("%Y-%m-%d"),
    "NVDA": (datetime.now() + timedelta(days=33)).strftime("%Y-%m-%d"),
    "TSM": (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d"),
}


def get_mock_earnings(ticker: str) -> str | None:
    """Return next earnings date for a ticker, or None."""
    return MOCK_EARNINGS.get(ticker)


# ============================================================
# MOCK DIVIDEND CALENDAR
# ============================================================
MOCK_DIVIDENDS = {
    "SPWO": {"ex_date": (datetime.now() + timedelta(days=65)).strftime("%Y-%m-%d"),
             "amount": 0.18, "yield_pct": 1.3},
    "SPUS": {"ex_date": (datetime.now() + timedelta(days=58)).strftime("%Y-%m-%d"),
             "amount": 0.12, "yield_pct": 0.64},
    "AAPL": {"ex_date": (datetime.now() + timedelta(days=22)).strftime("%Y-%m-%d"),
             "amount": 0.25, "yield_pct": 0.38},
    "MSFT": {"ex_date": (datetime.now() + timedelta(days=18)).strftime("%Y-%m-%d"),
             "amount": 0.83, "yield_pct": 0.80},
}


def get_mock_dividend(ticker: str) -> dict | None:
    """Return upcoming dividend info for a ticker, or None."""
    return MOCK_DIVIDENDS.get(ticker, {}).copy() or None
