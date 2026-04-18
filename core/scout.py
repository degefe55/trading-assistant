"""
Scouting module - finds new trading opportunities.
Flow:
1. Pull candidate universe (market movers + news mentions)
2. Filter (price, market cap, already-held)
3. Haiku-score each candidate for "worth deeper look?"
4. Sonnet deep-dive on top 3 max
5. Return only quality finalists (silence is acceptable)
"""
import json
import re
import requests
from config import MOCK_MODE, TWELVE_DATA_KEY, MARKETAUX_KEY
from core import claude_client, analyst
from core.logger import log_event


# Filter thresholds - tune here
MIN_MARKET_CAP = 1_000_000_000   # $1B
MIN_PRICE = 5.00
MAX_FINALISTS = 3
HAIKU_SCORE_THRESHOLD = 7  # 0-10 scale, only ≥7 becomes a finalist


def scout_opportunities(held_tickers: set, focus_tickers: set,
                        watchlist_tickers: set) -> list:
    """
    Main entry point. Returns list of full analysis dicts (0-3 items).
    Empty list is a valid, expected output - silence is golden.
    """
    log_event("INFO", "scout", "Starting opportunity scan")

    # Skip list - already covered by main pipeline
    skip = held_tickers | focus_tickers | watchlist_tickers

    # 1. Get candidates
    candidates = _get_candidate_universe()
    candidates = [c for c in candidates if c["ticker"] not in skip]
    log_event("INFO", "scout", f"Candidate universe: {len(candidates)} tickers")

    if not candidates:
        return []

    # 2. Hard filters (no Claude cost)
    filtered = []
    for c in candidates:
        if c.get("price", 0) < MIN_PRICE:
            continue
        mc = c.get("market_cap", 0)
        if mc and mc < MIN_MARKET_CAP:
            continue
        filtered.append(c)
    log_event("INFO", "scout", f"After hard filters: {len(filtered)}")

    # 3. Haiku relevance scoring
    scored = []
    for c in filtered[:20]:  # cap Haiku calls at 20
        score = _haiku_score(c)
        if score >= HAIKU_SCORE_THRESHOLD:
            c["scout_score"] = score
            scored.append(c)

    # 4. Take top N, sort by score descending
    scored.sort(key=lambda x: x["scout_score"], reverse=True)
    finalists = scored[:MAX_FINALISTS]
    log_event("INFO", "scout", f"Finalists: {[f['ticker'] for f in finalists]}")

    # 5. Full Sonnet analysis on finalists only
    results = []
    for f in finalists:
        result = analyst.analyze_ticker(f["ticker"], "US", "scouting")
        if "error" not in result:
            result["scout_catalyst"] = f.get("catalyst", "")
            result["scout_score"] = f["scout_score"]
            results.append(result)

    return results


def _get_candidate_universe() -> list:
    """Pull candidates from movers + news mentions."""
    if MOCK_MODE:
        return _mock_candidates()
    return _live_candidates()


def _mock_candidates() -> list:
    """Realistic mock for offline testing."""
    return [
        {"ticker": "AVGO", "price": 215.40, "market_cap": 1_010_000_000_000,
         "change_pct": 3.2, "catalyst": "Meta custom AI chip deal announced"},
        {"ticker": "AMD", "price": 148.30, "market_cap": 240_000_000_000,
         "change_pct": 2.1, "catalyst": "Analyst upgrade Wells Fargo, PT $175"},
        {"ticker": "ORCL", "price": 185.20, "market_cap": 515_000_000_000,
         "change_pct": 4.1, "catalyst": "Azure rival cloud growth acceleration"},
        {"ticker": "PLTR", "price": 28.40, "market_cap": 63_000_000_000,
         "change_pct": -1.8, "catalyst": "No specific catalyst"},
        {"ticker": "TSLA", "price": 268.50, "market_cap": 850_000_000_000,
         "change_pct": 7.3, "catalyst": "Musk AI5 chip update at earnings"},
        {"ticker": "GOOGL", "price": 192.40, "market_cap": 2_380_000_000_000,
         "change_pct": 0.4, "catalyst": "No specific catalyst"},
        {"ticker": "SHOP", "price": 98.20, "market_cap": 127_000_000_000,
         "change_pct": 1.2, "catalyst": "No specific catalyst"},
    ]


def _live_candidates() -> list:
    """Live candidate generation from Twelve Data movers + Marketaux news."""
    candidates = []
    seen_tickers = set()

    # Source 1: Most mentioned in news last 24h
    try:
        url = "https://api.marketaux.com/v1/news/all"
        params = {
            "api_token": MARKETAUX_KEY,
            "language": "en",
            "limit": 50,
            "filter_entities": "true",
            "group_similar": "true",
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        articles = r.json().get("data", [])
        ticker_mentions = {}
        for a in articles:
            for e in a.get("entities", []):
                sym = e.get("symbol")
                if sym and sym.isupper() and len(sym) <= 5:
                    ticker_mentions[sym] = ticker_mentions.get(sym, 0) + 1
                    if sym not in seen_tickers:
                        seen_tickers.add(sym)

        # Top 15 by mention count
        top = sorted(ticker_mentions.items(), key=lambda x: x[1], reverse=True)[:15]
        for ticker, count in top:
            # Fetch price + market cap
            price_data = _fetch_light_quote(ticker)
            if not price_data:
                continue
            candidates.append({
                "ticker": ticker,
                "price": price_data.get("price", 0),
                "market_cap": price_data.get("market_cap", 0),
                "change_pct": price_data.get("change_pct", 0),
                "catalyst": f"Mentioned in {count} news articles",
                "news_mentions": count,
            })
    except Exception as e:
        log_event("ERROR", "scout", f"News mention fetch failed: {e}")

    # Source 2: Twelve Data movers (top % gainers/losers US stocks)
    try:
        # Not all free tiers have this endpoint - try, fall back if 403
        url = "https://api.twelvedata.com/market_movers/stocks"
        params = {"apikey": TWELVE_DATA_KEY, "direction": "gainers",
                  "country": "United States", "outputsize": 10}
        r = requests.get(url, params=params, timeout=15)
        if r.ok:
            movers = r.json().get("values", [])
            for m in movers:
                sym = m.get("symbol")
                if not sym or sym in seen_tickers:
                    continue
                candidates.append({
                    "ticker": sym,
                    "price": float(m.get("last", 0)),
                    "market_cap": 0,  # not always provided
                    "change_pct": float(m.get("percent_change", 0)),
                    "catalyst": f"Top gainer +{m.get('percent_change')}%",
                })
                seen_tickers.add(sym)
    except Exception as e:
        log_event("WARN", "scout", f"Movers endpoint unavailable: {e}")

    return candidates


def _fetch_light_quote(ticker: str) -> dict | None:
    """Lightweight price+cap fetch for scouting (not full analysis)."""
    try:
        url = "https://api.twelvedata.com/quote"
        params = {"symbol": ticker, "apikey": TWELVE_DATA_KEY}
        r = requests.get(url, params=params, timeout=10)
        if not r.ok:
            return None
        d = r.json()
        if "code" in d and d["code"] != 200:
            return None
        return {
            "price": float(d.get("close", 0)),
            "change_pct": float(d.get("percent_change", 0)),
            "market_cap": 0,  # not in quote endpoint, would need statistics endpoint
        }
    except Exception:
        return None


def _haiku_score(candidate: dict) -> int:
    """Use Haiku to score candidate 0-10 for deep-dive worthiness."""
    system = claude_client.load_prompt("filter")
    user = (f"Candidate: {candidate['ticker']} at ${candidate['price']} "
            f"({candidate['change_pct']:+.1f}% today)\n"
            f"Catalyst: {candidate.get('catalyst', 'none')}\n\n"
            f"Context: User trades daily swing trades in US equities, "
            f"tight stop loss, Halal-aware. "
            f"Is this worth a full analysis today? Score 0-10.")

    response, _ = claude_client.call_filter(system, user)
    if not response:
        return 0

    # Try JSON parse first
    match = re.search(r'"score"\s*:\s*(\d+)', response)
    if match:
        return int(match.group(1))

    # Fallback: find first number
    match = re.search(r"\b(\d+)\b", response)
    if match:
        score = int(match.group(1))
        return min(max(score, 0), 10)

    return 0
