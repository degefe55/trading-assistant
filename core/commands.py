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
from config import KSA_TZ, TELEGRAM_CHAT_ID, ACTIVE_MARKETS
from core import telegram_client, trades, sheets
from core.logger import log_event


STATE_FILE = "/tmp/telegram_state.txt"
PAUSE_FILE = "/tmp/bot_paused.txt"

# Internal (Python) brief names use underscores. User-facing CLI uses
# hyphens for SA briefs — converted at the dispatcher boundary via
# _normalize_brief_arg(). The watcher entry covers both US and SA
# watcher cancellation (single cancel flag per market handled in the
# branching below).
VALID_BRIEFS = (
    "premarket", "midsession", "preclose", "eod",
    "premarket_sa", "midsession_sa", "preclose_sa", "eod_sa",
    "watcher", "watcher_sa",
    # Phase G.2 — option-method runner (single tick at a time, no
    # market suffix; runs only while the US extended-hours window
    # 11:00–23:55 KSA is open).
    "method",
)


def _normalize_brief_arg(s: str) -> str:
    """Convert user-facing form (hyphens) to internal form (underscores).
    e.g. 'premarket-sa' -> 'premarket_sa'. Lowercased + stripped."""
    return (s or "").lower().strip().replace("-", "_")


def _detect_market(ticker: str) -> str | None:
    """Detect market from ticker shape.
      - all-digit (1-4 chars)                    → 'SA'  (Tadawul codes)
      - alphanumeric + ./- (1-6, has a letter)   → 'US'  (NYSE/Nasdaq;
                                                    incl. mixed like
                                                    US500, BRK.B)
      - anything else                            → None  (ambiguous)
    """
    if not ticker:
        return None
    t = ticker.strip().upper()
    if not t:
        return None
    if t.isdigit():
        if 1 <= len(t) <= 4:
            return "SA"
        return None
    if all(c.isalnum() or c in (".", "-") for c in t):
        if 1 <= len(t) <= 6:
            return "US"
        return None
    return None


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
        if cmd == "/cancel":
            return _cmd_cancel(args)
        if cmd == "/times":
            return _cmd_times()
        if cmd == "/settime":
            return _cmd_settime(args)
        if cmd == "/ask":
            return _cmd_ask(args)
        if cmd == "/list":
            return _cmd_list()
        if cmd == "/watch":
            return _cmd_watch(args)
        if cmd == "/unwatch":
            return _cmd_unwatch(args)
        if cmd == "/focus":
            return _cmd_focus(args)
        if cmd == "/unfocus":
            return _cmd_unfocus(args)
        if cmd == "/watcher":
            return _cmd_watcher(args)
        if cmd == "/markets":
            return _cmd_markets()
        if cmd == "/method":
            return _cmd_method(args)
        if cmd == "/trim_logs":
            return _cmd_trim_logs()
        if cmd == "/diagnose":
            return _cmd_diagnose(args)
        if cmd == "/menu":
            return _cmd_menu()
        return f"Unknown command: {cmd}\nSend /help for available commands."
    except Exception as e:
        log_event("ERROR", "commands", f"Command {cmd} failed: {e}")
        return f"❌ Command failed: {e}"


def _cmd_start() -> str:
    return ("👋 <b>Trading bot ready</b>\n\n"
            "Send /help for commands.")


def _cmd_help() -> str:
    return ("""<b>📚 Commands</b>

👉 <b>Tap /menu for the button-driven UI.</b>
Slash commands below are for power users / typed input.

<b>Trades</b>  (typed only — needs args)
<code>/buy TICKER SHARES PRICE</code>
<code>/sell TICKER SHARES PRICE</code>
/pnl — current P&amp;L snapshot

<b>Watchlist &amp; focus</b>
<code>/watch TICKER</code> · <code>/unwatch TICKER</code>
<code>/focus TICKER</code> · <code>/unfocus TICKER</code>
/list — show watchlist + focus

<b>Briefs</b>
/run — show options + fire a brief
<code>/cancel BRIEF</code> — stop a running brief
<code>/settime BRIEF HH:MM</code> — change schedule
  Examples: <code>/settime preclose 22:45</code> · <code>/settime preclose_sa 14:30</code>

<b>Method (TradingView webhook)</b>
/method — subcommand router
  <code>status · on · off · reset · history · test · debug</code>

<b>Watcher</b>
/watcher status|on|off

<b>Status</b>
/status · /times · /markets

<b>Follow-up</b>
<code>/ask RecID question</code> — or just reply to a brief

<b>Diagnostics</b>
<code>/diagnose [MIN]</code> · /trim_logs

<b>Control</b>
/pause · /resume · /menu · /help""")


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

    # Phase F guard: SA trade-side support not built yet. Reject SA-shaped
    # tickers to prevent corrupting TradeLog with mixed-currency rows.
    if _detect_market(ticker) == "SA":
        return ("❌ /buy and /sell don't support SA market yet. "
                "SA support for trades is a future patch. For now, "
                "record SA trades manually in the TradeLog tab.")

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

    # Phase F guard: SA trade-side support not built yet. Reject SA-shaped
    # tickers to prevent corrupting TradeLog with mixed-currency rows.
    if _detect_market(ticker) == "SA":
        return ("❌ /buy and /sell don't support SA market yet. "
                "SA support for trades is a future patch. For now, "
                "record SA trades manually in the TradeLog tab.")

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
    lines.append(f"Active markets: {', '.join(ACTIVE_MARKETS) or '—'}")
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
    # Phase A — pause state is now in the Config tab so it survives
    # Railway redeploys. The /tmp file is kept in sync as a transitional
    # safety net for any reader that hasn't migrated yet.
    if not sheets.write_config("PAUSED", "true"):
        return "❌ Could not save PAUSED=true to Config tab."
    try:
        with open(PAUSE_FILE, "w") as f:
            f.write(datetime.now(KSA_TZ).isoformat())
    except Exception as e:
        log_event("WARN", "commands",
                  f"Pause /tmp mirror write failed (Sheet still wins): {e}")
    return ("⏸️ <b>Briefs paused</b>\n"
            "Scheduled briefs will not send. Use /resume to re-enable.")


def _cmd_resume() -> str:
    if not sheets.write_config("PAUSED", "false"):
        return "❌ Could not save PAUSED=false to Config tab."
    if os.path.exists(PAUSE_FILE):
        try:
            os.remove(PAUSE_FILE)
        except Exception as e:
            log_event("WARN", "commands",
                      f"Pause /tmp mirror remove failed (Sheet still wins): {e}")
    return "▶️ <b>Briefs resumed</b>\nScheduled briefs re-enabled."


# ---------------------------------------------------------------------------
# Manual brief triggers and schedule management
# ---------------------------------------------------------------------------

def _format_runlist() -> str:
    """Brief-trigger menu shown when `/run` is called with no args.
    Phase A — formerly the standalone /runlist command; folded into
    /run since they were redundant."""
    sa_lines = ""
    if "SA" in ACTIVE_MARKETS:
        sa_lines = ("\n\n<b>🇸🇦 Saudi (Tadawul)</b>\n"
                    "<code>/run premarket-sa</code>\n"
                    "<code>/run midsession-sa</code>\n"
                    "<code>/run preclose-sa</code>\n"
                    "<code>/run eod-sa</code>")
    return ("<b>🚀 Manual brief triggers</b>\n"
            "─────────────\n"
            "<b>🇺🇸 US</b>\n"
            "<code>/run premarket</code> — full pre-market brief\n"
            "<code>/run midsession</code> — mid-session check\n"
            "<code>/run preclose</code> — pre-close verdict\n"
            "<code>/run eod</code> — end-of-day summary"
            f"{sa_lines}\n\n"
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
        user_facing = [b.replace("_", "-") for b in VALID_BRIEFS]
        return ("Usage: <code>/cancel BRIEF</code>\n"
                f"Briefs: {', '.join(user_facing)}\n"
                "Example: <code>/cancel premarket</code>\n\n"
                "Send /status to see what's running.")

    brief = _normalize_brief_arg(args[0])
    if brief not in VALID_BRIEFS:
        user_facing = [b.replace("_", "-") for b in VALID_BRIEFS]
        return (f"❌ Unknown brief: <code>{args[0]}</code>\n"
                f"Valid: {', '.join(user_facing)}")

    try:
        from webhook import app as webhook_app
        from core import analyst as analyst_mod
    except Exception as e:
        return f"❌ Could not reach scheduler: {e}"

    # Watcher uses per-(market, mode) _currently_running keys but a
    # single cancel flag per market. Map the user's brief arg to both.
    if brief == "watcher":
        running_keys = ("watcher_us_price", "watcher_us_news")
    elif brief == "watcher_sa":
        running_keys = ("watcher_sa_price", "watcher_sa_news")
    elif brief == "method":
        # Method runner registers as "method_tick" in _currently_running.
        running_keys = ("method_tick",)
    else:
        running_keys = (brief,)

    with webhook_app._running_lock:
        is_running = any(k in webhook_app._currently_running
                         for k in running_keys)

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
        return _format_runlist()

    brief = _normalize_brief_arg(args[0])
    if brief not in VALID_BRIEFS or brief.startswith("watcher"):
        # /run watcher and /run watcher_sa are not supported via this
        # path — the watcher has its own /watcher command surface and
        # spawn function.
        return (f"❌ Unknown brief: <code>{args[0]}</code>\n"
                f"Send <code>/run</code> with no args to see options.")

    if brief.endswith("_sa") and "SA" not in ACTIVE_MARKETS:
        return ("⏸ <b>SA market not active in ACTIVE_MARKETS</b> — "
                "set it to <code>US,SA</code> on Railway to enable.")

    try:
        from webhook import app as webhook_app
    except Exception as e:
        return f"❌ Could not reach scheduler: {e}"

    spawned, reason = webhook_app._spawn_brief(brief, source="telegram")
    user_facing = brief.replace("_", "-")
    if spawned:
        return (f"🚀 Running <b>{user_facing}</b> brief in background…\n"
                f"<i>Result will arrive in 30–90 seconds.</i>")
    else:
        return (f"⚠️ <b>{user_facing}</b> not started — {reason}.\n"
                f"Send /status to see what's running.")


def _cmd_times() -> str:
    try:
        from webhook import app as webhook_app
        active = webhook_app._active_times or {}
    except Exception:
        active = {}

    if not active:
        from webhook.app import (DEFAULT_TIMES, DEFAULT_TIMES_SA,
                                  CONFIG_KEY_PREFIX)
        cfg = sheets.read_config() or {}
        active = {}
        for brief, default in {**DEFAULT_TIMES, **DEFAULT_TIMES_SA}.items():
            active[brief] = cfg.get(
                f"{CONFIG_KEY_PREFIX}{brief.upper()}", default)

    # Phase A — SA times now live in _active_times (and pick up
    # Config-tab overrides). DEFAULT_TIMES_SA is only used as a final
    # fallback if _active_times is missing a key entirely.
    from webhook.app import DEFAULT_TIMES_SA

    label = {
        "premarket":  "Pre-market  ",
        "midsession": "Mid-session ",
        "preclose":   "Pre-close   ",
        "eod":        "End of day  ",
    }

    lines = ["<b>🕒 Brief schedule (KSA)</b>", "─────────────"]

    lines.append("")
    lines.append("<b>🇺🇸 US Market</b> <i>(Mon–Fri)</i>")
    for brief in ("premarket", "midsession", "preclose", "eod"):
        lines.append(f"  <code>{label[brief]} {active.get(brief, '?')}</code>")
    lines.append("  <i>Market hours: 16:30–23:00</i>")

    if "SA" in ACTIVE_MARKETS:
        lines.append("")
        lines.append("<b>🇸🇦 Saudi Market</b> <i>(Sun–Thu)</i>")
        for brief in ("premarket_sa", "midsession_sa",
                      "preclose_sa", "eod_sa"):
            short = brief.replace("_sa", "")
            hhmm = active.get(brief) or DEFAULT_TIMES_SA.get(brief, '?')
            lines.append(f"  <code>{label[short]} {hhmm}</code>")
        lines.append("  <i>Market hours: 10:00–15:00</i>")

    lines.append("")
    lines.append("Change times with: <code>/settime BRIEF HH:MM</code>")
    return "\n".join(lines)


def _cmd_settime(args: list) -> str:
    if len(args) < 2:
        return ("Usage: <code>/settime BRIEF HH:MM</code>\n"
                "US briefs: premarket, midsession, preclose, eod\n"
                "SA briefs: premarket_sa, midsession_sa, "
                "preclose_sa, eod_sa\n"
                "Example: <code>/settime preclose 22:45</code>\n"
                "Example: <code>/settime preclose_sa 14:30</code>")

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
        days = "Sun–Thu" if brief.endswith("_sa") else "Mon–Fri"
        return (f"✅ <b>Schedule updated</b>\n"
                f"<code>{brief}</code> → <code>{active}</code> "
                f"(KSA, {days})\n\n"
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

    # Pass chat_id for rate-limit tracking (single-user, but the limit
    # provides cost-burn protection regardless)
    result = followup.answer_followup(
        rec_id, question, chat_id=str(TELEGRAM_CHAT_ID)
    )
    if not result.get("ok"):
        return f"❌ {result.get('error', 'Unknown error')}"

    return (f"💬 <b>Re:</b> {result['ticker']} ({result['action']})\n"
            f"─────────────\n"
            f"{result['answer']}\n\n"
            f"<i>💰 ${result['cost_usd']:.4f}</i>")


# ---------------------------------------------------------------------------
# Watchlist & Focus management
# ---------------------------------------------------------------------------

def _validate_ticker_format(t: str) -> bool:
    """Cheap pre-filter for obviously-invalid ticker input.

    Accepts:
      - US: 1-6 chars, letters/digits + . or - (e.g. AAPL, BRK.B, US500)
      - SA: 1-4 digits (Tadawul codes, e.g. 2222)
    Real existence is checked by _verify_ticker_exists() (US only —
    SAHMK doesn't have a free 'exists?' endpoint, so SA tickers are
    accepted on shape alone). Alphanumeric tickers like US500 pass
    the format gate here and Twelve Data is the source of truth for
    whether the symbol actually trades.
    """
    if not t:
        return False
    t = t.strip().upper()
    if not (1 <= len(t) <= 6):
        return False
    if t.isdigit():
        # Saudi (Tadawul) codes are 4 digits today; allow 1-4 to be
        # safe in case of future suffixes like rights issues.
        return 1 <= len(t) <= 4
    return all(c.isalnum() or c in (".", "-") for c in t)


def _verify_ticker_exists(ticker: str) -> dict:
    """Hit Twelve Data's /quote endpoint to confirm the ticker is real.

    Returns:
        {"ok": True, "name": "...", "type": "...", "exchange": "..."}
                                           if the ticker resolves to data
        {"ok": False, "error": "..."}      if it doesn't
        {"ok": True, "warning": "..."}     if the API itself is down
                                           (graceful degradation)

    Costs one Twelve Data API call (free tier allows 800/day, plenty).
    """
    try:
        from config import TWELVE_DATA_KEY
        if not TWELVE_DATA_KEY:
            # No API key configured — fall through to format-only check.
            log_event("WARN", "commands",
                      "Twelve Data key missing; ticker existence not verified")
            return {"ok": True, "warning": "not_verified"}

        import requests
        r = requests.get(
            "https://api.twelvedata.com/quote",
            params={"symbol": ticker, "apikey": TWELVE_DATA_KEY},
            timeout=8,
        )
        if not r.ok:
            # Twelve Data itself is broken — don't block the user. Accept
            # with a warning so we degrade gracefully (same pattern as
            # FRED outages elsewhere in the bot).
            log_event("WARN", "commands",
                      f"Ticker check API returned {r.status_code}; "
                      f"accepting {ticker} unverified")
            return {"ok": True, "warning": f"price API down ({r.status_code})"}
        data = r.json()
        # Twelve Data returns {"code": 400/404, "message": "..."} for bad tickers
        if isinstance(data, dict) and data.get("code") and data.get("code") != 200:
            msg = data.get("message", f"code {data.get('code')}")
            return {"ok": False, "error": msg}
        # A real ticker has a "close" or "price" field
        if not (data.get("close") or data.get("price") or data.get("symbol")):
            return {"ok": False, "error": "no price data returned"}
        # Extract metadata for display: company name, instrument type
        # (Common Stock, ETF, etc.), and exchange. All optional — older API
        # responses may not include all of them; fall back to empty strings.
        return {
            "ok": True,
            "name": (data.get("name") or "").strip(),
            "type": (data.get("type") or "").strip(),
            "exchange": (data.get("exchange") or "").strip(),
        }
    except Exception as e:
        # If the API itself is down, don't block the user — accept with warning
        log_event("WARN", "commands",
                  f"Ticker existence check errored: {e}")
        return {"ok": True, "warning": str(e)}


def _format_ticker_summary(check: dict) -> str:
    """Build a 'this is what you added' line from a successful verify result.

    Examples of output:
      "NVIDIA Corporation · Common Stock · NASDAQ"
      "GraniteShares 2x Short NVDA Daily ETF · ETF · NASDAQ"
      ""    (if we have no info — render nothing)

    Used by /watch and /focus to confirm what the user actually added,
    so typos like NVD-when-you-meant-NVDA surface immediately.
    """
    if not check.get("ok"):
        return ""
    parts = []
    name = check.get("name", "")
    typ = check.get("type", "")
    exch = check.get("exchange", "")
    if name:
        parts.append(name)
    if typ:
        parts.append(typ)
    if exch:
        parts.append(exch)
    return " · ".join(parts)


def _is_unusual_instrument(check: dict) -> str:
    """If the ticker is a leveraged/inverse ETF or other instrument that
    might genuinely surprise the user, return a short warning string.
    Empty if normal.

    Plain ETFs like SPY, IBIT, SPWO are NOT flagged here — the name is
    already shown, and the user can read "ETF" in the summary line.
    Only flag instruments where typo-or-confusion is a real risk:
      - Leveraged or inverse ETFs (moves don't match the underlying)
      - Bond/mutual/closed-end funds (different liquidity, different risk)
    """
    if not check.get("ok"):
        return ""
    name = (check.get("name") or "").lower()
    typ = (check.get("type") or "").lower()

    # Leveraged or inverse ETFs — keywords that almost always mean this
    # is a derivative product, not a plain stock or index ETF
    leveraged_markers = ("2x ", "3x ", " 2x", " 3x", "leveraged",
                         "inverse", " short ", " bull ",
                         " bear ", "ultrapro", "ultrashort")
    for marker in leveraged_markers:
        if marker in f" {name} ":  # padded so word-boundary checks work
            return "leveraged or inverse ETF — moves opposite or amplified vs. underlying"

    # Bond / mutual funds — flag because liquidity and behavior differ
    if typ in ("mutual fund", "bond fund", "closed-end fund"):
        return f"this is a {check.get('type', 'fund')}, not a stock"

    # Plain ETFs are fine — no warning. Name in summary is enough.
    return ""


def _validate_ticker(t: str) -> bool:
    """Legacy alias kept for any internal callers; format-only check.
    Use _verify_ticker_exists() in /watch and /focus for real validation."""
    return _validate_ticker_format(t)


def _default_watchlist(market: str = "US") -> list:
    """Lazy import of the per-market default watchlist."""
    try:
        if market.upper() == "SA":
            from markets.saudi import config as sa_cfg
            return list(getattr(sa_cfg, "DEFAULT_WATCHLIST", []))
        from markets.us import config as us_cfg
        return list(us_cfg.DEFAULT_WATCHLIST)
    except Exception:
        return []


def _cmd_list() -> str:
    """Show current watchlist + focus, grouped by active market."""
    lines = ["<b>📋 Tracking</b>", "─────────────"]

    # Show every ACTIVE market plus US (always shown for legacy users
    # whose ACTIVE_MARKETS may not be set explicitly). Order: US first,
    # then SA, then any future markets.
    markets_to_show = ["US"]
    for m in ACTIVE_MARKETS:
        if m not in markets_to_show:
            markets_to_show.append(m)

    for market in markets_to_show:
        watch = sheets.read_watchlist(
            default=_default_watchlist(market), market=market
        )
        focus_rows = sheets.read_focus(market=market) or []
        # Coerce to str — gspread can return numeric tickers (Saudi
        # codes like 1321) as int, which crashes downstream .upper()/
        # .strip() calls and breaks /list rendering.
        focus = [str(r.get("Ticker", "")) for r in focus_rows
                 if r.get("Ticker") not in (None, "")]

        flag = "🇺🇸" if market == "US" else ("🇸🇦" if market == "SA" else "🌐")
        lines.append("")
        lines.append(f"<b>{flag} {market}</b>")
        if focus:
            lines.append(f"  🎯 Focus ({len(focus)}/{sheets.FOCUS_LIMIT}): "
                         + " ".join(f"<code>{t}</code>" for t in focus))
        else:
            lines.append("  🎯 Focus: <i>none</i>")
        if watch:
            lines.append(f"  👁 Watchlist: "
                         + " ".join(f"<code>{t}</code>" for t in watch))
        else:
            lines.append("  👁 Watchlist: <i>empty</i>")

    lines.append("")
    lines.append("<i>Use /watch /unwatch /focus /unfocus to change. "
                 "Market is auto-detected from ticker shape.</i>")
    return "\n".join(lines)


def _cmd_watch(args: list) -> str:
    if not args:
        return ("Usage: <code>/watch TICKER</code>\n"
                "Examples: <code>/watch NVDA</code> · "
                "<code>/watch 2222</code> (Aramco)")
    ticker = args[0].strip().upper()
    if not _validate_ticker_format(ticker):
        return f"❌ <code>{ticker}</code> doesn't look like a valid ticker."

    market = _detect_market(ticker)
    if market is None:
        return (f"❌ Can't tell which market <code>{ticker}</code> belongs to.\n"
                f"<i>Use a US ticker (letters, e.g. NVDA) or an SA "
                f"Tadawul code (digits, e.g. 2222).</i>")

    # Existence check: US only (Twelve Data /quote). SA accepted on
    # shape — SAHMK doesn't expose a free 'exists?' endpoint, and the
    # next data fetch will surface a bad ticker via the Logs tab.
    if market == "US":
        check = _verify_ticker_exists(ticker)
        if not check.get("ok"):
            return (f"❌ <code>{ticker}</code> isn't a recognized ticker.\n"
                    f"<i>{check.get('error', 'verification failed')}</i>")
    else:
        check = {"ok": True, "warning": "SA tickers accepted on shape only"}

    current = sheets.read_watchlist(
        default=_default_watchlist(market), market=market
    )
    if ticker in current:
        summary = _format_ticker_summary(check) if market == "US" else ""
        suffix = f"\n<i>{summary}</i>" if summary else ""
        return (f"ℹ️ <code>{ticker}</code> is already in your "
                f"{market} watchlist.{suffix}")

    new_list = current + [ticker]
    if not sheets.write_watchlist(new_list, market=market):
        return "❌ Failed to save watchlist. Check the Logs tab."

    lines = [f"✅ Added <code>{ticker}</code> to {market} watchlist."]

    if market == "US":
        summary = _format_ticker_summary(check)
        if summary:
            lines.append(f"<i>{summary}</i>")
        unusual = _is_unusual_instrument(check)
        if unusual:
            lines.append(f"⚠️ <i>{unusual}.</i>")
            lines.append(f"<i>If you meant something else, /unwatch "
                         f"{ticker} and try a different symbol.</i>")
        warning = check.get("warning")
        if warning:
            lines.append(f"<i>⚠️ Couldn't verify details ({warning}); "
                         f"accepted anyway.</i>")
    else:
        lines.append(f"<i>SA ticker shape accepted; first data fetch "
                     f"will confirm it's live.</i>")

    lines.append(f"<i>Now tracking {len(new_list)} in {market}: "
                 f"{', '.join(new_list)}</i>")
    return "\n".join(lines)


def _cmd_unwatch(args: list) -> str:
    if not args:
        return "Usage: <code>/unwatch TICKER</code>"
    ticker = args[0].strip().upper()
    market = _detect_market(ticker)
    if market is None:
        return (f"❌ Can't tell which market <code>{ticker}</code> belongs "
                f"to. Use letters for US, digits for SA.")
    current = sheets.read_watchlist(
        default=_default_watchlist(market), market=market
    )
    if ticker not in current:
        return (f"ℹ️ <code>{ticker}</code> isn't in your {market} watchlist."
                f"\nSend /list to see what is.")
    new_list = [t for t in current if t != ticker]
    if not sheets.write_watchlist(new_list, market=market):
        return "❌ Failed to save watchlist."
    return (f"✅ Removed <code>{ticker}</code> from {market} watchlist.\n"
            f"<i>Now tracking {len(new_list)} in {market}: "
            f"{', '.join(new_list) if new_list else '(empty)'}</i>")


def _cmd_focus(args: list) -> str:
    if not args:
        return ("Usage: <code>/focus TICKER</code>\n"
                f"Max {sheets.FOCUS_LIMIT} focus tickers per market; "
                f"oldest gets dropped.")
    ticker = args[0].strip().upper()
    if not _validate_ticker_format(ticker):
        return f"❌ <code>{ticker}</code> doesn't look like a valid ticker."

    market = _detect_market(ticker)
    if market is None:
        return (f"❌ Can't tell which market <code>{ticker}</code> belongs "
                f"to. Use letters for US, digits for SA.")

    if market == "US":
        check = _verify_ticker_exists(ticker)
        if not check.get("ok"):
            return (f"❌ <code>{ticker}</code> isn't a recognized ticker.\n"
                    f"<i>{check.get('error', 'verification failed')}</i>")
    else:
        check = {"ok": True, "warning": "SA tickers accepted on shape only"}

    result = sheets.add_focus(ticker, market=market)
    if not result.get("ok"):
        return f"❌ {result.get('error', 'Unknown error')}"

    lines = [f"✅ <code>{ticker}</code> added to {market} focus."]

    if market == "US":
        summary = _format_ticker_summary(check)
        if summary:
            lines.append(f"<i>{summary}</i>")
        unusual = _is_unusual_instrument(check)
        if unusual:
            lines.append(f"⚠️ <i>{unusual}.</i>")
            lines.append(f"<i>If you meant something else, /unfocus "
                         f"{ticker} and try a different symbol.</i>")
        if check.get("warning"):
            lines.append(f"<i>⚠️ Couldn't verify details ({check['warning']}); "
                         f"accepted anyway.</i>")
    else:
        lines.append(f"<i>SA ticker shape accepted; first data fetch "
                     f"will confirm it's live.</i>")

    if result.get("dropped"):
        lines.append(f"<i>Dropped <code>{result['dropped']}</code> "
                     f"(oldest in {market} focus, "
                     f"capped at {sheets.FOCUS_LIMIT}).</i>")

    return "\n".join(lines)


def _cmd_unfocus(args: list) -> str:
    if not args:
        return "Usage: <code>/unfocus TICKER</code>"
    ticker = args[0].strip().upper()
    result = sheets.remove_focus(ticker)
    if not result.get("ok"):
        return f"❌ {result.get('error', 'Unknown error')}"
    return f"✅ <code>{ticker}</code> removed from focus."


# ---------------------------------------------------------------------------
# Watcher (Phase D.5) — /watcher status|on|off
# ---------------------------------------------------------------------------

def _cmd_watcher(args: list) -> str:
    if not args:
        return ("Usage: <code>/watcher status|on|off</code>\n"
                "  • <code>/watcher status</code> — show last ticks + "
                "today's alert counts\n"
                "  • <code>/watcher on</code>  — enable the watcher\n"
                "  • <code>/watcher off</code> — disable the watcher")

    sub = args[0].lower().strip()

    if sub == "status":
        return _cmd_watcher_status()
    if sub == "on":
        if not sheets.write_config("WATCHER_ENABLED", "true"):
            return "❌ Could not save WATCHER_ENABLED=true to Config tab."
        return ("✅ <b>Watcher enabled</b>\n"
                "Will run on the next scheduled tick during US market "
                "hours.")
    if sub == "off":
        if not sheets.write_config("WATCHER_ENABLED", "false"):
            return "❌ Could not save WATCHER_ENABLED=false to Config tab."
        return ("🔕 <b>Watcher disabled</b>\n"
                "Scheduled ticks will skip silently. Re-enable with "
                "<code>/watcher on</code>.")

    return (f"❌ Unknown subcommand: <code>{sub}</code>\n"
            "Use: <code>/watcher status|on|off</code>")


def _cmd_watcher_status() -> str:
    """Show the watcher's enabled state, last tick times, and today's
    per-ticker alert counts from the WatcherCooldown tab."""
    from config import (WATCHER_ENABLED as PY_WATCHER_ENABLED,
                        WATCHER_DAILY_ALERT_CAP as PY_CAP,
                        WATCHER_PRICE_INTERVAL_MIN as PY_PRICE,
                        WATCHER_NEWS_INTERVAL_MIN as PY_NEWS,
                        WATCHER_INCLUDE_WATCHLIST as PY_INC_WATCH)

    cfg = sheets.read_config() or {}

    def _read_bool(key, fallback):
        raw = cfg.get(key)
        if raw is None or str(raw).strip() == "":
            return fallback
        return str(raw).strip().lower() == "true"

    def _read_int(key, fallback):
        raw = cfg.get(key)
        if raw is None or str(raw).strip() == "":
            return fallback
        try:
            return int(str(raw).strip())
        except (ValueError, TypeError):
            return fallback

    enabled = _read_bool("WATCHER_ENABLED", PY_WATCHER_ENABLED)
    cap = _read_int("WATCHER_DAILY_ALERT_CAP", PY_CAP)
    price_int = _read_int("WATCHER_PRICE_INTERVAL_MIN", PY_PRICE)
    news_int = _read_int("WATCHER_NEWS_INTERVAL_MIN", PY_NEWS)
    inc_watch = _read_bool("WATCHER_INCLUDE_WATCHLIST", PY_INC_WATCH)

    # Pull live last-run timestamps from the webhook scheduler. State
    # keys are per-(market, mode): "us_price", "us_news", "sa_price",
    # "sa_news". Missing → "—".
    last = {"us_price": "—", "us_news": "—",
            "sa_price": "—", "sa_news": "—"}
    try:
        from webhook import app as webhook_app
        for k in last:
            v = webhook_app._last_watcher_runs.get(k)
            if v:
                last[k] = v
    except Exception:
        pass

    today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    counts = sheets.read_cooldowns_for_date(today_str)

    lines = ["<b>🔔 WATCHER STATUS</b>", "─────────────"]
    lines.append(f"State: {'✅ ENABLED' if enabled else '🔕 DISABLED'}")
    lines.append(f"Daily cap per ticker: <code>{cap}</code>")
    lines.append(f"Price tick: every <code>{price_int}</code> min")
    lines.append(f"News tick:  every <code>{news_int}</code> min")
    lines.append(f"Watchlist included: "
                 f"<code>{'yes' if inc_watch else 'no'}</code>")
    lines.append("")
    lines.append("<b>🇺🇸 US — last ticks</b>")
    lines.append(f"  Price: <code>{last['us_price']}</code>")
    lines.append(f"  News:  <code>{last['us_news']}</code>")
    if "SA" in ACTIVE_MARKETS:
        lines.append("")
        lines.append("<b>🇸🇦 SA — last ticks</b>")
        lines.append(f"  Price: <code>{last['sa_price']}</code>")
        lines.append(f"  News:  <code>{last['sa_news']}</code>")
    lines.append("")
    lines.append("<b>Today's alerts (all markets)</b>")
    if counts:
        for c in counts:
            # Numeric ticker → SA, otherwise US (matches _detect_market)
            flag = "🇸🇦" if c["Ticker"].isdigit() else "🇺🇸"
            lines.append(f"  • {flag} <code>{c['Ticker']}</code>: "
                         f"{c['AlertCount']}/{cap}")
    else:
        lines.append("  <i>No alerts sent today.</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /markets — show ACTIVE_MARKETS state and per-market data-source health
# ---------------------------------------------------------------------------

def _cmd_markets() -> str:
    """Display which markets are currently active per the env var, and
    the on-the-ground status of each market's data sources."""
    from config import (TWELVE_DATA_KEY, MARKETAUX_KEY, FRED_KEY,
                        SAHMK_API_KEY)

    lines = ["<b>🌐 MARKETS</b>", "─────────────"]
    lines.append(f"ACTIVE_MARKETS: <code>{','.join(ACTIVE_MARKETS) or '—'}</code>")
    lines.append("")

    # US block
    us_active = "US" in ACTIVE_MARKETS
    lines.append(f"<b>🇺🇸 US</b> — {'✅ active' if us_active else '⏸ dormant'}")
    lines.append(f"  Hours (KSA): 16:30–23:00, Mon–Fri")
    lines.append(f"  Prices: Twelve Data "
                 f"{'✅' if TWELVE_DATA_KEY else '⚠️ key missing'}")
    lines.append(f"  News:   Marketaux "
                 f"{'✅' if MARKETAUX_KEY else '⚠️ key missing'}")
    lines.append(f"  Macro:  FRED "
                 f"{'✅' if FRED_KEY else '⚠️ key missing'}")
    lines.append("")

    # SA block
    sa_active = "SA" in ACTIVE_MARKETS
    lines.append(f"<b>🇸🇦 SA</b> — {'✅ active' if sa_active else '⏸ dormant'}")
    lines.append(f"  Hours (KSA): 10:00–15:00, Sun–Thu")
    lines.append(f"  Prices: SAHMK "
                 f"{'✅' if SAHMK_API_KEY else '⚠️ key missing'}")
    lines.append(f"  News:   Marketaux (.SR suffix) "
                 f"{'✅' if MARKETAUX_KEY else '⚠️ key missing'}")
    lines.append(f"  Halal screening: <i>not applied (verify yourself)</i>")
    if sa_active and not SAHMK_API_KEY:
        lines.append("")
        lines.append("⚠️ <b>SA active but SAHMK_API_KEY missing</b> — "
                     "SA briefs will fail. Set the env var on Railway.")
    lines.append("")
    lines.append("<i>Change ACTIVE_MARKETS via Railway env var "
                 "(e.g. <code>US,SA</code>) and redeploy.</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /method — option-method runner control (Phase G.2)
# ---------------------------------------------------------------------------

def _cmd_method(args: list) -> str:
    if not args:
        return ("Usage: <code>/method status|on|off|reset|history|test|debug</code>\n"
                "  • <code>/method status</code> — enabled, current state, "
                "today's signal count\n"
                "  • <code>/method on</code> — enable the runner\n"
                "  • <code>/method off</code> — disable the runner\n"
                "  • <code>/method reset</code> — clear in-flight state + "
                "today's cooldown\n"
                "  • <code>/method history</code> — last 10 signals\n"
                "  • <code>/method test</code> — fire a synthetic "
                "TradingView webhook to self-test\n"
                "  • <code>/method debug</code> — Databento data-source "
                "health probe (legacy polling path)")

    sub = args[0].lower().strip()
    if sub == "status":
        return _cmd_method_status()
    if sub == "on":
        if not sheets.write_config("METHOD_ENABLED", "true"):
            return "❌ Could not save METHOD_ENABLED=true to Config tab."
        return ("✅ <b>Option method enabled</b>\n"
                "TradingView webhook alerts will be honored. "
                "(Polling path is dormant in Phase G.4.)")
    if sub == "off":
        if not sheets.write_config("METHOD_ENABLED", "false"):
            return "❌ Could not save METHOD_ENABLED=false to Config tab."
        return ("🔕 <b>Option method disabled</b>\n"
                "TradingView webhooks will be rejected with HTTP 403. "
                "Re-enable with <code>/method on</code>.")
    if sub == "reset":
        return _cmd_method_reset()
    if sub == "history":
        return _cmd_method_history()
    if sub == "test":
        return _cmd_method_test()
    if sub == "debug":
        return _cmd_method_debug()
    return (f"❌ Unknown subcommand: <code>{sub}</code>\n"
            "Use: <code>/method status|on|off|reset|history|test|debug</code>")


def _cmd_method_status() -> str:
    from config import (METHOD_ENABLED as PY_METHOD_ENABLED,
                        METHOD_INTERVAL_SEC as PY_INT,
                        METHOD_TICKER as PY_TICKER,
                        METHOD_MAX_DAILY_SIGNALS as PY_CAP)

    cfg = sheets.read_config() or {}
    raw = cfg.get("METHOD_ENABLED")
    if raw is None or str(raw).strip() == "":
        enabled = PY_METHOD_ENABLED
    else:
        enabled = str(raw).strip().lower() == "true"

    today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    cd_call = sheets.read_method_cooldown(today_str, "call") or {}
    cd_put = sheets.read_method_cooldown(today_str, "put") or {}

    try:
        from core import method_state
        snap = method_state.get_tracker().state_snapshot()
    except Exception as e:
        snap = {}
        log_event("WARN", "commands",
                  f"/method status tracker snapshot failed: {e}")

    last_started = "—"
    last_completed = "—"
    interval_sec = PY_INT
    try:
        from webhook import app as webhook_app
        last_started = webhook_app._last_method_run.get("started") or "—"
        last_completed = webhook_app._last_method_run.get("completed") or "—"
        interval_sec = (webhook_app._active_method_interval_sec
                        or PY_INT)
    except Exception:
        pass

    def _fmt_dir(d):
        s = snap.get(d, {})
        state = s.get("state", "—")
        sig = s.get("signal_id") or "—"
        return f"{state} ({sig})"

    # Phase G.4 — runner is webhook-driven; the polling tick stats
    # below stay around for diagnostic continuity but should read "—"
    # while the runner is dormant.
    from config import TRADINGVIEW_WEBHOOK_SECRET
    secret_set = bool(TRADINGVIEW_WEBHOOK_SECRET)

    lines = ["<b>🎯 OPTION METHOD STATUS</b>", "─────────────"]
    lines.append(f"State: {'✅ ENABLED' if enabled else '🔕 DISABLED'}")
    lines.append("Mode: <code>webhook-driven (TradingView)</code>")
    lines.append(f"Webhook secret: "
                 f"{'✅ set' if secret_set else '⚠️ not set'}")
    lines.append(f"Ticker: <code>{PY_TICKER}</code>")
    lines.append(f"Polling interval (dormant): "
                 f"every <code>{interval_sec}</code> sec")
    lines.append(f"Daily cap: <code>{PY_CAP}</code> per direction")
    lines.append(f"Last tick started: <code>{last_started}</code>")
    lines.append(f"Last tick completed: <code>{last_completed}</code>")
    lines.append("")
    lines.append("<b>📊 Direction state</b>")
    lines.append(f"  CALL: <code>{_fmt_dir('call')}</code>")
    lines.append(f"  PUT:  <code>{_fmt_dir('put')}</code>")
    lines.append("")
    lines.append("<b>📅 Today's setup count</b>")
    lines.append(f"  CALL: {cd_call.get('SetupCount', 0) or 0}/{PY_CAP}")
    lines.append(f"  PUT:  {cd_put.get('SetupCount', 0) or 0}/{PY_CAP}")
    return "\n".join(lines)


def _cmd_method_reset() -> str:
    """Force-DONE all active signals + clear today's cooldown rows.
    In-memory tracker is wiped too so the next tick starts clean."""
    today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    sheet_signals_ok = sheets.reset_method_signals_active()
    sheet_cd_ok = sheets.reset_method_cooldown(today_str)
    try:
        from core import method_state
        method_state.get_tracker().force_reset()
    except Exception as e:
        return f"❌ Tracker reset failed: {e}"

    notes = []
    if not sheet_signals_ok:
        notes.append("MethodSignals reset failed (see Logs)")
    if not sheet_cd_ok:
        notes.append(f"MethodCooldown reset for {today_str} failed")
    suffix = ("\n<i>" + "; ".join(notes) + "</i>") if notes else ""
    return ("🔄 <b>Method state reset</b>\n"
            "  • In-memory tracker cleared\n"
            "  • Active signals force-DONE in MethodSignals\n"
            f"  • Today's cooldown rows cleared ({today_str})"
            f"{suffix}")


def _cmd_method_debug() -> str:
    """`/method debug` — manual data-source health probe for the dormant
    polling path. Hits Databento with the configured METHOD_TICKER + a
    1m bar; never called on a schedule. Used after Railway redeploys
    to confirm the DATABENTO_API_KEY env var actually reached the
    container. Phase A: renamed from /method_health and folded into
    the /method subcommand surface."""
    from config import METHOD_TICKER, DATABENTO_DATASET
    try:
        from core import databento_client
    except Exception as e:
        return f"❌ Could not import databento_client: {e}"

    try:
        result = databento_client.health_check()
    except Exception as e:
        return f"❌ Databento health-check crashed: {e}"

    ok = result.get("ok")
    err = result.get("error")
    last = result.get("latest_close")

    lines = ["<b>🩺 METHOD DATA-SOURCE HEALTH</b>", "─────────────"]
    lines.append(f"Provider: <code>Databento ({DATABENTO_DATASET})</code>")
    lines.append(f"Symbol:   <code>{METHOD_TICKER}</code>")
    if ok:
        lines.append(f"Status:   ✅ ok")
        lines.append(f"Last close: <code>{last}</code>")
    else:
        lines.append(f"Status:   ❌ failed")
        lines.append(f"Error: <code>{err or 'unknown'}</code>")
        lines.append("")
        lines.append("<i>Check Logs tab for the underlying HTTP error. "
                     "Most common causes: missing DATABENTO_API_KEY env "
                     "var on Railway, or symbol/dataset mismatch.</i>")
    return "\n".join(lines)


def _cmd_method_test() -> str:
    """`/method test` — fire a synthetic pre_signal payload at our own
    /webhook/tradingview endpoint to verify end-to-end wiring.

    Useful right after configuring TRADINGVIEW_WEBHOOK_SECRET on
    Railway: if the bot answers with a Telegram pre-signal alert
    within a few seconds, the secret + dispatch path are correct.

    Phase A: invoked via /method test subcommand (was top-level
    /method_test before consolidation)."""
    from config import TRADINGVIEW_WEBHOOK_SECRET
    if not TRADINGVIEW_WEBHOOK_SECRET:
        return ("❌ <code>TRADINGVIEW_WEBHOOK_SECRET</code> not set. "
                "Configure it on Railway before running this self-test.")

    # Always self-call via localhost. Going through PUBLIC_URL forces a
    # round-trip through Railway's edge (DNS + TLS + ingress) which can
    # fail even when the container is healthy — and the goal here is to
    # verify the local handler + secret, not the edge.
    port = os.environ.get("PORT", "8080")
    url = f"http://localhost:{port}/webhook/tradingview"

    now = datetime.now(KSA_TZ)
    payload = {
        "secret": TRADINGVIEW_WEBHOOK_SECRET,
        "event": "pre_signal",
        "direction": "call",
        "symbol": "TEST:US500",
        "trigger_price": 7232.50,
        "stop_price": 7224.30,
        "tp1": 7240.70,
        "tp2": 7245.00,
        "tp3": 7250.00,
        "fractal_high": 7231.10,
        "fractal_low": 7220.50,
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%SZ"),
    }

    try:
        import requests
        r = requests.post(url, json=payload, timeout=5)
        status = r.status_code
        body = (r.text or "")[:300]
        ok = r.ok
    except Exception as e:
        return f"❌ Test POST to <code>{url}</code> failed: <code>{e}</code>"

    icon = "✅" if ok else "⚠️"
    return (f"{icon} <b>Method webhook self-test fired</b>\n"
            f"URL: <code>{url}</code>\n"
            f"HTTP: <code>{status}</code>\n"
            f"Body: <code>{body}</code>\n\n"
            f"<i>If accepted, a synthetic CALL pre-signal Telegram "
            f"alert should arrive shortly.</i>")


def _cmd_diagnose(args: list) -> str:
    """Run the diagnostic Haiku agent on demand. Same logic as the
    30-min cron tick but with a wider default window (60m) so a single
    /diagnose covers the time since the last scheduled tick.

    Optional first arg overrides the window in minutes, e.g.
    <code>/diagnose 240</code> to look back 4 hours."""
    window = 60
    if args:
        try:
            n = int(args[0])
            if 1 <= n <= 1440:
                window = n
        except (ValueError, TypeError):
            return ("Usage: <code>/diagnose [WINDOW_MINUTES]</code>\n"
                    "Default 60 min. Range 1–1440. "
                    "Example: <code>/diagnose 240</code>")

    try:
        from core import log_analyst
        result = log_analyst.diagnose_now(window_minutes=window)
    except Exception as e:
        return f"❌ /diagnose crashed: <code>{e}</code>"

    if not result.get("ran"):
        reason = result.get("error") or result.get("skipped") or "unknown"
        return f"⚠️ /diagnose did not run: <code>{reason}</code>"

    rows = result.get("rows", 0)
    filtered = result.get("filtered", 0)
    patterns = result.get("patterns", 0)
    alerted = result.get("alerted", 0)
    return (f"🔎 <b>Diagnose ({window}m window)</b>\n"
            f"Logs scanned: <code>{rows}</code>\n"
            f"WARN/ERROR in window: <code>{filtered}</code>\n"
            f"Patterns ≥10: <code>{patterns}</code>\n"
            f"Alerts sent (fresh this hour): <code>{alerted}</code>")


def _cmd_trim_logs() -> str:
    """Manually trigger the Logs-tab trim. Useful right after an
    incident where logs piled up faster than the 6-hourly cron, or
    to confirm the 50000-row cap is doing what we think it does."""
    from config import MAX_LOG_ROWS
    ss = sheets._get_spreadsheet()
    if ss is None:
        return "❌ No spreadsheet access (check GOOGLE_SA_JSON env var)."

    try:
        ws = ss.worksheet("Logs")
        before = max(0, ws.row_count - 1)
    except Exception as e:
        return f"❌ Could not read Logs tab row count: <code>{e}</code>"

    try:
        result = sheets.trim_logs_if_needed()
    except Exception as e:
        return f"❌ trim_logs crashed: <code>{e}</code>"

    if result.get("error"):
        return (f"❌ Trim failed: <code>{result['error']}</code>\n"
                f"Before: <code>{before}</code> rows · cap "
                f"<code>{MAX_LOG_ROWS}</code>")

    deleted = result.get("deleted", 0)
    remaining = result.get("remaining", before)
    if not result.get("trimmed"):
        return (f"ℹ️ <b>Logs under cap, nothing to do.</b>\n"
                f"Before: <code>{before}</code> · "
                f"Deleted: <code>0</code> · "
                f"After: <code>{before}</code>\n"
                f"<i>Cap is {MAX_LOG_ROWS} rows.</i>")

    return (f"✂️ <b>Logs trimmed</b>\n"
            f"Before: <code>{before}</code> · "
            f"Deleted: <code>{deleted}</code> · "
            f"After: <code>{remaining}</code>\n"
            f"<i>Cap is {MAX_LOG_ROWS} rows.</i>")


def _cmd_method_history() -> str:
    rows = sheets.read_method_signals(limit=10) or []
    if not rows:
        return ("<b>🎯 OPTION METHOD HISTORY</b>\n"
                "─────────────\n"
                "<i>No signals yet.</i>")
    lines = ["<b>🎯 OPTION METHOD HISTORY</b>",
             "<i>Most recent first · last 10</i>",
             "─────────────"]
    for r in rows:
        sid = r.get("SignalID", "—")
        date = r.get("Date", "")
        time = r.get("Time_KSA", "")
        direction = (r.get("Direction", "") or "").upper()
        state = (r.get("State", "") or "").upper()
        trig = r.get("TriggerPrice", "")
        tp1_hit = str(r.get("TP1Hit", "FALSE")).upper() == "TRUE"
        invalid = r.get("InvalidatedAt", "")
        result_emoji = "✅" if tp1_hit else (
            "❌" if invalid else "•"
        )
        lines.append(
            f"{result_emoji} <code>{date} {time}</code> "
            f"{direction} @ <code>{trig}</code> · "
            f"<i>{state}</i>"
        )
        lines.append(f"   <code>{sid}</code>")
    return "\n".join(lines)


def _cmd_menu() -> str:
    """Phase B — open the inline-keyboard /menu UI. Sends the message
    directly (the dispatcher path doesn't carry inline_keyboard) and
    returns "" so the dispatcher's `if reply` skips the second send."""
    try:
        from core import menu
        text, kb = menu.render_main()
        telegram_client.send_message(text, inline_keyboard=kb)
    except Exception as e:
        log_event("ERROR", "commands", f"/menu render failed: {e}")
        return f"❌ /menu failed: <code>{e}</code>"
    return ""


def _is_paused() -> bool:
    # Phase A — authoritative source is the Config tab (sheets.is_paused).
    # The /tmp file is no longer authoritative; do not check it here.
    return sheets.is_paused()


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
