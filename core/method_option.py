"""
method_option — pure rule engine for the options-trading setup.

Five primitive detectors plus one orchestrator (`evaluate_setup`). No
I/O, no Telegram, no scheduler, no state across calls. Given OHLCV
bars (the polygon_client output shape), return signal status.

Bar shape (per core/polygon_client.py._normalize_bars):
    {"timestamp": str, "open": float, "high": float, "low": float,
     "close": float, "volume": int}

Insufficient data degrades to False / None; only genuinely malformed
bars (missing/non-numeric close in MACD) emit a WARN log. Counts of
"too few bars yet" are not errors and stay silent — the orchestrator
is invoked tick-by-tick and would otherwise spam the log.
"""
from core.logger import log_event


# ---------------------------------------------------------------------------
# Public: fractals
# ---------------------------------------------------------------------------

def detect_fractal_high(bars: list) -> dict | None:
    """Most recent fractal-high bar (Bill Williams 5-bar fractal):
    bars[i].high strictly greater than the highs of i-2, i-1, i+1, i+2.
    Needs 2 confirmed bars after the candidate, so the candidate range
    is [2, len-3]. Scans from the right and returns the first match.
    """
    return _scan_fractal(bars, kind="high")


def detect_fractal_low(bars: list) -> dict | None:
    """Most recent fractal-low bar — mirror of detect_fractal_high."""
    return _scan_fractal(bars, kind="low")


# ---------------------------------------------------------------------------
# Public: 5-min structural trend
# ---------------------------------------------------------------------------

def is_trend_bullish(bars_5min: list) -> bool:
    """Bullish-structure check on 5-min bars:
      1. Find the lowest-low bar within the last 30 bars.
      2. Find the most recent fractal high *before* that low.
      3. Last bar's close must be above that fractal high's high.
    Any missing piece returns False (not an error)."""
    if not bars_5min:
        return False
    low_idx = _index_of_extreme(bars_5min, last_n=30, key="low", mode="min")
    if low_idx is None:
        return False
    fractal = _find_fractal_before(bars_5min, before_idx=low_idx, kind="high")
    if fractal is None:
        return False
    last_close = _safe_num(bars_5min[-1].get("close"))
    fh_high = _safe_num(fractal.get("high"))
    if last_close is None or fh_high is None:
        return False
    return last_close > fh_high


def is_trend_bearish(bars_5min: list) -> bool:
    """Mirror of is_trend_bullish: highest-high in last 30, then the
    most recent fractal low before it, then last close below it."""
    if not bars_5min:
        return False
    high_idx = _index_of_extreme(bars_5min, last_n=30, key="high", mode="max")
    if high_idx is None:
        return False
    fractal = _find_fractal_before(bars_5min, before_idx=high_idx, kind="low")
    if fractal is None:
        return False
    last_close = _safe_num(bars_5min[-1].get("close"))
    fl_low = _safe_num(fractal.get("low"))
    if last_close is None or fl_low is None:
        return False
    return last_close < fl_low


# ---------------------------------------------------------------------------
# Public: MACD
# ---------------------------------------------------------------------------

def macd(bars: list, fast: int = 12, slow: int = 26,
         signal: int = 9) -> dict:
    """Standard MACD on close prices. Returns three lists, each the
    same length as `bars`. Early indices are None until the relevant
    EMA seed has been established (SMA-seeded EMAs).

    Malformed bar data (close missing or non-numeric) logs a WARN and
    returns all-None lists — the orchestrator treats that as
    "MACD not aligned" and the setup degrades to NO_SETUP.
    """
    n = len(bars)
    none_result = {
        "macd_line": [None] * n,
        "signal_line": [None] * n,
        "histogram": [None] * n,
    }
    if n == 0:
        return none_result

    closes = _extract_closes(bars)
    if closes is None:
        log_event("WARN", "method_option",
                  "macd: malformed bar data — missing or non-numeric close")
        return none_result

    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    macd_line = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]

    # Signal line: 9-EMA of macd_line. macd_line has leading Nones, so
    # compute the EMA on the non-None tail and re-align.
    signal_line: list = [None] * n
    valid_start = next(
        (i for i, v in enumerate(macd_line) if v is not None), None
    )
    if valid_start is not None:
        tail = macd_line[valid_start:]
        if len(tail) >= signal:
            tail_ema = _ema(tail, signal)
            for i, v in enumerate(tail_ema):
                signal_line[valid_start + i] = v

    histogram = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return {"macd_line": macd_line, "signal_line": signal_line,
            "histogram": histogram}


def is_macd_aligned(bars: list, direction: str) -> bool:
    """Check the friend's MACD-alignment condition on the latest bar.

    bullish: histogram > 0 AND macd_line < histogram  (green and
             histogram leading the macd line up)
    bearish: histogram < 0 AND macd_line > histogram  (red and
             histogram leading the macd line down)

    Returns False on insufficient data or any unexpected direction.
    """
    if direction not in ("bullish", "bearish"):
        return False
    if not bars:
        return False
    m = macd(bars)
    macd_last = m["macd_line"][-1] if m["macd_line"] else None
    hist_last = m["histogram"][-1] if m["histogram"] else None
    if macd_last is None or hist_last is None:
        return False
    if direction == "bullish":
        return hist_last > 0 and macd_last < hist_last
    return hist_last < 0 and macd_last > hist_last


# ---------------------------------------------------------------------------
# Public: orchestrator
# ---------------------------------------------------------------------------

def evaluate_setup(bars_1m: list, bars_5m: list,
                   bars_10m: list) -> dict:
    """Combine the primitives into call/put state for a single tick.

    NO_SETUP    — at least one prerequisite (5m trend, 10m MACD, 1m
                  MACD) fails on that side.
    PRE_SIGNAL  — all three prerequisites pass; awaiting a 1m close
                  through the most recent 1m fractal.
    ENTRY_SIGNAL — PRE_SIGNAL + last 1m close has crossed the relevant
                  1m fractal (above fractal_high for calls, below
                  fractal_low for puts).
    """
    trend_5m_bull = is_trend_bullish(bars_5m)
    trend_5m_bear = is_trend_bearish(bars_5m)
    macd_10m_bull = is_macd_aligned(bars_10m, "bullish")
    macd_10m_bear = is_macd_aligned(bars_10m, "bearish")
    macd_1m_bull = is_macd_aligned(bars_1m, "bullish")
    macd_1m_bear = is_macd_aligned(bars_1m, "bearish")
    fh_1m = detect_fractal_high(bars_1m)
    fl_1m = detect_fractal_low(bars_1m)

    last_1m_close = None
    if bars_1m:
        last_1m_close = _safe_num(bars_1m[-1].get("close"))

    call_state = "NO_SETUP"
    if trend_5m_bull and macd_10m_bull and macd_1m_bull:
        call_state = "PRE_SIGNAL"
        if fh_1m is not None and last_1m_close is not None:
            fh_high = _safe_num(fh_1m.get("high"))
            if fh_high is not None and last_1m_close > fh_high:
                call_state = "ENTRY_SIGNAL"

    put_state = "NO_SETUP"
    if trend_5m_bear and macd_10m_bear and macd_1m_bear:
        put_state = "PRE_SIGNAL"
        if fl_1m is not None and last_1m_close is not None:
            fl_low = _safe_num(fl_1m.get("low"))
            if fl_low is not None and last_1m_close < fl_low:
                put_state = "ENTRY_SIGNAL"

    return {
        "call_state": call_state,
        "put_state": put_state,
        "details": {
            "trend_5m_bullish": trend_5m_bull,
            "trend_5m_bearish": trend_5m_bear,
            "macd_10m_bullish_aligned": macd_10m_bull,
            "macd_10m_bearish_aligned": macd_10m_bear,
            "macd_1m_bullish_aligned": macd_1m_bull,
            "macd_1m_bearish_aligned": macd_1m_bear,
            "fractal_high_1m": fh_1m,
            "fractal_low_1m": fl_1m,
        },
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_num(v):
    """Coerce to float if numeric, else None. Bool is rejected because
    bool is an int subclass and would silently coerce to 0.0/1.0."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _extract_closes(bars: list):
    """Pull a list of float closes from bar dicts. Returns None if any
    bar is malformed (not a dict, missing close, non-numeric close)."""
    closes = []
    for b in bars:
        if not isinstance(b, dict):
            return None
        c = _safe_num(b.get("close"))
        if c is None:
            return None
        closes.append(c)
    return closes


def _ema(values: list, period: int) -> list:
    """SMA-seeded EMA. Output is the same length as `values`; the first
    `period - 1` entries are None, then EMA from index `period - 1`.
    """
    n = len(values)
    out: list = [None] * n
    if period <= 0 or n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    for i in range(period, n):
        prev = out[i - 1]
        out[i] = values[i] * k + prev * (1 - k)
    return out


def _index_of_extreme(bars: list, last_n: int, key: str,
                      mode: str):
    """Index (in the *full* bars list) of the bar with the min/max
    `key` value within the last `last_n` bars. None if nothing
    numeric is found."""
    if not bars or last_n <= 0:
        return None
    chunk = bars[-last_n:] if len(bars) >= last_n else bars
    base = len(bars) - len(chunk)
    best_idx = None
    best_val = None
    for j, b in enumerate(chunk):
        if not isinstance(b, dict):
            continue
        v = _safe_num(b.get(key))
        if v is None:
            continue
        if best_val is None:
            best_val = v
            best_idx = j
            continue
        if mode == "min" and v < best_val:
            best_val = v
            best_idx = j
        elif mode == "max" and v > best_val:
            best_val = v
            best_idx = j
    return None if best_idx is None else base + best_idx


def _is_fractal_at(bars: list, i: int, kind: str) -> bool:
    """True if `bars[i]` is a 5-bar fractal of the given kind. Caller
    must ensure 2 <= i <= len(bars) - 3."""
    key = "high" if kind == "high" else "low"
    center = _safe_num(bars[i].get(key))
    if center is None:
        return False
    neighbors = [
        _safe_num(bars[i - 2].get(key)),
        _safe_num(bars[i - 1].get(key)),
        _safe_num(bars[i + 1].get(key)),
        _safe_num(bars[i + 2].get(key)),
    ]
    if any(x is None for x in neighbors):
        return False
    if kind == "high":
        return all(center > x for x in neighbors)
    return all(center < x for x in neighbors)


def _scan_fractal(bars: list, kind: str) -> dict | None:
    """Scan right-to-left through the valid candidate range
    [2, len-3] and return the first (i.e. most recent) fractal bar."""
    if not bars or len(bars) < 5:
        return None
    for i in range(len(bars) - 3, 1, -1):
        if not isinstance(bars[i], dict):
            continue
        if _is_fractal_at(bars, i, kind):
            return bars[i]
    return None


def _find_fractal_before(bars: list, before_idx: int,
                        kind: str) -> dict | None:
    """Most recent fractal of `kind` whose center index is strictly
    less than `before_idx`. Confirmation bars (i+1, i+2) are still
    required and must exist within the bars list."""
    if not bars or len(bars) < 5:
        return None
    upper = min(len(bars) - 3, before_idx - 1)
    for i in range(upper, 1, -1):
        if not isinstance(bars[i], dict):
            continue
        if _is_fractal_at(bars, i, kind):
            return bars[i]
    return None


# ---------------------------------------------------------------------------
# Manual-test entry point (no automatic execution)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("method_option module loaded. To test: "
          "import core.method_option; "
          "core.method_option.evaluate_setup(bars_1m, bars_5m, bars_10m)")
