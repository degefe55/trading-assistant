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

    # ----- Phase C + D placeholders (filled in next two phases) -----
    if data.startswith(("r:", "w:", "x:", "d:")):
        log_event("INFO", "menu",
                  f"callback not yet implemented in this phase: {data}")
        telegram_client.send_message(
            f"<i>This screen lands in a later phase: <code>{data}</code></i>"
        )
        return

    log_event("WARN", "menu", f"Unknown callback path: {data}")
