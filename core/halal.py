"""
Halal screening - dual signal system.
Signal 1: Authoritative list lookup (fast, certain)
Signal 2: AI business model check (for tickers not on list)
Never refuses a trade - just labels it.
"""
from markets.us import config as us_cfg
from markets.saudi import config as saudi_cfg


def check_halal_listed(ticker: str, market: str = "US") -> dict:
    """
    Signal 1: Is ticker on the authoritative Halal-approved list?
    Returns {approved: bool, source: str}
    """
    cfg = us_cfg if market == "US" else saudi_cfg
    ticker_u = ticker.upper()
    if ticker_u in cfg.HALAL_APPROVED:
        return {"listed": True, "source": f"{market} approved list"}
    return {"listed": False, "source": None}


def halal_prompt_context(ticker: str, market: str = "US") -> str:
    """
    Text snippet to embed in Claude's analysis prompt.
    Claude uses this to assess the AI signal (Signal 2).
    """
    listed = check_halal_listed(ticker, market)
    excluded = us_cfg.HALAL_EXCLUDED_SECTORS if market == "US" else saudi_cfg.HALAL_EXCLUDED_SECTORS
    return f"""HALAL CONTEXT for {ticker}:
- On authoritative approved list: {'YES' if listed['listed'] else 'NO'}
- Known excluded business types: {', '.join(sorted(excluded))}
When producing output, include two signals:
  1. list_signal: '✅' if on approved list, blank otherwise
  2. ai_signal: your own assessment based on business model
     - '🟢' if business appears Shariah-compliant
     - '🟡' if uncertain or mixed
     - '🔴' if clearly excluded (banks, alcohol, gambling, etc.)
Treat analysis itself identically regardless of Halal status.
User wants this as informational tags only, not trade restrictions."""


def format_halal_badge(list_signal: bool, ai_signal: str) -> str:
    """Format badges for display in Telegram messages."""
    parts = []
    if list_signal:
        parts.append("✅")
    if ai_signal in ("🟢", "🟡", "🔴"):
        parts.append(ai_signal)
    return " ".join(parts) if parts else ""
