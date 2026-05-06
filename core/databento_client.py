"""
Databento data client — CME futures bars (ES, NQ, etc.) via REST.

Mirrors core/polygon_client.py: a dedicated module for one provider
with its own auth, error handling, and 60-second cache.

Used by the option-method runner in place of Polygon SPY: the bot
analyzes ES futures because (1) ES trades nearly 24h on Globex so
the runner can tick during pre/post US session, and (2) ES is the
underlying behind TradingView's US500 chart that the friend's setup
is read on. Trade execution stays on SPX options at IBKR — Databento
is purely chart-data.

Auth: HTTP Basic with the API key as username (Databento convention,
empty password). Plan assumption: any tier with historical OHLCV
access on GLBX.MDP3.

Failure modes always return [] (or an "ok": False health dict) so the
runner can degrade with one warning row instead of crashing.
"""
import json
import time as _time
import threading
from datetime import datetime, timedelta, timezone

import requests

from config import DATABENTO_API_KEY, DATABENTO_DATASET
from core.logger import log_event


_BASE_URL = "https://hist.databento.com/v0"
_CACHE_TTL_SEC = 60
_bar_cache = {}                 # (symbol, tf_min, lookback) -> (epoch, bars)
_cache_lock = threading.Lock()

_session = None
_session_lock = threading.Lock()

# Supported intraday timeframes in minutes. 5m and 10m are not native
# Databento schemas; we fetch ohlcv-1m and aggregate locally to keep
# one API path.
_SUPPORTED_TIMEFRAMES = (1, 5, 10)

# ES Globex closes for 60min/day (22:00–23:00 ET) and a full weekend
# Friday close → Sunday open. The fetch window is sized so even after
# those gaps we still have lookback_bars bars left after aggregation.
_WEEKEND_BUFFER_HOURS = 72


def _has_key() -> bool:
    return bool(DATABENTO_API_KEY)


def _get_session():
    """Lazy-init a requests.Session with HTTP Basic auth set. Databento
    auth is `<api_key>:` — key as username, empty password."""
    global _session
    if _session is not None:
        return _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            if DATABENTO_API_KEY:
                s.auth = (DATABENTO_API_KEY, "")
            _session = s
    return _session


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

def get_bars(symbol: str, timeframe_min: int,
             lookback_bars: int = 100) -> list:
    """Fetch OHLCV bars from Databento. Returns oldest-first list of
    normalized dicts; [] on any failure or missing key.

    Bar shape matches polygon_client.get_bars exactly:
        {"timestamp": ISO, "open", "high", "low", "close", "volume"}

    Always fetches ohlcv-1m natively. For 5m/10m we aggregate the 1m
    bars locally — Databento has no ohlcv-5m / ohlcv-10m schema.
    """
    if not symbol:
        return []
    if timeframe_min not in _SUPPORTED_TIMEFRAMES:
        raise ValueError(
            f"Unsupported timeframe_min={timeframe_min!r}; "
            f"supported: {list(_SUPPORTED_TIMEFRAMES)}"
        )
    if not _has_key():
        log_event("WARN", "databento",
                  "DATABENTO_API_KEY missing; cannot fetch ES bars")
        return []

    symbol = str(symbol).strip()
    cache_key = (symbol, timeframe_min, lookback_bars)
    now = _time.time()

    with _cache_lock:
        entry = _bar_cache.get(cache_key)
        if entry and (now - entry[0]) < _CACHE_TTL_SEC:
            return entry[1]

    minutes_needed = max(1, lookback_bars) * timeframe_min
    # Pad x2 for the Globex maintenance gap + intraday lulls; floor at
    # 24h so a small request still survives the weekend.
    window_minutes = max(
        24 * 60,
        minutes_needed * 2 + _WEEKEND_BUFFER_HOURS * 60
    )
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(minutes=window_minutes)

    raw_bars = _fetch_ohlcv_1m(symbol, start_dt, end_dt)
    if not raw_bars:
        return []

    if timeframe_min == 1:
        bars = raw_bars
    else:
        bars = _aggregate(raw_bars, timeframe_min)

    if lookback_bars and len(bars) > lookback_bars:
        bars = bars[-lookback_bars:]

    with _cache_lock:
        _bar_cache[cache_key] = (now, bars)
    return bars


def health_check() -> dict:
    """One-off diagnostic: fetch 1 ES 1-min bar via get_bars + the
    configured METHOD_TICKER. Manual-only — never called by the bot
    on a schedule. Mirrors polygon_client.health_check shape so a
    /method_health command can dispatch identically.
    """
    if not _has_key():
        return {"ok": False, "latest_close": None,
                "error": "DATABENTO_API_KEY missing", "delayed": False}
    from config import METHOD_TICKER
    try:
        bars = get_bars(METHOD_TICKER, 1, lookback_bars=1)
    except Exception as e:
        return {"ok": False, "latest_close": None,
                "error": f"exception: {e}", "delayed": False}
    if not bars:
        return {"ok": False, "latest_close": None,
                "error": "no bars returned (see Logs)", "delayed": False}
    return {"ok": True, "latest_close": bars[-1].get("close"),
            "error": None, "delayed": False}


# ---------------------------------------------------------------------------
# Internal: Databento HTTP call
# ---------------------------------------------------------------------------

def _stype_in_for(symbol: str) -> str:
    """Pick the right symbology input for Databento.

    Three ways to name an ES contract:
      - parent      "ES.FUT"   umbrella over all open ES contracts
                               (we filter to highest-volume after fetch)
      - continuous  "ES.c.0"   synthetic front-month, auto-rolled
      - raw_symbol  "ESM6"     specific contract (June 2026)
    """
    s = symbol.strip().upper()
    if s.endswith(".FUT") or s.endswith(".OPT"):
        return "parent"
    parts = s.split(".")
    if len(parts) == 3 and parts[1] in ("C", "N"):
        return "continuous"
    return "raw_symbol"


def _fetch_ohlcv_1m(symbol: str, start_dt: datetime,
                    end_dt: datetime) -> list:
    """Pull ohlcv-1m records from Databento and normalize to internal
    bar dicts. Returns [] on any failure.

    For parent symbology (ES.FUT), Databento returns rows for every
    open contract; we filter to the single highest-volume one after
    parse to approximate the front month.
    """
    url = f"{_BASE_URL}/timeseries.get_range"
    stype_in = _stype_in_for(symbol)
    params = {
        "dataset": DATABENTO_DATASET,
        "symbols": symbol,
        "schema": "ohlcv-1m",
        "start": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "end": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "stype_in": stype_in,
        "encoding": "json",
        "pretty_ts": "true",
        "pretty_px": "true",
    }
    text = _request_text(url, params, symbol_for_log=symbol)
    if text is None:
        return []
    bars = _parse_ohlcv_ndjson(text, symbol)
    if not bars:
        return []
    # Parent symbology pulls every open contract; pick the most-active
    # instrument as a proxy for the front month so the rule engine
    # sees a single coherent series.
    if stype_in == "parent":
        bars = _select_most_active_instrument(bars, symbol)
    # Strip the now-redundant grouping field before returning.
    for b in bars:
        b.pop("_instrument", None)
    return bars


def _request_text(url: str, params: dict, symbol_for_log: str = "?"):
    """One-shot GET that returns the response body as text (NDJSON)
    or None on failure. Mirrors polygon_client._request_json's degraded-
    fail behavior."""
    try:
        r = _get_session().get(url, params=params, timeout=20)
    except Exception as e:
        log_event("WARN", "databento",
                  f"Request failed for {symbol_for_log}: {e}")
        return None

    if r.status_code in (401, 403):
        log_event("ERROR", "databento",
                  f"Databento auth rejected ({r.status_code}) for "
                  f"{symbol_for_log} — rotate DATABENTO_API_KEY?")
        return None
    if r.status_code == 429:
        log_event("WARN", "databento",
                  f"Databento rate limit (429) for {symbol_for_log}")
        return None
    if r.status_code >= 500:
        log_event("WARN", "databento",
                  f"Databento server error {r.status_code} for "
                  f"{symbol_for_log}")
        return None
    if not r.ok:
        body_snippet = (r.text or "")[:200]
        log_event("WARN", "databento",
                  f"Databento returned {r.status_code} for "
                  f"{symbol_for_log}: {body_snippet}")
        return None
    return r.text or ""


def _parse_ohlcv_ndjson(text: str, symbol_for_log: str) -> list:
    """Newline-delimited-JSON → list of bar dicts. With pretty_ts +
    pretty_px the fields are human-friendly: ISO timestamps and decimal
    prices. We attach `_instrument` (instrument_id or symbol) so the
    parent-symbology filter can group by contract."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue

        # Databento sometimes nests the record header under "hd". Look
        # there as a fallback so the parser works across response
        # variants.
        hd = rec.get("hd") if isinstance(rec.get("hd"), dict) else {}
        ts = rec.get("ts_event") or hd.get("ts_event")
        if isinstance(ts, str):
            ts_iso = ts
        else:
            try:
                ts_ns = int(ts)
                ts_iso = datetime.fromtimestamp(
                    ts_ns / 1_000_000_000, tz=timezone.utc
                ).isoformat()
            except (TypeError, ValueError):
                continue

        instrument = (rec.get("symbol") or rec.get("instrument_id")
                      or hd.get("instrument_id") or "")

        out.append({
            "timestamp": ts_iso,
            "open":  _to_float(rec.get("open")),
            "high":  _to_float(rec.get("high")),
            "low":   _to_float(rec.get("low")),
            "close": _to_float(rec.get("close")),
            "volume": _to_int(rec.get("volume")) or 0,
            "_instrument": str(instrument),
        })
    if not out:
        log_event("WARN", "databento",
                  f"Databento {symbol_for_log}: parsed 0 bars from "
                  f"response ({len(text)} chars)")
    # Databento returns oldest-first by ts_event for time-series
    # queries; preserve order.
    return out


def _select_most_active_instrument(bars: list, symbol_for_log: str) -> list:
    """For parent-symbol responses, pick the single contract with the
    highest cumulative volume across the window. That's the front
    month except very close to a roll, which is good enough for the
    rule engine and avoids merging contracts whose absolute prices
    differ by a tick or two due to contango."""
    if not bars:
        return bars
    by_inst = {}                      # instrument -> total_volume
    grouped = {}                      # instrument -> [bars]
    for b in bars:
        inst = b.get("_instrument") or ""
        by_inst[inst] = by_inst.get(inst, 0) + (b.get("volume") or 0)
        grouped.setdefault(inst, []).append(b)
    if len(by_inst) <= 1:
        return bars
    winner = max(by_inst, key=by_inst.get)
    log_event("INFO", "databento",
              f"Parent-symbol {symbol_for_log} resolved to {winner!r} "
              f"({by_inst[winner]} contracts vs "
              f"{len(by_inst) - 1} other instrument(s))")
    return grouped[winner]


# ---------------------------------------------------------------------------
# Internal: aggregation (1m → 5m / 10m)
# ---------------------------------------------------------------------------

def _aggregate(bars_1m: list, target_min: int) -> list:
    """Combine 1m bars into target-min bars aligned to UTC minute
    boundaries. Open=first 1m, close=last 1m, high=max, low=min,
    volume=sum. Empty list on bad input."""
    if not bars_1m or target_min <= 1:
        return list(bars_1m)

    bucket_size = target_min * 60
    buckets = {}            # bucket_start_epoch -> bar_dict
    bucket_order = []       # preserves first-seen order

    for b in bars_1m:
        ts_iso = b.get("timestamp")
        dt = _parse_iso(ts_iso)
        if dt is None:
            continue
        epoch = int(dt.timestamp())
        bucket_epoch = (epoch // bucket_size) * bucket_size

        existing = buckets.get(bucket_epoch)
        if existing is None:
            buckets[bucket_epoch] = {
                "timestamp": datetime.fromtimestamp(
                    bucket_epoch, tz=timezone.utc
                ).isoformat(),
                "open":  b.get("open"),
                "high":  b.get("high"),
                "low":   b.get("low"),
                "close": b.get("close"),
                "volume": b.get("volume") or 0,
            }
            bucket_order.append(bucket_epoch)
            continue

        high = b.get("high")
        low = b.get("low")
        close = b.get("close")
        if high is not None and (existing["high"] is None
                                  or high > existing["high"]):
            existing["high"] = high
        if low is not None and (existing["low"] is None
                                 or low < existing["low"]):
            existing["low"] = low
        if close is not None:
            existing["close"] = close
        existing["volume"] = ((existing.get("volume") or 0)
                              + (b.get("volume") or 0))

    return [buckets[e] for e in bucket_order]


def _parse_iso(s):
    """Parse Databento's pretty_ts ISO strings. Pretty timestamps come
    with nanosecond precision and a `Z` suffix; Python's fromisoformat
    only handles up to microseconds and `+00:00`."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, _, tail = s.partition(".")
        digits = ""
        rest = ""
        for i, ch in enumerate(tail):
            if ch.isdigit():
                digits += ch
            else:
                rest = tail[i:]
                break
        if len(digits) > 6:
            digits = digits[:6]
        s = f"{head}.{digits}{rest}"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Internal: helpers
# ---------------------------------------------------------------------------

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
