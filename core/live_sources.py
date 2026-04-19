“””
Real data source adapters.
Each function has the SAME signature as the mock version
so swapping between MOCK_MODE=True/False requires zero code changes elsewhere.
“””
import requests
from datetime import datetime, timedelta
from config import TWELVE_DATA_KEY, MARKETAUX_KEY, FRED_KEY
from core.logger import log_event

# ============================================================

# TWELVE DATA - prices + history

# ============================================================

TD_BASE = “https://api.twelvedata.com”

def get_live_price(ticker: str) -> dict:
“”“Live price from Twelve Data. Falls back gracefully on error.”””
try:
url = f”{TD_BASE}/quote”
params = {“symbol”: ticker, “apikey”: TWELVE_DATA_KEY}
r = requests.get(url, params=params, timeout=10)
r.raise_for_status()
data = r.json()
if “code” in data and data[“code”] != 200:
log_event(“ERROR”, “twelve_data”, f”API error for {ticker}: {data.get(‘message’)}”)
return _fallback_yahoo(ticker)
return {
“price”: float(data[“close”]),
“open”: float(data[“open”]),
“high”: float(data[“high”]),
“low”: float(data[“low”]),
“volume”: int(data.get(“volume”, 0)),
“prev_close”: float(data.get(“previous_close”, data[“close”])),
“change_pct”: float(data.get(“percent_change”, 0)),
“52w_high”: float(data.get(“fifty_two_week”, {}).get(“high”, 0) or 0),
“52w_low”: float(data.get(“fifty_two_week”, {}).get(“low”, 0) or 0),
}
except Exception as e:
log_event(“ERROR”, “twelve_data”, f”Failed {ticker}: {e}”)
return _fallback_yahoo(ticker)

def get_live_price_history(ticker: str, days: int = 30) -> list:
“”“Historical daily candles from Twelve Data.”””
try:
url = f”{TD_BASE}/time_series”
params = {
“symbol”: ticker,
“interval”: “1day”,
“outputsize”: days,
“apikey”: TWELVE_DATA_KEY,
}
r = requests.get(url, params=params, timeout=15)
r.raise_for_status()
data = r.json()
if “values” not in data:
return []
return [
{
“date”: v[“datetime”],
“open”: float(v[“open”]),
“high”: float(v[“high”]),
“low”: float(v[“low”]),
“close”: float(v[“close”]),
“volume”: int(v.get(“volume”, 0)),
}
for v in data[“values”]
][::-1]  # oldest first
except Exception as e:
log_event(“ERROR”, “twelve_data”, f”History fetch failed {ticker}: {e}”)
return []

def _fallback_yahoo(ticker: str) -> dict:
“”“Fallback to Yahoo Finance unofficial JSON. Used when Twelve Data fails.”””
try:
url = f”https://query1.finance.yahoo.com/v8/finance/chart/{ticker}”
r = requests.get(url, timeout=10, headers={“User-Agent”: “Mozilla/5.0”})
r.raise_for_status()
j = r.json()
result = j[“chart”][“result”][0]
meta = result[“meta”]
price = meta.get(“regularMarketPrice”, 0)
prev = meta.get(“previousClose”, price)
return {
“price”: float(price),
“open”: float(meta.get(“regularMarketOpen”, price)),
“high”: float(meta.get(“regularMarketDayHigh”, price)),
“low”: float(meta.get(“regularMarketDayLow”, price)),
“volume”: int(meta.get(“regularMarketVolume”, 0)),
“prev_close”: float(prev),
“change_pct”: round((price - prev) / prev * 100, 2) if prev else 0,
“52w_high”: float(meta.get(“fiftyTwoWeekHigh”, 0)),
“52w_low”: float(meta.get(“fiftyTwoWeekLow”, 0)),
}
except Exception as e:
log_event(“ERROR”, “yahoo_fallback”, f”Failed {ticker}: {e}”)
return {“price”: 0, “open”: 0, “high”: 0, “low”: 0, “volume”: 0,
“prev_close”: 0, “change_pct”: 0, “52w_high”: 0, “52w_low”: 0}

# ============================================================

# MARKETAUX - filtered financial news

# ============================================================

MA_BASE = “https://api.marketaux.com/v1”

def get_live_news(tickers: list = None, hours_back: int = 48) -> list:
“”“Fetch news from Marketaux, filtered by tickers.”””
try:
url = f”{MA_BASE}/news/all”
params = {
“api_token”: MARKETAUX_KEY,
“language”: “en”,
“limit”: 20,
“published_after”: (datetime.utcnow() - timedelta(hours=hours_back)).strftime(”%Y-%m-%dT%H:%M”),
}
if tickers:
params[“symbols”] = “,”.join(tickers)
r = requests.get(url, params=params, timeout=15)
r.raise_for_status()
data = r.json()
articles = []
for item in data.get(“data”, []):
articles.append({
“headline”: item.get(“title”, “”),
“source”: item.get(“source”, “”),
“published”: item.get(“published_at”, “”),
“tickers”: [e.get(“symbol”) for e in item.get(“entities”, []) if e.get(“symbol”)],
“sentiment”: _extract_sentiment(item),
“summary”: item.get(“description”, “”)[:280],
“credibility”: _score_source(item.get(“source”, “”)),
“url”: item.get(“url”, “”),
})
return articles
except Exception as e:
log_event(“ERROR”, “marketaux”, f”News fetch failed: {e}”)
return []

def _extract_sentiment(item: dict) -> float:
“”“Average sentiment across entities in article. -1 to +1.”””
entities = item.get(“entities”, [])
scores = [e.get(“sentiment_score”, 0) for e in entities if e.get(“sentiment_score”) is not None]
return sum(scores) / len(scores) if scores else 0.0

def _score_source(source: str) -> float:
“”“Rough credibility score by source. Higher = more trustworthy.”””
tier1 = [“reuters”, “bloomberg”, “wsj”, “financial times”, “ft”, “associated press”]
tier2 = [“cnbc”, “cbs”, “nbc”, “wapo”, “guardian”, “economist”, “barron”]
s = source.lower()
if any(t in s for t in tier1):
return 0.95
if any(t in s for t in tier2):
return 0.85
return 0.65

# ============================================================

# FRED - macro indicators

# ============================================================

FRED_BASE = “https://api.stlouisfed.org/fred”
FRED_SERIES = {
“FEDFUNDS”: (“Fed Funds Rate”, “%”),
“CPIAUCSL”: (“CPI”, “index”),
“UNRATE”: (“Unemployment Rate”, “%”),
“DGS10”: (“10-Year Treasury”, “%”),
“DCOILWTICO”: (“WTI Crude Oil”, “$/bbl”),
}

def get_live_macro() -> dict:
“”“Latest macro indicators from FRED.”””
results = {}
for code, (label, unit) in FRED_SERIES.items():
try:
url = f”{FRED_BASE}/series/observations”
params = {
“series_id”: code,
“api_key”: FRED_KEY,
“file_type”: “json”,
“sort_order”: “desc”,
“limit”: 2,
}
r = requests.get(url, params=params, timeout=10)
r.raise_for_status()
obs = r.json().get(“observations”, [])
if len(obs) >= 2:
current = float(obs[0][“value”]) if obs[0][“value”] != “.” else None
prev = float(obs[1][“value”]) if obs[1][“value”] != “.” else None
if current is not None:
results[code] = {“value”: current, “prev”: prev or current,
“unit”: unit, “label”: label}
except Exception as e:
log_event(“ERROR”, “fred”, f”Fetch {code} failed: {e}”)
return results

# ============================================================

# EARNINGS (from Yahoo Finance unofficial - often blocked, degrades cleanly)

# ============================================================

# Track failure to stop hammering the endpoint once it blocks us

_yahoo_blocked = {“earnings”: False, “dividend”: False}

def get_live_earnings(ticker: str) -> str | None:
“”“Next earnings date for a ticker. Returns None if unavailable.”””
if _yahoo_blocked[“earnings”]:
return None
try:
url = f”https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}”
params = {“modules”: “earnings,calendarEvents”}
r = requests.get(url, params=params, timeout=10,
headers={“User-Agent”: “Mozilla/5.0”})
if r.status_code == 401:
_yahoo_blocked[“earnings”] = True
log_event(“WARN”, “yahoo_earnings”,
“Yahoo earnings API requires auth - disabling for this run”)
return None
r.raise_for_status()
j = r.json()
result = j.get(“quoteSummary”, {}).get(“result”, [])
if not result:
return None
cal = result[0].get(“calendarEvents”, {}).get(“earnings”, {})
dates = cal.get(“earningsDate”, [])
if dates:
d = dates[0]
if isinstance(d, dict) and “fmt” in d:
return d[“fmt”]
return None
except Exception as e:
log_event(“ERROR”, “yahoo_earnings”, f”Failed {ticker}: {e}”)
return None

# ============================================================

# DIVIDENDS (from Yahoo Finance unofficial - often blocked, degrades cleanly)

# ============================================================

def get_live_dividend(ticker: str) -> dict | None:
“”“Upcoming dividend info for a ticker. Returns None if unavailable.”””
if _yahoo_blocked[“dividend”]:
return None
try:
url = f”https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}”
params = {“modules”: “summaryDetail,calendarEvents”}
r = requests.get(url, params=params, timeout=10,
headers={“User-Agent”: “Mozilla/5.0”})
if r.status_code == 401:
_yahoo_blocked[“dividend”] = True
log_event(“WARN”, “yahoo_dividend”,
“Yahoo dividend API requires auth - disabling for this run”)
return None
r.raise_for_status()
j = r.json()
result = j.get(“quoteSummary”, {}).get(“result”, [])
if not result:
return None
sd = result[0].get(“summaryDetail”, {})
cal = result[0].get(“calendarEvents”, {})
ex_date = cal.get(“exDividendDate”, {}).get(“fmt”)
amount = sd.get(“dividendRate”, {}).get(“raw”)
yield_raw = sd.get(“dividendYield”, {}).get(“raw”)
if ex_date and amount:
return {
“ex_date”: ex_date,
“amount”: float(amount),
“yield_pct”: round(float(yield_raw) * 100, 2) if yield_raw else 0,
}
return None
except Exception as e:
log_event(“ERROR”, “yahoo_dividend”, f”Failed {ticker}: {e}”)
return None
