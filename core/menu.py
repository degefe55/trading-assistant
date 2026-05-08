"""
Telegram /menu — inline-keyboard UI for the trading assistant.

Single command (/menu) opens the main screen. Every screen is built
on demand from current state (Sheet config, in-memory tracker, env).
State is encoded in callback_data — never in process memory — so
concurrent menu navigations from multiple devices can't race.

Callback prefixes (kept short to stay under Telegram's 64-byte
callback_data ceiling):

  m:home               main menu
  t:home               toggles screen
  t:tw | tm | tp | td  flip Watcher / Method / Pause / Diagnostic
  r:home               run-a-brief screen
  r:<brief>            fire <brief> in background
  w:home               watchlist screen
  w:rm:<TICKER>        remove ticker from watchlist
  w:foc:<TICKER>       promote to focus
  w:unfoc:<TICKER>     remove from focus
  x:home               method screen
  x:on | off | reset | hist | test | debug
  s:home               schedule screen
  s:p:<brief>          pick brief → HH grid
  s:h:<brief>:HH       pick HH → MM grid
  s:m:<brief>:HH:MM    pick MM → save and redraw
  s:custom:<brief>     hint to type /settime brief HH:MM
  d:home               diagnostics screen
  d:diag | trim | mdebug | mtest

The render functions in this module return (text, inline_keyboard)
tuples. The webhook callback dispatcher edits the original menu
message in place; the /menu command sends the first message.
"""
import os
from datetime import datetime

from config import (KSA_TZ, ACTIVE_MARKETS, MOCK_MODE, MAX_LOG_ROWS,
                    METHOD_TICKER, METHOD_MAX_DAILY_SIGNALS,
                    TRADINGVIEW_WEBHOOK_SECRET,
                    METHOD_ENABLED as PY_METHOD_ENABLED,
                    WATCHER_ENABLED as PY_WATCHER_ENABLED,
                    WATCHER_DAILY_ALERT_CAP,
                    WATCHER_PRICE_INTERVAL_MIN, WATCHER_NEWS_INTERVAL_MIN,
                    DIAGNOSTIC_AGENT_ENABLED as PY_DIAG_ENABLED)
from core import sheets, telegram_client
from core.logger import log_event


# ============================================================
# State readers (all sheet-backed, env fallback)
# ============================================================

def _read_bool_setting(key: str, env_default: bool) -> bool:
    """Sheet wins, env fallback. Mirrors the watcher/method pattern."""
    try:
        cfg = sheets.read_config() or {}
        raw = cfg.get(key)
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip().lower() == "true"
    except Exception as e:
        log_event("WARN", "menu", f"read_bool({key}) failed: {e}")
    return env_default


def _watcher_enabled() -> bool:
    return _read_bool_setting("WATCHER_ENABLED", PY_WATCHER_ENABLED)


def _method_enabled() -> bool:
    return _read_bool_setting("METHOD_ENABLED", PY_METHOD_ENABLED)


def _diagnostic_enabled() -> bool:
    return _read_bool_setting("DIAGNOSTIC_AGENT_ENABLED", PY_DIAG_ENABLED)


def _paused() -> bool:
    return sheets.is_paused()


# ============================================================
# Common keyboard pieces
# ============================================================

NAV_HOME = {"text": "🏠 Home", "callback_data": "m:home"}


def _nav_row(back_to: str = None) -> list:
    """Bottom navigation row. `back_to` is a callback_data target for
    the ◀ Back button; if omitted, only Home is rendered."""
    row = []
    if back_to:
        row.append({"text": "◀ Back", "callback_data": back_to})
    row.append(NAV_HOME)
    return row


# ============================================================
# Main menu
# ============================================================

def render_main() -> tuple:
    """Top-level /menu screen — six categories."""
    text = (
        "<b>🤖 Trading bot menu</b>\n"
        "─────────────\n"
        "Tap a category to drill in. All screens are stateless — "
        "tap 🏠 Home or ◀ Back any time.\n\n"
        "<i>Slash commands still work for power users — see /help.</i>"
    )
    keyboard = [
        [{"text": "📊 Status",         "callback_data": "m:status"}],
        [{"text": "🚀 Run a brief",    "callback_data": "r:home"}],
        [{"text": "👁 Watchlist",      "callback_data": "w:home"}],
        [{"text": "⚙️ Toggles",        "callback_data": "t:home"}],
        [{"text": "🎯 Method",         "callback_data": "x:home"}],
        [{"text": "🔧 Schedule",       "callback_data": "s:home"}],
        [{"text": "🛠 Diagnostics",    "callback_data": "d:home"}],
    ]
    return text, keyboard


# ============================================================
# Status (consolidated /status + /list + /times + /markets)
# ============================================================

def render_status() -> tuple:
    """Phase B — merge of /status + /list + /times + /markets into one
    scrollable screen."""
    from config import (TWELVE_DATA_KEY, MARKETAUX_KEY, FRED_KEY,
                        SAHMK_API_KEY)
    paused = _paused()
    positions = sheets.read_positions() or []
    now_ksa = datetime.now(KSA_TZ).strftime('%Y-%m-%d %H:%M')

    lines = ["<b>📊 STATUS</b>", "─────────────",
             f"Time (KSA): {now_ksa}",
             f"Scheduled briefs: {'⏸️ PAUSED' if paused else '✅ active'}",
             f"Active markets: {', '.join(ACTIVE_MARKETS) or '—'}",
             f"Mock mode: {'ON' if MOCK_MODE else 'OFF'}",
             f"Open positions: {len(positions)}"]

    # /list — watchlist + focus per market
    lines.append("")
    lines.append("<b>📋 Watchlist + Focus</b>")
    markets_to_show = ["US"]
    for m in ACTIVE_MARKETS:
        if m not in markets_to_show:
            markets_to_show.append(m)
    for market in markets_to_show:
        from core.commands import _default_watchlist
        watch = sheets.read_watchlist(
            default=_default_watchlist(market), market=market) or []
        focus_rows = sheets.read_focus(market=market) or []
        focus = [str(r.get("Ticker", "")) for r in focus_rows
                 if r.get("Ticker") not in (None, "")]
        flag = "🇺🇸" if market == "US" else ("🇸🇦" if market == "SA" else "🌐")
        lines.append(f"  {flag} <b>{market}</b>")
        if focus:
            lines.append(f"    🎯 Focus: " +
                         " ".join(f"<code>{t}</code>" for t in focus))
        if watch:
            lines.append(f"    👁 Watch: " +
                         " ".join(f"<code>{t}</code>" for t in watch))
        if not focus and not watch:
            lines.append("    <i>(empty)</i>")

    # /times — brief schedule
    lines.append("")
    lines.append("<b>🕒 Brief schedule</b>")
    try:
        from webhook import app as webhook_app
        active = webhook_app._active_times or {}
    except Exception:
        active = {}
    label_us = {"premarket": "Pre-market", "midsession": "Mid-session",
                "preclose": "Pre-close", "eod": "End of day"}
    lines.append("  🇺🇸 US (Mon–Fri)")
    for b in ("premarket", "midsession", "preclose", "eod"):
        lines.append(f"    {label_us[b]}: <code>{active.get(b, '—')}</code>")
    if "SA" in ACTIVE_MARKETS:
        lines.append("  🇸🇦 SA (Sun–Thu)")
        for b in ("premarket_sa", "midsession_sa",
                  "preclose_sa", "eod_sa"):
            short = b.replace("_sa", "")
            lines.append(f"    {label_us[short]}: "
                         f"<code>{active.get(b, '—')}</code>")

    # /markets — data-source health
    lines.append("")
    lines.append("<b>🌐 Data sources</b>")
    lines.append(f"  🇺🇸 Twelve Data {'✅' if TWELVE_DATA_KEY else '⚠️'} · "
                 f"Marketaux {'✅' if MARKETAUX_KEY else '⚠️'} · "
                 f"FRED {'✅' if FRED_KEY else '⚠️'}")
    if "SA" in ACTIVE_MARKETS:
        lines.append(f"  🇸🇦 SAHMK {'✅' if SAHMK_API_KEY else '⚠️'} · "
                     f"Marketaux {'✅' if MARKETAUX_KEY else '⚠️'}")

    return "\n".join(lines), [_nav_row()]


# ============================================================
# Toggles
# ============================================================

def render_toggles() -> tuple:
    """Four sheet-backed toggles. State drawn live; tap flips and
    redraws the same screen with the new state."""
    w = _watcher_enabled()
    m = _method_enabled()
    p = _paused()
    d = _diagnostic_enabled()

    def _label(name: str, on: bool, on_text="ON", off_text="OFF") -> str:
        return f"{name}: {'✅ ' + on_text if on else '🔕 ' + off_text}"

    text = (
        "<b>⚙️ TOGGLES</b>\n"
        "─────────────\n"
        "Tap a row to flip. State persists in the Config tab and "
        "survives Railway redeploys.\n\n"
        "<i>US/SA market toggles are env-only — set ACTIVE_MARKETS on "
        "Railway and redeploy.</i>"
    )
    keyboard = [
        [{"text": _label("Watcher", w), "callback_data": "t:tw"}],
        [{"text": _label("Method", m),  "callback_data": "t:tm"}],
        [{"text": _label("Briefs", not p, on_text="Active",
                          off_text="Paused"),
          "callback_data": "t:tp"}],
        [{"text": _label("Diagnostic", d), "callback_data": "t:td"}],
        _nav_row(back_to="m:home"),
    ]
    return text, keyboard


def _flip_toggle(key: str) -> str:
    """Mutate one Sheet toggle. Returns a short toast for the user."""
    if key == "tw":
        new = "false" if _watcher_enabled() else "true"
        sheets.write_config("WATCHER_ENABLED", new)
        return f"Watcher → {new}"
    if key == "tm":
        new = "false" if _method_enabled() else "true"
        sheets.write_config("METHOD_ENABLED", new)
        return f"Method → {new}"
    if key == "tp":
        new = "false" if _paused() else "true"
        sheets.write_config("PAUSED", new)
        return f"Paused → {new}"
    if key == "td":
        new = "false" if _diagnostic_enabled() else "true"
        sheets.write_config("DIAGNOSTIC_AGENT_ENABLED", new)
        return f"Diagnostic → {new}"
    return f"Unknown toggle: {key}"


# ============================================================
# Schedule (settime button picker)
# ============================================================

# Internal short-codes for compactness in callback_data. Each maps to
# a brief identifier used everywhere else (DEFAULT_TIMES + DEFAULT_TIMES_SA).
_BRIEF_CODES = {
    "pm":  "premarket",
    "ms":  "midsession",
    "pc":  "preclose",
    "eo":  "eod",
    "pms": "premarket_sa",
    "mss": "midsession_sa",
    "pcs": "preclose_sa",
    "eos": "eod_sa",
}
_CODE_BY_BRIEF = {v: k for k, v in _BRIEF_CODES.items()}


def _brief_label(brief: str) -> str:
    flag = "🇸🇦" if brief.endswith("_sa") else "🇺🇸"
    label_map = {"premarket": "Pre-market", "midsession": "Mid-session",
                 "preclose": "Pre-close", "eod": "End of day"}
    short = brief.replace("_sa", "")
    return f"{flag} {label_map.get(short, brief)}"


def _active_times() -> dict:
    """Pull the live brief-time map from the scheduler. Falls back to
    DEFAULT_TIMES + DEFAULT_TIMES_SA if the scheduler hasn't booted."""
    try:
        from webhook import app as webhook_app
        if webhook_app._active_times:
            return dict(webhook_app._active_times)
    except Exception:
        pass
    from webhook.app import DEFAULT_TIMES, DEFAULT_TIMES_SA
    return {**DEFAULT_TIMES, **DEFAULT_TIMES_SA}


def render_schedule() -> tuple:
    """Top-of-Schedule screen — pick a brief to edit."""
    times = _active_times()
    text = (
        "<b>🔧 SCHEDULE</b>\n"
        "─────────────\n"
        "Tap a brief to change its time. Times persist in the Config "
        "tab and apply on the next scheduled run."
    )
    keyboard = []
    us_briefs = ["premarket", "midsession", "preclose", "eod"]
    for b in us_briefs:
        keyboard.append([{
            "text": f"{_brief_label(b)} ({times.get(b, '—')})",
            "callback_data": f"s:p:{_CODE_BY_BRIEF[b]}",
        }])
    if "SA" in ACTIVE_MARKETS:
        sa_briefs = ["premarket_sa", "midsession_sa", "preclose_sa", "eod_sa"]
        for b in sa_briefs:
            keyboard.append([{
                "text": f"{_brief_label(b)} ({times.get(b, '—')})",
                "callback_data": f"s:p:{_CODE_BY_BRIEF[b]}",
            }])
    keyboard.append(_nav_row(back_to="m:home"))
    return text, keyboard


def render_schedule_pick_hh(brief: str) -> tuple:
    """HH grid — 24 buttons in 4 rows of 6."""
    code = _CODE_BY_BRIEF.get(brief, brief)
    text = (f"<b>🔧 {_brief_label(brief)}</b>\n"
            f"─────────────\n"
            f"Pick the hour (24h, KSA).")
    keyboard = []
    for row_start in range(0, 24, 6):
        row = []
        for h in range(row_start, row_start + 6):
            row.append({
                "text": f"{h:02d}",
                "callback_data": f"s:h:{code}:{h:02d}",
            })
        keyboard.append(row)
    keyboard.append(_nav_row(back_to="s:home"))
    return text, keyboard


def render_schedule_pick_mm(brief: str, hh: str) -> tuple:
    """MM grid — 6 common values + custom."""
    code = _CODE_BY_BRIEF.get(brief, brief)
    text = (f"<b>🔧 {_brief_label(brief)} · {hh}:??</b>\n"
            f"─────────────\n"
            f"Pick the minute, or send <code>/settime {brief} {hh}:MM</code> "
            f"directly for a custom value.")
    keyboard = []
    row = []
    for m in (0, 10, 20, 30, 40, 50):
        row.append({
            "text": f":{m:02d}",
            "callback_data": f"s:m:{code}:{hh}:{m:02d}",
        })
    keyboard.append(row)
    keyboard.append([{
        "text": "Custom (type /settime …)",
        "callback_data": f"s:custom:{code}",
    }])
    keyboard.append(_nav_row(back_to=f"s:p:{code}"))
    return text, keyboard


def apply_schedule_change(brief: str, hh: str, mm: str) -> str:
    """Write Config row + rebuild scheduler. Returns a toast string."""
    hhmm = f"{hh}:{mm}"
    from webhook.app import CONFIG_KEY_PREFIX
    key = f"{CONFIG_KEY_PREFIX}{brief.upper()}"
    try:
        ok = sheets.write_config(key, hhmm)
    except Exception as e:
        return f"❌ Save failed: {e}"
    if not ok:
        return "❌ Could not write Config tab"
    try:
        from webhook import app as webhook_app
        webhook_app.rebuild_schedule()
    except Exception as e:
        log_event("WARN", "menu",
                  f"rebuild_schedule failed (saved anyway): {e}")
        return f"⚠️ Saved {hhmm} but reschedule failed"
    return f"✅ {brief} → {hhmm}"


# ============================================================
# Convenience: dispatch entry from webhook callback
# ============================================================
# Handlers below take a parent_message_id and return a small dict for
# logging. They edit the message in place.

def open_main(message_id: int) -> dict:
    text, kb = render_main()
    telegram_client.edit_message_text(message_id, text,
                                      inline_keyboard=kb)
    return {"screen": "m:home"}


def open_status(message_id: int) -> dict:
    text, kb = render_status()
    telegram_client.edit_message_text(message_id, text,
                                      inline_keyboard=kb)
    return {"screen": "m:status"}


def open_toggles(message_id: int, toast: str = None) -> dict:
    text, kb = render_toggles()
    if toast:
        text = f"<i>{toast}</i>\n\n" + text
    telegram_client.edit_message_text(message_id, text,
                                      inline_keyboard=kb)
    return {"screen": "t:home"}


def open_schedule(message_id: int) -> dict:
    text, kb = render_schedule()
    telegram_client.edit_message_text(message_id, text,
                                      inline_keyboard=kb)
    return {"screen": "s:home"}


def open_schedule_pick_hh(message_id: int, brief: str) -> dict:
    text, kb = render_schedule_pick_hh(brief)
    telegram_client.edit_message_text(message_id, text,
                                      inline_keyboard=kb)
    return {"screen": f"s:p:{brief}"}


def open_schedule_pick_mm(message_id: int, brief: str, hh: str) -> dict:
    text, kb = render_schedule_pick_mm(brief, hh)
    telegram_client.edit_message_text(message_id, text,
                                      inline_keyboard=kb)
    return {"screen": f"s:h:{brief}:{hh}"}


# ============================================================
# Run a brief (Phase C)
# ============================================================

def _running_briefs() -> set:
    """Snapshot of which briefs/method ticks are in flight right now.
    Reads webhook.app._currently_running under its existing lock."""
    try:
        from webhook import app as webhook_app
        with webhook_app._running_lock:
            return set(webhook_app._currently_running)
    except Exception as e:
        log_event("WARN", "menu", f"_running_briefs failed: {e}")
        return set()


def render_run() -> tuple:
    """Brief launcher screen. Shows US briefs always, SA briefs when SA
    is active, plus a Cancel button per currently-running brief."""
    running = _running_briefs()
    text = (
        "<b>🚀 RUN A BRIEF</b>\n"
        "─────────────\n"
        "Tap a brief to fire it in the background. The result message "
        "lands in 30–90 seconds. Re-tapping while a brief is running "
        "is a no-op."
    )
    if running:
        listed = ", ".join(sorted(running))
        text += f"\n\n<i>Currently running: {listed}</i>"

    keyboard = []
    keyboard.append([{"text": "🇺🇸 US Pre-market",
                       "callback_data": "r:premarket"}])
    keyboard.append([{"text": "🇺🇸 US Mid-session",
                       "callback_data": "r:midsession"}])
    keyboard.append([{"text": "🇺🇸 US Pre-close",
                       "callback_data": "r:preclose"}])
    keyboard.append([{"text": "🇺🇸 US End of day",
                       "callback_data": "r:eod"}])
    if "SA" in ACTIVE_MARKETS:
        keyboard.append([{"text": "🇸🇦 SA Pre-market",
                           "callback_data": "r:premarket_sa"}])
        keyboard.append([{"text": "🇸🇦 SA Mid-session",
                           "callback_data": "r:midsession_sa"}])
        keyboard.append([{"text": "🇸🇦 SA Pre-close",
                           "callback_data": "r:preclose_sa"}])
        keyboard.append([{"text": "🇸🇦 SA End of day",
                           "callback_data": "r:eod_sa"}])

    # One Cancel row per running brief — keys are short enough.
    for brief in sorted(running):
        keyboard.append([{"text": f"🛑 Cancel {brief}",
                           "callback_data": f"r:cancel:{brief}"}])

    keyboard.append(_nav_row(back_to="m:home"))
    return text, keyboard


def fire_brief(brief: str) -> str:
    """Spawn a brief in the background. Returns a toast string."""
    try:
        from webhook import app as webhook_app
    except Exception as e:
        return f"❌ Could not reach scheduler: {e}"
    spawned, reason = webhook_app._spawn_brief(brief, source="menu")
    if spawned:
        return f"🚀 {brief} running in background…"
    return f"⚠️ {brief} not started — {reason}"


def cancel_brief(brief: str) -> str:
    """Request cancellation. Same path /cancel uses."""
    try:
        from webhook import app as webhook_app
        from core import analyst as analyst_mod
    except Exception as e:
        return f"❌ Could not reach scheduler: {e}"

    # Watcher / method use distinct cancel keys per the existing
    # /cancel command logic. For /menu we only expose briefs (not
    # watcher / method), so a 1:1 mapping is enough.
    flag_was_new = analyst_mod.request_cancel(brief)
    if flag_was_new:
        return f"🛑 Cancellation requested for {brief}"
    return f"⚠️ {brief} already had a pending cancellation"


def open_run(message_id: int, toast: str = None) -> dict:
    text, kb = render_run()
    if toast:
        text = f"<i>{toast}</i>\n\n" + text
    telegram_client.edit_message_text(message_id, text,
                                      inline_keyboard=kb)
    return {"screen": "r:home"}


# ============================================================
# Watchlist (Phase C)
# ============================================================

# Soft cap for per-screen ticker buttons. Telegram tolerates up to 100
# total but rendering anything close to that is unusable on phone. Past
# this cap we render the ticker list as text and skip the per-ticker
# buttons (with a hint to use slash commands).
_WATCHLIST_BUTTON_CAP = 24


def _safe_ticker(t) -> str:
    """Coerce + uppercase. Saudi codes come back from gspread as int."""
    s = str(t or "").strip().upper()
    return s


def _watchlist_for(market: str) -> tuple:
    """Return (watch_list, focus_list) for a market. Both are clean
    str lists, deduped, in original order."""
    from core.commands import _default_watchlist
    watch = sheets.read_watchlist(
        default=_default_watchlist(market), market=market) or []
    focus_rows = sheets.read_focus(market=market) or []
    focus = [_safe_ticker(r.get("Ticker", ""))
             for r in focus_rows if r.get("Ticker") not in (None, "")]
    return [_safe_ticker(t) for t in watch], focus


def render_watchlist() -> tuple:
    """Watchlist + focus dashboard. Per-ticker buttons for remove +
    focus toggle. Adding a new ticker stays typed (free-form name)."""
    text_lines = ["<b>👁 WATCHLIST</b>", "─────────────"]
    keyboard = []

    markets_to_show = ["US"]
    for m in ACTIVE_MARKETS:
        if m not in markets_to_show:
            markets_to_show.append(m)

    total_buttons = 0

    for market in markets_to_show:
        watch, focus = _watchlist_for(market)
        flag = ("🇺🇸" if market == "US"
                else ("🇸🇦" if market == "SA" else "🌐"))
        text_lines.append("")
        text_lines.append(f"<b>{flag} {market}</b>")
        if focus:
            text_lines.append("  🎯 Focus: " +
                              " ".join(f"<code>{t}</code>" for t in focus))
        else:
            text_lines.append("  🎯 Focus: <i>(none)</i>")
        if watch:
            text_lines.append("  👁 Watch: " +
                              " ".join(f"<code>{t}</code>" for t in watch))
        else:
            text_lines.append("  👁 Watch: <i>(empty)</i>")

        # Per-ticker buttons for the watchlist (remove + promote)
        for t in watch:
            if total_buttons >= _WATCHLIST_BUTTON_CAP:
                break
            keyboard.append([
                {"text": f"🔭 Focus {t}",
                 "callback_data": f"w:foc:{t}"},
                {"text": f"🗑 Unwatch {t}",
                 "callback_data": f"w:rm:{t}"},
            ])
            total_buttons += 2

        # Per-focus buttons (unfocus only)
        for t in focus:
            if total_buttons >= _WATCHLIST_BUTTON_CAP:
                break
            keyboard.append([
                {"text": f"🚫 Unfocus {t}",
                 "callback_data": f"w:unfoc:{t}"},
            ])
            total_buttons += 1

    if total_buttons >= _WATCHLIST_BUTTON_CAP:
        text_lines.append("")
        text_lines.append("<i>List is long — only first ~24 buttons "
                          "shown. Use /unwatch /unfocus for the rest.</i>")

    text_lines.append("")
    text_lines.append("<i>To add a new ticker, send "
                      "<code>/watch SYMBOL</code> (free-form input "
                      "needed for new symbols).</i>")

    keyboard.append([{"text": "➕ Add ticker (typed)",
                       "callback_data": "w:add"}])
    keyboard.append(_nav_row(back_to="m:home"))
    return "\n".join(text_lines), keyboard


def watchlist_action(verb: str, ticker: str) -> str:
    """Apply rm / foc / unfoc to a ticker. Auto-detects market by shape
    using the same _detect_market in commands.py."""
    from core.commands import _detect_market
    ticker = _safe_ticker(ticker)
    if not ticker:
        return "⚠️ Empty ticker"
    market = _detect_market(ticker)
    if market is None:
        return f"❌ Cannot infer market for {ticker}"

    if verb == "rm":
        from core.commands import _default_watchlist
        current = sheets.read_watchlist(
            default=_default_watchlist(market), market=market) or []
        new_list = [t for t in current if _safe_ticker(t) != ticker]
        if sheets.write_watchlist(new_list, market=market):
            return f"🗑 Removed {ticker} from {market} watchlist"
        return f"❌ Sheet write failed for unwatch {ticker}"

    if verb == "foc":
        result = sheets.add_focus(ticker, market=market)
        if result.get("ok"):
            extra = (f" (dropped {result['dropped']})"
                     if result.get("dropped") else "")
            return f"🔭 Focus added: {ticker}{extra}"
        return f"❌ {result.get('error', 'focus failed')}"

    if verb == "unfoc":
        result = sheets.remove_focus(ticker)
        if result.get("ok"):
            return f"🚫 Focus removed: {ticker}"
        return f"❌ {result.get('error', 'unfocus failed')}"

    return f"Unknown watchlist verb: {verb}"


def open_watchlist(message_id: int, toast: str = None) -> dict:
    text, kb = render_watchlist()
    if toast:
        text = f"<i>{toast}</i>\n\n" + text
    telegram_client.edit_message_text(message_id, text,
                                      inline_keyboard=kb)
    return {"screen": "w:home"}


# ============================================================
# Method dashboard (Phase C)
# ============================================================

def render_method() -> tuple:
    """Live method dashboard. Mirrors /method status as a panel +
    buttons mapping to the existing /method subcommands."""
    enabled = _method_enabled()
    secret_set = bool(TRADINGVIEW_WEBHOOK_SECRET)

    today_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    cd_call = sheets.read_method_cooldown(today_str, "call") or {}
    cd_put = sheets.read_method_cooldown(today_str, "put") or {}

    try:
        from core import method_state
        snap = method_state.get_tracker().state_snapshot()
    except Exception as e:
        snap = {}
        log_event("WARN", "menu",
                  f"method dashboard tracker snapshot failed: {e}")

    def _fmt_dir(d):
        s = snap.get(d, {})
        return f"{s.get('state', '—')} ({s.get('signal_id') or '—'})"

    text_lines = [
        "<b>🎯 METHOD</b>",
        "─────────────",
        f"State: {'✅ ENABLED' if enabled else '🔕 DISABLED'}",
        "Mode: <code>webhook-driven (TradingView)</code>",
        f"Webhook secret: {'✅ set' if secret_set else '⚠️ not set'}",
        f"Ticker: <code>{METHOD_TICKER}</code>",
        f"Daily cap: <code>{METHOD_MAX_DAILY_SIGNALS}</code> per direction",
        "",
        "<b>📊 Direction state</b>",
        f"  CALL: <code>{_fmt_dir('call')}</code>",
        f"  PUT:  <code>{_fmt_dir('put')}</code>",
        "",
        "<b>📅 Today's setup count</b>",
        f"  CALL: {cd_call.get('SetupCount', 0) or 0}/"
        f"{METHOD_MAX_DAILY_SIGNALS}",
        f"  PUT:  {cd_put.get('SetupCount', 0) or 0}/"
        f"{METHOD_MAX_DAILY_SIGNALS}",
    ]

    toggle_cb = "x:off" if enabled else "x:on"
    toggle_text = ("🔕 Disable method" if enabled
                   else "✅ Enable method")
    keyboard = [
        [{"text": toggle_text,         "callback_data": toggle_cb}],
        [{"text": "🔄 Reset",          "callback_data": "x:reset"},
         {"text": "📜 History (10)",   "callback_data": "x:hist"}],
        [{"text": "🧪 Test webhook",   "callback_data": "x:test"},
         {"text": "🐞 Debug data",     "callback_data": "x:debug"}],
        _nav_row(back_to="m:home"),
    ]
    return "\n".join(text_lines), keyboard


def method_action(verb: str) -> str:
    """Apply a method action and return a toast string. The full panels
    (history, debug, test) get sent as separate Telegram messages so
    the menu screen itself stays compact."""
    from core import commands as cmd_mod
    if verb == "on":
        if sheets.write_config("METHOD_ENABLED", "true"):
            return "✅ METHOD_ENABLED → true"
        return "❌ Could not write METHOD_ENABLED"
    if verb == "off":
        if sheets.write_config("METHOD_ENABLED", "false"):
            return "🔕 METHOD_ENABLED → false"
        return "❌ Could not write METHOD_ENABLED"
    if verb == "reset":
        return cmd_mod._cmd_method_reset()
    if verb == "hist":
        return cmd_mod._cmd_method_history()
    if verb == "test":
        return cmd_mod._cmd_method_test()
    if verb == "debug":
        return cmd_mod._cmd_method_debug()
    return f"Unknown method verb: {verb}"


def open_method(message_id: int, toast: str = None) -> dict:
    text, kb = render_method()
    if toast:
        text = f"<i>{toast}</i>\n\n" + text
    telegram_client.edit_message_text(message_id, text,
                                      inline_keyboard=kb)
    return {"screen": "x:home"}


# ============================================================
# Diagnostics (Phase D)
# ============================================================

def render_diagnostics() -> tuple:
    """Tools panel — manual triggers for the same things the diagnostic
    agent + log-trim cron do automatically. Useful for incidents."""
    diag_on = _diagnostic_enabled()
    text = (
        "<b>🛠 DIAGNOSTICS</b>\n"
        "─────────────\n"
        f"Diagnostic agent: "
        f"{'✅ ENABLED' if diag_on else '🔕 DISABLED'} "
        f"(toggle in ⚙️ Toggles)\n"
        f"Log-tab cap: <code>{MAX_LOG_ROWS}</code> rows\n\n"
        "Each button below runs the same path as the corresponding "
        "slash command. Output is sent as a separate message."
    )
    keyboard = [
        [{"text": "🩺 Run /diagnose 60",  "callback_data": "d:diag"}],
        [{"text": "🗑 Trim Logs now",     "callback_data": "d:trim"}],
        [{"text": "🐞 Method debug",      "callback_data": "d:mdebug"}],
        [{"text": "🧪 Method test",       "callback_data": "d:mtest"}],
        _nav_row(back_to="m:home"),
    ]
    return text, keyboard


def diagnostics_action(verb: str) -> str:
    """Run one diagnostic action and return a message string."""
    from core import commands as cmd_mod
    if verb == "diag":
        return cmd_mod._cmd_diagnose(["60"])
    if verb == "trim":
        return cmd_mod._cmd_trim_logs()
    if verb == "mdebug":
        return cmd_mod._cmd_method_debug()
    if verb == "mtest":
        return cmd_mod._cmd_method_test()
    return f"Unknown diagnostics verb: {verb}"


def open_diagnostics(message_id: int) -> dict:
    text, kb = render_diagnostics()
    telegram_client.edit_message_text(message_id, text,
                                      inline_keyboard=kb)
    return {"screen": "d:home"}


# ============================================================
# Top-level callback dispatch — wired into the webhook handler
# ============================================================

# Phase B handles m:, t:, s: prefixes. Phase C adds r:, w:, x:.
# Phase D adds d:. Each branch in _route does one thing so /menu
# stays grep-able.
_MENU_PREFIXES = ("m", "t", "r", "w", "x", "s", "d")


def handle_callback(data: str, message_id: int) -> bool:
    """Route a /menu inline-keyboard callback. Returns True if the
    callback was a menu action (handled here), False if not (so the
    webhook dispatcher can try its own prefixes like deepdive: and
    method_cancel:)."""
    if not data or ":" not in data:
        return False
    prefix = data.split(":", 1)[0]
    if prefix not in _MENU_PREFIXES:
        return False
    try:
        _route(data, message_id)
    except Exception as e:
        log_event("ERROR", "menu", f"callback {data!r} crashed: {e}")
        try:
            telegram_client.send_message(
                f"❌ Menu action failed: <code>{e}</code>",
                reply_to_message_id=message_id,
            )
        except Exception:
            pass
    return True


def _route(data: str, message_id: int) -> None:
    # ----- Main -----
    if data == "m:home":
        open_main(message_id);   return
    if data == "m:status":
        open_status(message_id); return

    # ----- Toggles -----
    if data == "t:home":
        open_toggles(message_id); return
    if data.startswith("t:t"):
        toast = _flip_toggle(data[2:])
        open_toggles(message_id, toast=toast)
        return

    # ----- Schedule -----
    if data == "s:home":
        open_schedule(message_id); return
    if data.startswith("s:p:"):
        code = data[len("s:p:"):]
        brief = _BRIEF_CODES.get(code)
        if brief:
            open_schedule_pick_hh(message_id, brief)
        return
    if data.startswith("s:h:"):
        parts = data[len("s:h:"):].split(":")
        if len(parts) == 2:
            code, hh = parts
            brief = _BRIEF_CODES.get(code)
            if brief:
                open_schedule_pick_mm(message_id, brief, hh)
        return
    if data.startswith("s:m:"):
        parts = data[len("s:m:"):].split(":")
        if len(parts) == 3:
            code, hh, mm = parts
            brief = _BRIEF_CODES.get(code)
            if brief:
                toast = apply_schedule_change(brief, hh, mm)
                text, kb = render_schedule()
                text = f"<i>{toast}</i>\n\n" + text
                telegram_client.edit_message_text(message_id, text,
                                                  inline_keyboard=kb)
        return
    if data.startswith("s:custom:"):
        code = data[len("s:custom:"):]
        brief = _BRIEF_CODES.get(code, code)
        telegram_client.send_message(
            f"<b>Custom time for {brief}</b>\n"
            f"Send: <code>/settime {brief} HH:MM</code>\n"
            f"e.g. <code>/settime {brief} 14:42</code>"
        )
        open_schedule(message_id)
        return

    # ----- Run a brief (Phase C) -----
    if data == "r:home":
        open_run(message_id); return
    if data.startswith("r:cancel:"):
        brief = data[len("r:cancel:"):]
        toast = cancel_brief(brief)
        open_run(message_id, toast=toast)
        return
    if data.startswith("r:"):
        brief = data[len("r:"):]
        # Validate against the brief surface; reject anything we don't
        # recognize so callback typos don't fire random briefs.
        from core.commands import VALID_BRIEFS
        if brief in VALID_BRIEFS and not brief.startswith("watcher") \
                and brief != "method":
            toast = fire_brief(brief)
            open_run(message_id, toast=toast)
        else:
            open_run(message_id, toast=f"Unknown brief: {brief}")
        return

    # ----- Watchlist (Phase C) -----
    if data == "w:home":
        open_watchlist(message_id); return
    if data == "w:add":
        telegram_client.send_message(
            "<b>➕ Add ticker</b>\n"
            "Send: <code>/watch SYMBOL</code>\n"
            "e.g. <code>/watch NVDA</code> or "
            "<code>/watch 2222</code> (Aramco)"
        )
        open_watchlist(message_id)
        return
    if data.startswith("w:rm:"):
        toast = watchlist_action("rm", data[len("w:rm:"):])
        open_watchlist(message_id, toast=toast); return
    if data.startswith("w:foc:"):
        toast = watchlist_action("foc", data[len("w:foc:"):])
        open_watchlist(message_id, toast=toast); return
    if data.startswith("w:unfoc:"):
        toast = watchlist_action("unfoc", data[len("w:unfoc:"):])
        open_watchlist(message_id, toast=toast); return

    # ----- Method (Phase C) -----
    if data == "x:home":
        open_method(message_id); return
    if data in ("x:on", "x:off"):
        toast = method_action(data[2:])
        open_method(message_id, toast=toast); return
    if data == "x:reset":
        toast = method_action("reset")
        # Reset returns a multi-line payload — render as a separate
        # message rather than cramming into the menu header.
        telegram_client.send_message(toast)
        open_method(message_id); return
    if data == "x:hist":
        text = method_action("hist")
        telegram_client.send_message(text)
        open_method(message_id); return
    if data == "x:test":
        text = method_action("test")
        telegram_client.send_message(text)
        open_method(message_id); return
    if data == "x:debug":
        text = method_action("debug")
        telegram_client.send_message(text)
        open_method(message_id); return

    # ----- Diagnostics (Phase D) -----
    if data == "d:home":
        open_diagnostics(message_id); return
    if data in ("d:diag", "d:trim", "d:mdebug", "d:mtest"):
        verb = data[2:]
        result = diagnostics_action(verb)
        telegram_client.send_message(result)
        open_diagnostics(message_id)
        return

    log_event("WARN", "menu", f"Unknown callback path: {data}")
