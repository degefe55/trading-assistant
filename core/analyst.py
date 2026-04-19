"""
Analyst pipeline orchestrator.
Given a ticker + context, pulls all data, calls Claude, returns formatted result.
"""
import json
from datetime import datetime
from config import USD_TO_SAR, DEFAULT_RULES
from core import data_router, technical, halal, claude_client, sheets
from core.logger import log_event


def analyze_ticker(ticker: str, market: str = "US",
                   context_type: str = "routine",
                   position: dict = None) -> dict:
    """
    Full analysis pipeline for one ticker.
    Returns dict with Claude's recommendation + metadata.

    context_type: 'routine' | 'premarket' | 'midsession' | 'preclose' | 'alert'
    position: dict from Positions tab if held, None otherwise
    """
    log_event("INFO", "analyst", f"Starting analysis: {ticker} ({context_type})")

    # 1. Gather data
    price = data_router.get_price(ticker)
    history = data_router.get_price_history(ticker, 60)
    news = data_router.get_news([ticker], hours_back=36)
    macro = data_router.get_macro()
    tech = technical.full_technical_snapshot(history)
    earnings = data_router.get_earnings(ticker)
    dividend = data_router.get_dividend(ticker)
    halal_info = halal.check_halal_listed(ticker, market)

    # 2. Build prompt
    system_prompt = claude_client.load_prompt("analyst")
    system_prompt += "\n\n" + halal.halal_prompt_context(ticker, market)

    user_prompt = _build_user_prompt(
        ticker, market, context_type, price, tech, news, macro,
        earnings, dividend, position, halal_info
    )

    # 3. Call Claude
    response, meta = claude_client.call_analyst(system_prompt, user_prompt)

    if not response:
        return {
            "ticker": ticker,
            "error": "Claude call failed",
            "meta": meta,
        }

    # 4. Package result
    return {
        "ticker": ticker,
        "market": market,
        "timestamp": datetime.now().isoformat(),
        "context_type": context_type,
        "price": price.get("price"),
        "halal_listed": halal_info["listed"],
        "recommendation": response,
        "data_snapshot": {
            "price": price,
            "technical_signals": tech.get("signals", []),
            "news_count": len(news),
            "earnings_date": earnings,
            "dividend": dividend,
        },
        "meta": meta,
    }


def _build_user_prompt(ticker, market, context_type, price, tech, news,
                       macro, earnings, dividend, position, halal_info):
    """Compose the user-side prompt with all data."""
    sar_price = price.get("price", 0) * USD_TO_SAR

    # Format news
    news_lines = []
    for n in news[:5]:
        sentiment = n.get("sentiment", 0)
        arrow = "📈" if sentiment > 0.2 else "📉" if sentiment < -0.2 else "➖"
        news_lines.append(f"  {arrow} [{n.get('source', '?')}] {n.get('headline', '')}")
    news_block = "\n".join(news_lines) if news_lines else "  (no relevant news)"

    # Format macro
    macro_lines = []
    for code, m in macro.items():
        macro_lines.append(f"  {m.get('label', code)}: {m.get('value')}{m.get('unit', '')}")
    macro_block = "\n".join(macro_lines)

    # Format position
    if position and position.get("Ticker") == ticker:
        pos_block = (f"HELD POSITION:\n"
                     f"  Shares: {position.get('Shares')}\n"
                     f"  Avg Cost: ${position.get('AvgCost_USD')} ({float(position.get('AvgCost_USD', 0)) * USD_TO_SAR:.2f} SAR)\n"
                     f"  Stop Loss: ${position.get('StopLoss')}\n"
                     f"  Target: ${position.get('Target')}\n"
                     f"  P&L: ${round((price['price'] - float(position.get('AvgCost_USD', 0))) * float(position.get('Shares', 0)), 2)}")
    else:
        pos_block = "POSITION: None (evaluating entry)"

    # Technical signals
    signals_block = "\n".join(f"  • {s}" for s in tech.get("signals", []))
    if not signals_block:
        signals_block = "  (insufficient history)"

    earnings_block = f"Next earnings: {earnings}" if earnings else "No earnings scheduled"

    dividend_block = ""
    if dividend:
        dividend_block = (f"\nDIVIDEND:\n"
                          f"  Ex-date: {dividend.get('ex_date')}\n"
                          f"  Amount: ${dividend.get('amount')} ({dividend.get('yield_pct')}% yield)")

    prompt = f"""CONTEXT: {context_type} briefing for {ticker} ({market})
Time: {datetime.now().strftime('%Y-%m-%d %H:%M KSA')}

PRICE:
  Current: ${price.get('price')} ({sar_price:.2f} SAR)
  Today: {price.get('change_pct')}% | Open: ${price.get('open')} | High: ${price.get('high')} | Low: ${price.get('low')}
  52-week: ${price.get('52w_low')} - ${price.get('52w_high')}
  Volume: {price.get('volume'):,}

TECHNICAL SNAPSHOT:
  20-day SMA: {tech.get('sma_20')}
  50-day SMA: {tech.get('sma_50')}
  RSI(14): {tech.get('rsi_14')}
  MACD histogram: {tech.get('macd', {}).get('histogram') if tech.get('macd') else 'n/a'}
  Bollinger: {tech.get('bollinger', {})}
  Support: {tech.get('levels', {}).get('support')}
  Resistance: {tech.get('levels', {}).get('resistance')}
SIGNALS:
{signals_block}

NEWS (last 36h):
{news_block}

MACRO:
{macro_block}

{earnings_block}{dividend_block}

HALAL:
  Listed on approved list: {halal_info['listed']}

{pos_block}

RULES TO APPLY:
  Stop loss = average cost (never lose money on a trade)
  Max position size: {DEFAULT_RULES['max_position_pct']}% of portfolio
  Warn if earnings within {DEFAULT_RULES['earnings_buffer_days']} days

Produce the recommendation in the required format."""
    return prompt
