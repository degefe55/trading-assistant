"""
Sahm KSA API client — Saudi (Tadawul) market quotes and history.

Wraps app.sahmk.sa/api/v1 and exposes the same dict shape the analyst
expects (so brief_composer / technical / data_router don't care which
provider answered). Lives next to live_sources.py rather than inside
it because the auth (X-API-Key header) and rate model (100/day free
tier with 15-min delayed prices) are different enough to warrant a
dedicated surface.

Caching: 60-second per-ticker quote cache. The watcher and briefs
won't normally hit the same ticker twice within 60s, but a manual
/run that overlaps with a scheduled tick can — the cache prevents
double-counting against the 100/day budget.

Failure modes — every error path returns an empty dict / list and
logs. The analyst handles "no price" by producing an error_result,
so the brief continues with a single warning row instead of crashing.
"""
import time as _time
import threading
import requests

from config import SAHMK_API_KEY, SAHMK_BASE_URL
from core.logger import log_event


_CACHE_TTL_SEC = 60
_quote_cache = {}              # ticker -> (epoch, normalized_dict)
_cache_lock = threading.Lock()

_session = None
_session_lock = threading.Lock()


def _has_key() -> bool:
    return bool(SAHMK_API_KEY)


def _get_session():
    """Lazy-init a requests.Session with the X-API-Key header set."""
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            if SAHMK_API_KEY:
                s.headers.update({"X-API-Key": SAHMK_API_KEY})
            _session = s
    return _session


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

def get_quote(ticker: str) -> dict:
    """Return a quote in the bot's internal price-dict shape, or {} on
    failure. 60-second per-ticker cache."""
    if not ticker:
        return {}
    if not _has_key():
        log_event("WARN", "sahmk",
                  "SAHMK_API_KEY missing; cannot fetch SA quotes")
        return {}

    ticker = str(ticker).strip()
    now = _time.time()

    with _cache_lock:
        entry = _quote_cache.get(ticker)
        if entry and (now - entry[0]) < _CACHE_TTL_SEC:
            return entry[1]

    url = f"{SAHMK_BASE_URL}/quote/{ticker}/"
    raw = _request_json("GET", url, ticker_for_log=ticker)
    if raw is None:
        return {}

    normalized = _normalize_quote(raw, ticker)
    if normalized:
        with _cache_lock:
            _quote_cache[ticker] = (now, normalized)
    return normalized


def get_history(ticker: str, days: int = 60) -> list:
    """Daily candle history. Returns [] when the SAHMK plan does not
    expose a history endpoint or the call fails. Analyst will skip
    technical signals (RSI/MACD/Bollinger) when history is empty."""
    if not ticker or not _has_key():
        return []

    ticker = str(ticker).strip()
    url = f"{SAHMK_BASE_URL}/history/{ticker}/"

    try:
        r = _get_session().get(url, params={"days": days}, timeout=15)
    except Exception as e:
        log_event("WARN", "sahmk",
                  f"History request failed for {ticker}: {e}")
        return []

    # 404 specifically: the endpoint path doesn't exist on this plan.
    # Don't escalate to WARN on the assumption — log INFO once.
    if r.status_code == 404:
        log_event("INFO", "sahmk",
                  f"SAHMK history endpoint not available for {ticker} (404)")
        return []
    if r.status_code in (401, 403):
        log_event("WARN", "sahmk",
                  f"SAHMK history forbidden for {ticker} ({r.status_code})")
        return []
    if r.status_code == 429:
        log_event("WARN", "sahmk",
                  f"SAHMK rate limit on history for {ticker}")
        return []
    if not r.ok:
        log_event("WARN", "sahmk",
                  f"SAHMK history {ticker} returned {r.status_code}")
        return []

    try:
        raw = r.json()
    except ValueError as e:
        log_event("WARN", "sahmk",
                  f"SAHMK history {ticker} non-JSON: {e}")
        return []

    return _normalize_history(raw, ticker)


# ---------------------------------------------------------------------------
# Internal: HTTP + parsing
# ---------------------------------------------------------------------------

def _request_json(method: str, url: str, ticker_for_log: str = "?",
                  **kwargs):
    """One-shot HTTP request with the same graceful-degradation pattern
    used elsewhere in the bot. Returns parsed JSON dict/list or None."""
    try:
        r = _get_session().request(method, url, timeout=10, **kwargs)
    except Exception as e:
        log_event("WARN", "sahmk",
                  f"Request to {url} failed for {ticker_for_log}: {e}")
        return None

    if r.status_code == 401:
        log_event("ERROR", "sahmk",
                  f"SAHMK auth rejected (401) for {ticker_for_log} — "
                  f"rotate SAHMK_API_KEY?")
        return None
    if r.status_code == 429:
        log_event("WARN", "sahmk",
                  f"SAHMK rate limit (429) for {ticker_for_log} — "
                  f"free-tier daily quota exhausted")
        return None
    if r.status_code >= 500:
        log_event("WARN", "sahmk",
                  f"SAHMK server error {r.status_code} for "
                  f"{ticker_for_log}")
        return None
    if not r.ok:
        log_event("WARN", "sahmk",
                  f"SAHMK {url} returned {r.status_code} for "
                  f"{ticker_for_log}")
        return None

    try:
        return r.json()
    except ValueError as e:
        log_event("WARN", "sahmk",
                  f"SAHMK {url} non-JSON for {ticker_for_log}: {e}")
        return None


def _to_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_int(v):
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (ValueError, TypeError):
        return None


# Fields we expect from SAHMK per the user's spec (unverified — first
# real call will tell us if the contract holds). If 'price' is missing
# we treat the response as broken; everything else is best-effort.
_EXPECTED_QUOTE_FIELDS = {"symbol", "price", "change_percent",
                          "volume", "is_delayed"}


def _normalize_quote(raw, ticker: str) -> dict:
    """Map a SAHMK /quote response onto the analyst's internal price-dict
    shape. Spec response (per user; unverified):
        symbol, name, name_en, price, change, change_percent,
        volume, is_delayed
    Internal shape used downstream:
        price, change_pct, open, high, low, volume, 52w_low, 52w_high
    """
    if not isinstance(raw, dict):
        log_event("WARN", "sahmk",
                  f"SAHMK quote {ticker} unexpected type "
                  f"{type(raw).__name__}; got {str(raw)[:200]}")
        return {}

    missing = _EXPECTED_QUOTE_FIELDS - set(raw.keys())
    if missing:
        # Log once; if 'price' missing the whole quote is unusable.
        log_event("WARN", "sahmk",
                  f"SAHMK quote {ticker} missing expected fields: "
                  f"{sorted(missing)}; got keys={list(raw.keys())[:12]}")
        if "price" in missing:
            return {}

    return {
        # Analyst-required fields (None where SAHMK doesn't supply)
        "price": _to_float(raw.get("price")),
        "change_pct": _to_float(raw.get("change_percent")),
        "open": _to_float(raw.get("open")),
        "high": _to_float(raw.get("high")),
        "low": _to_float(raw.get("low")),
        "volume": _to_int(raw.get("volume")) or 0,
        "52w_low": _to_float(raw.get("52w_low")
                              or raw.get("week52_low")
                              or raw.get("low_52w")),
        "52w_high": _to_float(raw.get("52w_high")
                               or raw.get("week52_high")
                               or raw.get("high_52w")),
        # Saudi-specific extras for display / debugging
        "symbol": raw.get("symbol") or ticker,
        "name": raw.get("name_en") or raw.get("name") or "",
        "is_delayed": bool(raw.get("is_delayed", True)),
        "currency": "SAR",
        "source": "sahmk",
    }


def _normalize_history(raw, ticker: str) -> list:
    """Map SAHMK history response onto the internal candle-list shape
    used by core/technical.py. Tolerates either a top-level list or a
    {data: [...]} wrapper.
    """
    if isinstance(raw, list):
        candles = raw
    elif isinstance(raw, dict):
        candles = (raw.get("data") or raw.get("history")
                   or raw.get("candles") or [])
    else:
        candles = []

    if not isinstance(candles, list):
        log_event("INFO", "sahmk",
                  f"SAHMK history {ticker}: no candle list found in response")
        return []

    out = []
    for c in candles:
        if not isinstance(c, dict):
            continue
        out.append({
            "date": c.get("date") or c.get("datetime") or c.get("time") or "",
            "open": _to_float(c.get("open")),
            "high": _to_float(c.get("high")),
            "low": _to_float(c.get("low")),
            "close": _to_float(c.get("close") or c.get("price")),
            "volume": _to_int(c.get("volume")),
        })
    return out


# ---------------------------------------------------------------------------
# Test helper (manual-only; not called by the bot)
# ---------------------------------------------------------------------------

def health_check() -> dict:
    """One-off diagnostic: hit /quote/2222/ and report the shape we got.
    Useful from a Python REPL or manual /run/sahmk_health endpoint when
    we wire one. Never called automatically."""
    if not _has_key():
        return {"ok": False, "error": "SAHMK_API_KEY missing"}
    raw = _request_json("GET", f"{SAHMK_BASE_URL}/quote/2222/",
                        ticker_for_log="2222")
    if raw is None:
        return {"ok": False, "error": "request failed (see Logs)"}
    return {
        "ok": True,
        "raw_keys": list(raw.keys()) if isinstance(raw, dict) else None,
        "normalized": _normalize_quote(raw, "2222"),
    }
