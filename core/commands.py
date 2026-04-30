"""
Command handler for Telegram bot interactions.
Parses /buy /sell /pnl /status /pause /resume /help commands.

/run dispatches to the in-process scheduler in webhook/app.py via
_spawn_brief, which runs the brief in a background thread and returns
immediately so the Telegram webhook doesn't time out.
"""
import re
import os
from datetime import datetime
from config import KSA_TZ, TELEGRAM_CHAT_ID
from core import telegram_client, trades, sheets
from core.logger import log_event


STATE_FILE = "/tmp/telegram_state.txt"
PAUSE_FILE = "/tmp/bot_paused.txt"

VALID_BRIEFS = ("premarket", "midsession", "preclose", "eod")


def process_commands() -> int:
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
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
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
        if cmd == "/run":
            return _cmd_run(args)
        if cmd == "/runlist":
            return _cmd_runlist()
        if cmd == "/cancel":
            return _cmd_cancel(args)
        if cmd == "/times":
            return _cmd_times()
        if cmd == "/settime":
            return _cmd_settime(args)
        if cmd == "/ask":
            return _cmd_ask(args)
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
/times — show brief schedule

<b>Manual briefs</b>
/runlist — show /run options
<code>/run premarket</code>
<code>/run midsession</code>
<code>/run preclose</code>
<code>/run eod</code>
<code>/cancel BRIEF</code> — stop a running brief
Example: <code>/cancel premarket</code>

<b>Follow-up questions</b>
<code>/ask RecID question</code>
Example: <code>/ask 20260430-1530-PRE-SPWO why SELL?</code>
Or: reply directly to any brief message and type your question.

<b>Schedule</b>
<code>/settime BRIEF HH:MM</code> — change brief time
Example: <code>/settime preclose 22:45</code>

<b>Control</b>
/pause — silence scheduled briefs
/resume — re-enable briefs
/help — this menu

<b>Trade examples</b>
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

    # Show currently-running briefs if any
    try:
        from webhook import app as webhook_app
        with webhook_app._running_lock:
            running = sorted(webhook_app._currently_running)
        if running:
            lines.append(f"Running now: {', '.join(running)}")
    except Exception:
        pass

    return "\n".join(lines)


def _cmd_pause() -> str:
    with open(PAUSE_FILE, "w") as f:
        f.write(datetime.now(KSA_TZ).isoformat())
    return "⏸️ <b>Briefs paused</b>\nScheduled briefs will not send. Use /resume to re-enable."


def _cmd_resume() -> str:
    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
    return "▶️ <b>Briefs resumed</b>\nScheduled briefs re-enabled."


# ---------------------------------------------------------------------------
# Manual brief triggers and schedule management
# ---------------------------------------------------------------------------

def _cmd_runlist() -> str:
    return ("<b>🚀 Manual brief triggers</b>\n"
            "─────────────\n"
            "<code>/run premarket</code> — full pre-market brief\n"
            "<code>/run midsession</code> — mid-session check\n"
            "<code>/run preclose</code> — pre-close verdict\n"
            "<code>/run eod</code> — end-of-day summary\n\n"
            "Brief runs in background; the brief Telegram message arrives "
            "in 30–90 seconds. Sending /run again while one is running is "
            "ignored (no duplicate runs / no token waste).\n\n"
            "To stop a running brief: <code>/cancel BRIEF</code>")


def _cmd_cancel(args: list) -> str:
    """Stop a running brief. Cooperative cancel: in-flight Sonnet call
    finishes (we can't kill it from Python), but no further tickers are
    analyzed. So damage is capped at one Claude call from the moment
    /cancel is sent."""
    if not args:
        return ("Usage: <code>/cancel BRIEF</code>\n"
                f"Briefs: {', '.join(VALID_BRIEFS)}\n"
                "Example: <code>/cancel premarket</code>\n\n"
                "Send /status to see what's running.")

    brief = args[0].lower().strip()
    if brief not in VALID_BRIEFS:
        return (f"❌ Unknown brief: <code>{brief}</code>\n"
                f"Valid: {', '.join(VALID_BRIEFS)}")

    try:
        from webhook import app as webhook_app
        from core import analyst as analyst_mod
    except Exception as e:
        return f"❌ Could not reach scheduler: {e}"

    with webhook_app._running_lock:
        is_running = brief in webhook_app._currently_running

    if not is_running:
        return (f"ℹ️ <b>{brief}</b> is not currently running.\n"
                f"Nothing to cancel.")

    flag_was_new = analyst_mod.request_cancel(brief)
    if flag_was_new:
        return (f"🛑 <b>Cancellation requested for {brief}</b>\n"
                f"In-flight Claude call (if any) will finish, then the "
                f"brief stops. No more tickers will be analyzed.\n\n"
                f"You'll get a partial Telegram message confirming.")
    else:
        return (f"⚠️ <b>{brief}</b> already has a pending cancellation.\n"
                f"Waiting for the in-flight Claude call to finish.")


def _cmd_run(args: list) -> str:
    if not args:
        return _cmd_runlist()

    brief = args[0].lower().strip()
    if brief not in VALID_BRIEFS:
        return (f"❌ Unknown brief: <code>{brief}</code>\n"
                f"Send /runlist to see options.")

    try:
        from webhook import app as webhook_app
    except Exception as e:
        return f"❌ Could not reach scheduler: {e}"

    spawned, reason = webhook_app._spawn_brief(brief, source="telegram")
    if spawned:
        return (f"🚀 Running <b>{brief}</b> brief in background…\n"
                f"<i>Result will arrive in 30–90 seconds.</i>")
    else:
        return (f"⚠️ <b>{brief}</b> not started — {reason}.\n"
                f"Send /status to see what's running.")


def _cmd_times() -> str:
    try:
        from webhook import app as webhook_app
        active = webhook_app._active_times or {}
    except Exception:
        active = {}

    if not active:
        from webhook.app import DEFAULT_TIMES, CONFIG_KEY_PREFIX
        cfg = sheets.read_config() or {}
        active = {}
        for brief, default in DEFAULT_TIMES.items():
            active[brief] = cfg.get(f"{CONFIG_KEY_PREFIX}{brief.upper()}", default)

    lines = ["<b>🕒 Brief schedule (KSA, Mon–Fri)</b>",
             "─────────────"]
    label = {
        "premarket":  "Pre-market   ",
        "midsession": "Mid-session  ",
        "preclose":   "Pre-close    ",
        "eod":        "End of day   ",
    }
    for brief in ("premarket", "midsession", "preclose", "eod"):
        lines.append(f"<code>{label[brief]} {active.get(brief, '?')}</code>")
    lines.append("")
    lines.append("Change with: <code>/settime BRIEF HH:MM</code>")
    return "\n".join(lines)


def _cmd_settime(args: list) -> str:
    if len(args) < 2:
        return ("Usage: <code>/settime BRIEF HH:MM</code>\n"
                "Briefs: premarket, midsession, preclose, eod\n"
                "Example: <code>/settime preclose 22:45</code>")

    brief = args[0].lower().strip()
    hhmm = args[1].strip()

    if brief not in VALID_BRIEFS:
        return (f"❌ Unknown brief: <code>{brief}</code>\n"
                f"Valid: {', '.join(VALID_BRIEFS)}")

    if not _validate_hhmm(hhmm):
        return (f"❌ Invalid time: <code>{hhmm}</code>\n"
                f"Use 24-hour HH:MM (zero-padded), e.g. 22:45 or 09:00")

    from webhook.app import CONFIG_KEY_PREFIX
    key = f"{CONFIG_KEY_PREFIX}{brief.upper()}"
    try:
        sheets.write_config(key, hhmm)
    except Exception as e:
        return f"❌ Could not save to Config tab: {e}"

    rebuild_ok = True
    rebuild_err = ""
    try:
        from webhook import app as webhook_app
        webhook_app.rebuild_schedule()
        active = webhook_app._active_times.get(brief, hhmm)
    except Exception as e:
        rebuild_ok = False
        rebuild_err = str(e)
        log_event("WARN", "commands", f"Reschedule failed (saved anyway): {e}")
        active = hhmm

    if rebuild_ok:
        return (f"✅ <b>Schedule updated</b>\n"
                f"<code>{brief}</code> → <code>{active}</code> (KSA, Mon–Fri)\n\n"
                f"Saved to Config tab.\n"
                f"Send /times to see all.")
    else:
        return (f"⚠️ <b>Saved but not applied</b>\n"
                f"<code>{brief}</code> → <code>{active}</code> "
                f"saved to Config tab, but the live scheduler rejected it: "
                f"<code>{rebuild_err}</code>\n\n"
                f"Restart the Railway service or fix the value to apply.")


def _cmd_ask(args: list) -> str:
    """/ask <RecID> <question>: ask a follow-up about an earlier recommendation.

    The RecID is the first arg (no spaces inside, hyphens only).
    Everything after that is the question.
    """
    if len(args) < 2:
        return ("Usage: <code>/ask RecID your question here</code>\n"
                "RecID format: <code>YYYYMMDD-HHMM-BRIEF-TICKER</code>\n"
                "Example: <code>/ask 20260430-1530-PRE-SPWO why SELL?</code>\n\n"
                "<i>Or just reply directly to any brief message in this chat.</i>")

    rec_id = args[0].strip()
    question = " ".join(args[1:]).strip()

    # Quick sanity check on the RecID shape (don't require regex perfection,
    # just catch obvious typos before paying for a Claude call)
    if "-" not in rec_id or len(rec_id) < 10:
        return (f"❌ That doesn't look like a RecID: <code>{rec_id}</code>\n"
                f"Expected: <code>YYYYMMDD-HHMM-BRIEF-TICKER</code>")

    # Lazy import — followup module pulls in claude_client which loads
    # the API client; we don't want that on every dispatch
    try:
        from core import followup
    except Exception as e:
        return f"❌ Could not load follow-up module: {e}"

    result = followup.answer_followup(rec_id, question)
    if not result.get("ok"):
        return f"❌ {result.get('error', 'Unknown error')}"

    return (f"💬 <b>Re:</b> {result['ticker']} ({result['action']})\n"
            f"─────────────\n"
            f"{result['answer']}\n\n"
            f"<i>💰 ${result['cost_usd']:.4f}</i>")


def _is_paused() -> bool:
    return os.path.exists(PAUSE_FILE)


def _validate_hhmm(s: str) -> bool:
    try:
        s = s.strip()
        if len(s) != 5 or s[2] != ":":
            return False
        h, m = int(s[0:2]), int(s[3:5])
        return 0 <= h < 24 and 0 <= m < 60
    except (ValueError, AttributeError):
        return False


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
