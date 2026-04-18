"""
Analyst pipeline orchestrator.
Given a ticker + context, pulls all data, calls Claude, returns structured result.
Expects v2 prompt which returns JSON.
"""
import json
import re
from datetime import datetime
from config import USD_TO_SAR, DEFAULT_RULES
from core import data_router, technical, halal, claude_client
from core.logger import log_event


def analyze_ticker(ticker: str, market: str = "US",
                   context_type: str = "routine",
                   position: dict = None) -> dict:
    """Full analysis pipeline. Returns structured dict."""
    log_event("INFO", "analyst", f"Analyzing {ticker} ({context_type})")

    price = data_router.get_price(ticker)
    history = data_router.get_price_history(ticker, 60)
    news = data_router.get_news([ticker], hours_back=36)
    macro = data_router.get_macro()
    tech = technical.full_technical_snapshot(history)
    earnings = data_router.get_earnings(ticker)
    dividend = data_router.get_dividend(ticker)
    halal_info = halal.check_halal_listed(ticker, market)

    system_prompt = claude_client.load_prompt("analyst")
    user_prompt = _build_user_prompt(
        ticker, market, context_type, price, tech, news, macro,
        earnings, dividend, position, halal_info
    )

    response, meta = claude_client.call_analyst(system_prompt, user_prompt)

    if not response:
        return _error_result(ticker, market, price, "Claude call failed", meta)

    parsed = _parse_json_response(response)
    if parsed is None:
        log_event("WARN", "analyst",
                  f"Could not parse JSON for {ticker}, raw: {response[:200]}")
        return _error_result(ticker, market, price, "JSON parse failed", meta,
                             raw_response=response)

    return {
        "ticker": ticker,
        "market": market,
        "timestamp": datetime.now().isoformat(),
        "context_type": context_type,
        "price": price.get("price"),
        "change_pct": price.get("change_pct"),
        "position": position,
        "halal_listed": halal_info["listed"],
        "analysis": parsed,
        "data_snapshot": {
            "price": price,
            "technical_signals": tech.get("signals", []),
            "news_count": len(news),
            "top_news": news[:3],
            "earnings_date": earnings,
            "dividend": dividend,
        },
        "meta": meta,
    }


def _parse_json_response(response: str) -> dict | None:
    """Parse Claude's JSON output, tolerating wrapper issues."""
    text = response.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as e:
        log_event("WARN", "analyst", f"JSON decode error: {e}")
        return None


def _error_result(ticker, market, price, error_msg, meta, raw_response=None):
    """Minimal valid result on error so downstream doesn't break."""
    return {
        "ticker": ticker,
        "market": market,
        "timestamp": datetime.now().isoformat(),
        "price": price.get("price", 0) if price else 0,
        "error": error_msg,
        "analysis": {
            "action": "WAIT",
            "action_urgent": False,
            "one_line_plan": "⚠️ Analysis error - see logs",
            "confidence": "low",
            "risk_score": 3,
            "halal_ai_signal": "🟡",
            "deep_dive": {
                "reasoning": f"Bot error: {error_msg}. {raw_response[:300] if raw_response else ''}",
                "warnings": ["Analysis failed - check logs"],
            },
        },
        "meta": meta,
    }


def _build_user_prompt(ticker, market, context_type, price, tech, news,
                       macro, earnings, dividend, position, halal_info):
    """Compose the user-side prompt."""
    sar_price = price.get("price", 0) * USD_TO_SAR

    news_lines = []
    for n in news[:5]:
        sentiment = n.get("sentiment", 0)
        arrow = "📈" if sentiment > 0.2 else "📉" if sentiment < -0.2 else "➖"
        news_lines.append(f"  {arrow} [{n.get('source', '?')}] {n.get('headline', '')}")
    news_block = "\n".join(news_lines) if news_lines else "  (no relevant news)"

    macro_lines = [f"  {m.get('label', c)}: {m.get('value')}{m.get('unit', '')}"
                   for c, m in macro.items()]
    macro_block = "\n".join(macro_lines)

    if position and position.get("Ticker") == ticker:
        shares = float(position.get("Shares", 0))
        avg = float(position.get("AvgCost_USD", 0))
        pnl = (price["price"] - avg) * shares
        pos_block = (f"HELD POSITION:\n"
                     f"  Shares: {shares}\n"
                     f"  Avg Cost: ${avg}\n"
                     f"  Stop Loss: ${position.get('StopLoss')}\n"
                     f"  Target: ${position.get('Target')}\n"
                     f"  Current P&L: ${pnl:+.2f}")
    else:
        pos_block = "POSITION: None (evaluating entry)"

    signals_block = "\n".join(f"  • {s}" for s in tech.get("signals", [])) or "  (insufficient history)"
    earnings_block = f"Next earnings: {earnings}" if earnings else "No earnings scheduled"
    dividend_block = ""
    if dividend:
        dividend_block = (f"\nDIVIDEND: ex-date {dividend.get('ex_date')}, "
                          f"${dividend.get('amount')} ({dividend.get('yield_pct')}% yield)")
    halal_block = (f"HALAL: On approved list = {halal_info['listed']}. "
                   f"Assess 🟢/🟡/🔴 based on business model; informational only.")

    return f"""CONTEXT: {context_type} briefing for {ticker} ({market})
Time: {datetime.now().strftime('%Y-%m-%d %H:%M KSA')}

PRICE: ${price.get('price')} (SAR {sar_price:.2f}) | Today {price.get('change_pct')}%
Open ${price.get('open')} | High ${price.get('high')} | Low ${price.get('low')}
52-week: ${price.get('52w_low')} - ${price.get('52w_high')}
Volume: {price.get('volume'):,}

TECHNICAL:
  SMA20: {tech.get('sma_20')} | SMA50: {tech.get('sma_50')} | RSI14: {tech.get('rsi_14')}
  MACD hist: {tech.get('macd', {}).get('histogram') if tech.get('macd') else 'n/a'}
  Bollinger: {tech.get('bollinger')}
  Support: {tech.get('levels', {}).get('support')} | Resistance: {tech.get('levels', {}).get('resistance')}
SIGNALS:
{signals_block}

NEWS (last 36h):
{news_block}

MACRO:
{macro_block}

{earnings_block}{dividend_block}

{halal_block}

{pos_block}

RULES:
  Stop loss = average cost
  Max position size: {DEFAULT_RULES['max_position_pct']}% of portfolio
  Warn if earnings within {DEFAULT_RULES['earnings_buffer_days']} days

Return the JSON object per schema. JSON ONLY."""
