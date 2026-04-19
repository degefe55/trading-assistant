"""
Technical analysis.
Computes indicators from price history.
No external libraries beyond stdlib + pandas-like math via pure Python.
(Kept lean to minimize GitHub Actions install time.)
"""


def sma(prices: list, period: int) -> float | None:
    """Simple moving average of last N prices."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def ema(prices: list, period: int) -> float | None:
    """Exponential moving average."""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(prices[:period]) / period
    for p in prices[period:]:
        ema_val = p * k + ema_val * (1 - k)
    return ema_val


def rsi(prices: list, period: int = 14) -> float | None:
    """Relative Strength Index. 0-100. >70 overbought, <30 oversold."""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def macd(prices: list, fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """MACD. Returns dict with macd_line, signal_line, histogram."""
    if len(prices) < slow + signal:
        return None
    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    if ema_fast is None or ema_slow is None:
        return None
    macd_line = ema_fast - ema_slow
    # For signal, compute EMA of MACD line over last `signal` periods
    macd_values = []
    k_fast = 2 / (fast + 1)
    k_slow = 2 / (slow + 1)
    ef = sum(prices[:fast]) / fast
    es = sum(prices[:slow]) / slow
    for i, p in enumerate(prices):
        if i >= fast:
            ef = p * k_fast + ef * (1 - k_fast)
        if i >= slow:
            es = p * k_slow + es * (1 - k_slow)
        if i >= slow:
            macd_values.append(ef - es)
    signal_line = ema(macd_values, signal) if len(macd_values) >= signal else None
    return {
        "macd": round(macd_line, 3),
        "signal": round(signal_line, 3) if signal_line else None,
        "histogram": round(macd_line - signal_line, 3) if signal_line else None,
    }


def bollinger_bands(prices: list, period: int = 20, std_mult: float = 2.0) -> dict | None:
    """Bollinger Bands. Returns upper, middle (SMA), lower."""
    if len(prices) < period:
        return None
    recent = prices[-period:]
    mid = sum(recent) / period
    variance = sum((p - mid) ** 2 for p in recent) / period
    std = variance ** 0.5
    return {
        "upper": round(mid + std_mult * std, 2),
        "middle": round(mid, 2),
        "lower": round(mid - std_mult * std, 2),
    }


def support_resistance(candles: list, lookback: int = 30) -> dict:
    """
    Identify recent support (higher-low, higher-low pattern)
    and resistance (lower-high, lower-high) levels.
    Simple approach: recent high/low zones within lookback.
    """
    if len(candles) < 5:
        return {"support": None, "resistance": None}
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    highs = sorted([c["high"] for c in recent], reverse=True)
    lows = sorted([c["low"] for c in recent])
    # Take top-3 highs and bottom-3 lows and average (filters noise)
    resistance = round(sum(highs[:3]) / 3, 2)
    support = round(sum(lows[:3]) / 3, 2)
    return {"support": support, "resistance": resistance}


def volume_analysis(candles: list) -> dict:
    """Compare today's volume vs recent average."""
    if len(candles) < 10:
        return {"today_volume": 0, "avg_volume": 0, "ratio": 1.0, "unusual": False}
    today = candles[-1]["volume"]
    avg = sum(c["volume"] for c in candles[-20:-1]) / max(len(candles[-20:-1]), 1)
    ratio = today / avg if avg > 0 else 1.0
    return {
        "today_volume": today,
        "avg_volume": int(avg),
        "ratio": round(ratio, 2),
        "unusual": ratio > 1.5 or ratio < 0.5,
    }


def full_technical_snapshot(candles: list) -> dict:
    """
    Compute the full technical picture.
    Returns everything Claude needs for analysis.
    """
    if not candles or len(candles) < 5:
        return {"error": "insufficient data"}
    closes = [c["close"] for c in candles]
    snapshot = {
        "price_now": closes[-1],
        "sma_20": round(sma(closes, 20), 2) if sma(closes, 20) else None,
        "sma_50": round(sma(closes, 50), 2) if sma(closes, 50) else None,
        "sma_200": round(sma(closes, 200), 2) if sma(closes, 200) else None,
        "rsi_14": rsi(closes, 14),
        "macd": macd(closes),
        "bollinger": bollinger_bands(closes),
        "levels": support_resistance(candles),
        "volume": volume_analysis(candles),
    }
    # Derive simple signals
    snapshot["signals"] = _derive_signals(snapshot, closes)
    return snapshot


def _derive_signals(snap: dict, closes: list) -> list:
    """Plain-English chart signals for Claude to use."""
    signals = []
    price = snap["price_now"]
    if snap["sma_20"] and price > snap["sma_20"]:
        signals.append("above 20-day MA (short-term bullish)")
    elif snap["sma_20"] and price < snap["sma_20"]:
        signals.append("below 20-day MA (short-term bearish)")
    if snap["sma_50"] and price > snap["sma_50"]:
        signals.append("above 50-day MA (medium-term bullish)")
    elif snap["sma_50"] and price < snap["sma_50"]:
        signals.append("below 50-day MA (medium-term bearish)")
    r = snap["rsi_14"]
    if r and r > 70:
        signals.append(f"RSI {r} (overbought)")
    elif r and r < 30:
        signals.append(f"RSI {r} (oversold)")
    elif r:
        signals.append(f"RSI {r} (neutral)")
    m = snap["macd"]
    if m and m.get("histogram") is not None:
        if m["histogram"] > 0:
            signals.append("MACD histogram positive (bullish momentum)")
        else:
            signals.append("MACD histogram negative (bearish momentum)")
    b = snap["bollinger"]
    if b:
        if price > b["upper"]:
            signals.append("above upper Bollinger (stretched)")
        elif price < b["lower"]:
            signals.append("below lower Bollinger (potentially oversold)")
    v = snap["volume"]
    if v.get("unusual"):
        signals.append(f"volume {v['ratio']}x normal (unusual activity)")
    lv = snap["levels"]
    if lv["resistance"] and abs(price - lv["resistance"]) / price < 0.01:
        signals.append(f"near resistance {lv['resistance']}")
    if lv["support"] and abs(price - lv["support"]) / price < 0.01:
        signals.append(f"near support {lv['support']}")
    return signals
