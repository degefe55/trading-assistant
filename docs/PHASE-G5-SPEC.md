# Phase G.5 — Contract Picker Spec

Locked May 9, 2026. To build when Webull OpenAPI access is approved (1-3 business days from May 9).

## Context

Pine indicator (G.5.9+) detects index-side fractal break and fires pre_signal. The bot then needs to:
1. Fetch the SPX 0DTE option chain from Webull
2. Pick the right contract per the method
3. Verify MACD on that contract
4. Forward to Telegram with the right contract attached
5. Track that contract for TP1/TP2/TP3/STOP

## The actual method (per friend, May 9 conversations)

### Contract selection

When pre_signal direction = CALL or PUT:
1. Filter same-day expiry chain by direction (calls or puts)
2. Filter by ASK price between $3.00 and $3.90
3. Pick closest-to-ATM strike that fits the price filter
4. Tie-break: lowest ASK if multiple are equidistant from ATM

### Contract verification (after pick)

Once contract is selected:
1. Fetch its 10-min and 1-min OHLCV bars from Webull
2. Compute standard MACD (12/26/9) on each timeframe
3. Both MACDs must align with the index direction:
   - CALL setup: histogram green AND macd line below it (bullish on contract price)
   - PUT setup: histogram red AND macd line above it (bullish on contract price — puts gain when index drops)
4. If aligned → fire enriched pre_signal to Telegram with contract details
5. If not aligned → log "skipped: contract MACD not aligned" but don't alert

### Entry timing (unchanged from G.5.0)

After pre_signal arrives, wait for index 5-min candle to close past the fractal level.
On confirmation → fire ENTRY alert with the same contract.

## Architecture

### New files

- core/webull_client.py — SDK wrapper: chain fetch, bars fetch, MACD compute
- core/contract_picker.py — pick logic (filter + ATM tie-break)
- core/contract_tracker.py — subscribe to contract MQTT, watch for TP/STOP hits

### Existing file changes

core/method_state.py handle_webhook_event() on pre_signal:
- Call contract_picker.pick_contract(direction, chain)
- Call webull_client.compute_macd(contract.ticker, "10m") + ("1m")
- If aligned, fire to Telegram + start contract_tracker
- Else, log silently

## Contract picker logic

```python
PRICE_MIN = 3.00
PRICE_MAX = 3.90

def pick_contract(direction: str, chain: list, spot: float):
    candidates = [
        c for c in chain
        if c.type == direction.upper()
        and PRICE_MIN <= c.ask <= PRICE_MAX
    ]
    if not candidates:
        return None
    # Closest to ATM, tie-break lowest ask
    candidates.sort(key=lambda c: (abs(c.strike - spot), c.ask))
    return candidates[0]
```

## Open questions parked for later

1. $4 peak filter — friend mentioned skipping contracts whose recent peak > $4. Need clarification on "today's peak" vs "recent N bars". Park.

2. Multi-contract MACD verification — checking just one is fine per friend. If real-world divergence is observed, revisit.

3. Quantity / scale-out — current plan: 3 contracts, sell 1 each at TP1/TP2/TP3. Verify before shipping.

4. What if pre_signal fires but no contract in $3.00-$3.90 range? Skip alert? Fire without contract? Friend's input needed.

5. What if MACD aligns but later un-aligns before entry? Cancel pre_signal? Keep it? Friend's input.

## Effort estimate

- webull_client.py: 200-300 LOC
- contract_picker.py: 50-80 LOC
- contract_tracker.py: 200-300 LOC
- method_state.py changes: 50 LOC
- Tests: 50-100 LOC

Total: ~600-800 LOC, ~6-10 hours of focused work.

## Pre-build checklist (before writing G.5 code)

When Webull approval lands:
- Generate App Key + App Secret on Webull dashboard
- Add WEBULL_APP_KEY, WEBULL_APP_SECRET, WEBULL_REGION to Railway env
- Subscribe to OPRA OpenAPI tier (~$3-15/mo or free with monthly trade)
- Test SDK can fetch a basic SPX 0DTE chain
- Test SDK can fetch 1-min bars for one strike
- Test MQTT connection to a real-time stream
- Only THEN start building G.5 modules
