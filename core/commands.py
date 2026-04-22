"""
Command handler for Telegram bot interactions.
Parses /buy /sell /pnl /status /pause /resume /help commands.
Run periodically by GitHub Actions to poll for new messages.
"""
import re
import os
from datetime import datetime
from config import KSA_TZ, TELEGRAM_CHAT_ID
from core import telegram_client, trades, sheets
from core.logger import log_event


# State file - stores last processed update_id to avoid re-processing
STATE_FILE = "/tmp/telegram_state.txt"
PAUSE_FILE = "/tmp/bot_paused.txt"


def process_commands() -> int:
    """
    Poll Telegram for new messages, process commands.
    Returns number of commands processed.
    """
    last_id = _read_last_update_id()
    updates = telegram_client.get_updates(offset=last_id + 1)
    if not updates:
        return 0

    processed = 0
    max_id = last_id
    for update in updates:
        update_id = update.get("update_id", 0)
        max_id = max(max_id, update_id)

        msg = update.get("message", {})
        # Only accept commands from the owner's chat
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(TELEGRAM_CHAT_ID):
            log_event("WARN", "commands", f"Ignored message from chat {chat_id}")
            continue

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue

        reply = _dispatch(text)
        if reply:
            telegram_client.send_message(reply)
        processed += 1

    _save_last_update_id(max_id)
    return processed


def _dispatch(text: str) -> str:
    """Route command to handler."""
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]  # strip @botname suffix
    args = parts[1:]

    try:
        if cmd == "/start":
            return _cmd_start()
        if cmd == "/help":
            return _cmd_help()
        if cmd == "/buy":
            return _cmd_buy(args)
        if cmd == "/sell":
            return _cmd_sell(args)
        if cmd == "/pnl":
            return _cmd_pnl()
        if cmd == "/status":
            return _cmd_status()
        if cmd == "/pause":
            return _cmd_pause()
        if cmd == "/resume":
            return _cmd_resume()
        return f"Unknown command: {cmd}\nSend /help for available commands."
    except Exception as e:
        log_event("ERROR", "commands", f"Command {cmd} failed: {e}")
        return f"❌ Command failed: {e}"


def _cmd_start() -> str:
    return ("👋 <b>Trading bot ready</b>\n\n"
            "Send /help for commands.")


def _cmd_help() -> str:
    return ("""<b>📚 Commands</b>

<b>Trades</b>
<code>/buy TICKER SHARES PRICE</code>
<code>/sell TICKER SHARES PRICE</code>

<b>Status</b>
/pnl — current P&amp;L snapshot
/status — bot health check

<b>Control</b>
/pause — silence scheduled briefs
/resume — re-enable briefs
/help — this menu

<b>Examples</b>
<code>/buy SPWO 10 31.40</code>
<code>/sell SPWO 5 31.80</code>""")


def _cmd_buy(args: list) -> str:
    if len(args) < 3:
        return "Usage: <code>/buy TICKER SHARES PRICE</code>\nExample: <code>/buy SPWO 10 31.40</code>"
    try:
        ticker = args[0].upper()
        shares = float(args[1])
        price = float(args[2])
    except ValueError:
        return "❌ Invalid arguments. Use: /buy SPWO 10 31.40"

    if shares <= 0 or price <= 0:
        return "❌ Shares and price must be positive"

    result = trades.record_buy(ticker, shares, price, market="US",
                               reason="Manual /buy command")
    if not result.get("success"):
        return f"❌ {result.get('error', 'Unknown error')}"

    return (f"✅ <b>BOUGHT</b>\n"
            f"  {shares:g} × {ticker} @ <code>${price}</code> "
            f"(SAR {result['price_sar']:.2f})\n"
            f"  {result['summary']}")


def _cmd_sell(args: list) -> str:
    if len(args) < 3:
        return "Usage: <code>/sell TICKER SHARES PRICE</code>\nExample: <code>/sell SPWO 5 31.80</code>"
    try:
        ticker = args[0].upper()
        shares = float(args[1])
        price = float(args[2])
    except ValueError:
        return "❌ Invalid arguments. Use: /sell SPWO 5 31.80"

    result = trades.record_sell(ticker, shares, price, market="US",
                                reason="Manual /sell command")
    if not result.get("success"):
        return f"❌ {result.get('error', 'Unknown error')}"

    pnl = result["pnl_usd"]
    emoji = "🟢" if pnl >= 0 else "🔴"
    return (f"✅ <b>SOLD</b>\n"
            f"  {shares:g} × {ticker} @ <code>${price}</code>\n"
            f"  {emoji} Realized P&amp;L: <code>${pnl:+.2f}</code> "
            f"(SAR {result['pnl_sar']:+.2f})\n"
            f"  {result['summary']}")


def _cmd_pnl() -> str:
    pnl = trades.calculate_pnl()
    total_today = pnl["unrealized_usd"] + pnl["realized_today_usd"]
    emoji_t = "🟢" if total_today >= 0 else "🔴"
    emoji_cr = "🟢" if pnl["realized_total_usd"] >= 0 else "🔴"

    lines = [f"<b>💰 P&amp;L SNAPSHOT</b>"]
    lines.append("─────────────")
    lines.append(f"{emoji_t} <b>Today:</b> <code>${total_today:+.2f}</code> "
                 f"(SAR {(pnl['unrealized_sar'] + pnl['realized_today_sar']):+.2f})")
    lines.append(f"  • Realized: <code>${pnl['realized_today_usd']:+.2f}</code>")
    lines.append(f"  • Unrealized: <code>${pnl['unrealized_usd']:+.2f}</code>")
    lines.append("")
    lines.append(f"{emoji_cr} <b>Cumulative realized:</b> "
                 f"<code>${pnl['realized_total_usd']:+.2f}</code>")
    lines.append("")

    if pnl["positions"]:
        lines.append("<b>Positions</b>")
        lines.append("─────────────")
        for p in pnl["positions"]:
            e = "🟢" if p["pnl_usd"] >= 0 else "🔴"
            lines.append(f"{e} <b>{p['ticker']}</b>  "
                         f"<code>{p['shares']:g}sh · ${p['avg_cost']:.2f}→${p['current_price']:.2f}</code>  "
                         f"<b>{p['pnl_usd']:+.2f}</b> ({p['pnl_pct']:+.1f}%)")
    else:
        lines.append("<i>No open positions</i>")

    return "\n".join(lines)


def _cmd_status() -> str:
    paused = _is_paused()
    lines = [f"<b>🩺 BOT STATUS</b>"]
    lines.append("─────────────")
    lines.append(f"Time (KSA): {datetime.now(KSA_TZ).strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Scheduled briefs: {'⏸️ PAUSED' if paused else '✅ active'}")
    lines.append(f"Mock mode: {'ON' if os.getenv('MOCK_MODE', 'true') == 'true' else 'OFF'}")
    positions = sheets.read_positions()
    lines.append(f"Open positions: {len(positions)}")
    return "\n".join(lines)


def _cmd_pause() -> str:
    with open(PAUSE_FILE, "w") as f:
        f.write(datetime.now(KSA_TZ).isoformat())
    return "⏸️ <b>Briefs paused</b>\nScheduled briefs will not send. Use /resume to re-enable."


def _cmd_resume() -> str:
    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
    return "▶️ <b>Briefs resumed</b>\nScheduled briefs re-enabled."


def _is_paused() -> bool:
    return os.path.exists(PAUSE_FILE)


def _read_last_update_id() -> int:
    try:
        with open(STATE_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _save_last_update_id(update_id: int):
    try:
        with open(STATE_FILE, "w") as f:
            f.write(str(update_id))
    except Exception as e:
        log_event("WARN", "commands", f"Could not save state: {e}")
