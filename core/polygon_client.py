"""
Polygon.io data client — US intraday OHLCV bars.

Mirrors core/sahmk_client.py: a dedicated module for one provider with
its own auth, error handling, and 60-second cache. data_router will be
updated in a follow-up phase to route US intraday requests here; this
file is pure data fetch — no rule logic, no signals, no Telegram.

Auth: ?apiKey=<key> query param (Polygon REST convention). Plan
assumption: Stocks Starter — real-time aggregates with no
per-minute rate limit, but defensive 429 handling is still present.

Failure modes always return [] (or an "ok": False health dict) so the
analyst / watcher can degrade with one warning row instead of crashing.
"""
import math
import time as _time
import threading
from datetime import datetime, timedelta, timezone

import requests

from config import POLYGON_API_KEY
from core.logger import log_event


_BASE_URL = "https://api.polygon.io"
_CACHE_TTL_SEC = 60
_bar_cache = {}                 # (ticker, tf_min, lookback) -> (epoch, bars)
_cache_lock = threading.Lock()

_session = None
_session_lock = threading.Lock()

# Aggregations we fetch. Kept short on purpose — the rule engine the
# next phase plugs in works on 1/5/10-min bars only; anything else
# should be an explicit code change, not a silent accept.
_TIMEFRAME_MAP = {
    1: ("1", "minute"),
    5: ("5", "minute"),
    10: ("10", "minute"),
}

# US regular session ≈ 390 min/day. Used to size the calendar-day
# window when we pick `from_date`.
_MARKET_MIN_PER_DAY = 390
# Extra calendar days to absorb weekends + US market holidays.
_WEEKEND_BUFFER_DAYS = 4


def _has_key() -> bool:
    return bool(POLYGON_API_KEY)


def _get_session():
    """Lazy-init a requests.Session. Polygon takes auth via query param
    so no default headers are needed."""
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is None:
            _session = requests.Session()
    return _session


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

def get_bars(ticker: str, timeframe_min: int,
             lookback_bars: int = 100) -> list:
    """Fetch OHLCV bars from Polygon. Returns oldest-first list of
    normalized dicts; [] on any failure or missing key.

    Bars are sliced to the most recent `lookback_bars`. We over-fetch
    by extending the `from` date to compensate for weekends, holidays,
    and out-of-session time.
    """
    if not ticker:
        return []
    if timeframe_min not in _TIMEFRAME_MAP:
        raise ValueError(
            f"Unsupported timeframe_min={timeframe_min!r}; "
            f"supported: {sorted(_TIMEFRAME_MAP.keys())}"
        )
    if not _has_key():
        log_event("WARN", "polygon",
                  "POLYGON_API_KEY missing; cannot fetch US intraday bars")
        return []

    ticker = str(ticker).strip().upper()
    cache_key = (ticker, timeframe_min, lookback_bars)
    now = _time.time()

    with _cache_lock:
        entry = _bar_cache.get(cache_key)
        if entry and (now - entry[0]) < _CACHE_TTL_SEC:
            return entry[1]

    multiplier, timespan = _TIMEFRAME_MAP[timeframe_min]
    from_date, to_date = _date_range(timeframe_min, lookback_bars)
    url = (f"{_BASE_URL}/v2/aggs/ticker/{ticker}/range/"
           f"{multiplier}/{timespan}/{from_date}/{to_date}")
    params = {
        "adjusted": "true",
        "sort": "asc",
        "limit": 50000,
        "apiKey": POLYGON_API_KEY,
    }

    raw = _request_json(url, params, ticker_for_log=ticker)
    if raw is None:
        return []

    if raw.get("status") != "OK":
        log_event("WARN", "polygon",
                  f"Polygon status={raw.get('status')!r} for {ticker} "
                  f"({timeframe_min}m); "
                  f"message={raw.get('error') or raw.get('message')}")
        return []

    bars = _normalize_bars(raw.get("results") or [], ticker)
    if lookback_bars and len(bars) > lookback_bars:
        bars = bars[-lookback_bars:]

    with _cache_lock:
        _bar_cache[cache_key] = (now, bars)
    return bars


def health_check() -> dict:
    """One-off diagnostic: fetch SPY 1-min last bar. Manual-only — never
    called by the bot. Mirrors sahmk_client.health_check so a future
    /run/polygon_health command has a consistent shape.
    """
    if not _has_key():
        return {"ok": False, "latest_close": None,
                "error": "POLYGON_API_KEY missing", "delayed": False}
    try:
        bars = get_bars("SPY", 1, lookback_bars=1)
    except Exception as e:
        return {"ok": False, "latest_close": None,
                "error": f"exception: {e}", "delayed": False}
    if not bars:
        return {"ok": False, "latest_close": None,
                "error": "no bars returned (see Logs)", "delayed": False}
    return {"ok": True, "latest_close": bars[-1].get("close"),
            "error": None, "delayed": False}


# ---------------------------------------------------------------------------
# Internal: HTTP + parsing
# ---------------------------------------------------------------------------

def _request_json(url: str, params: dict, ticker_for_log: str = "?"):
    """One-shot GET with the graceful-degradation pattern used elsewhere
    in the bot. Returns parsed JSON dict or None."""
    try:
        r = _get_session().get(url, params=params, timeout=15)
    except Exception as e:
        log_event("WARN", "polygon",
                  f"Request failed for {ticker_for_log}: {e}")
        return None

    if r.status_code in (401, 403):
        log_event("ERROR", "polygon",
                  f"Polygon auth rejected ({r.status_code}) for "
                  f"{ticker_for_log} — rotate POLYGON_API_KEY?")
        return None
    if r.status_code == 429:
        log_event("WARN", "polygon",
                  f"Polygon rate limit (429) for {ticker_for_log}")
        return None
    if r.status_code >= 500:
        log_event("WARN", "polygon",
                  f"Polygon server error {r.status_code} for "
                  f"{ticker_for_log}")
        return None
    if not r.ok:
        log_event("WARN", "polygon",
                  f"Polygon {url} returned {r.status_code} for "
                  f"{ticker_for_log}")
        return None

    try:
        return r.json()
    except ValueError as e:
        log_event("WARN", "polygon",
                  f"Polygon non-JSON for {ticker_for_log}: {e}")
        return None


def _date_range(timeframe_min: int, lookback_bars: int):
    """Pick (from_date, to_date) ISO YYYY-MM-DD strings wide enough to
    contain `lookback_bars` bars even with weekends/holidays.

    Sized in *calendar* days = trading-days-needed + weekend buffer.
    Over-fetch is fine: Polygon returns up to 50k bars per call and
    the caller slices to lookback_bars.
    """
    now = datetime.now(timezone.utc)
    minutes_needed = max(1, lookback_bars) * timeframe_min
    trading_days_needed = max(1, math.ceil(minutes_needed / _MARKET_MIN_PER_DAY))
    calendar_days = trading_days_needed + _WEEKEND_BUFFER_DAYS
    from_date = (now - timedelta(days=calendar_days)).strftime("%Y-%m-%d")
    to_date = now.strftime("%Y-%m-%d")
    return from_date, to_date


def _to_float(v):
    try:
        if v is None:
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v):
    try:
        if v is None:
            return None
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _normalize_bars(results, ticker: str) -> list:
    """Map Polygon aggs results onto our internal bar shape. Polygon
    per-bar fields: o/h/l/c/v/t (t is epoch ms). Malformed bars are
    dropped silently; a fully-broken response logs once via the caller.
    """
    if not isinstance(results, list):
        log_event("WARN", "polygon",
                  f"Polygon results for {ticker} not a list "
                  f"(got {type(results).__name__})")
        return []

    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        ts_ms = r.get("t")
        try:
            ts_iso = datetime.fromtimestamp(
                int(ts_ms) / 1000, tz=timezone.utc
            ).isoformat()
        except (TypeError, ValueError):
            continue
        out.append({
            "timestamp": ts_iso,
            "open": _to_float(r.get("o")),
            "high": _to_float(r.get("h")),
            "low": _to_float(r.get("l")),
            "close": _to_float(r.get("c")),
            "volume": _to_int(r.get("v")) or 0,
        })
    return out
