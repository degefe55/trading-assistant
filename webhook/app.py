"""
Telegram webhook + in-process scheduler for Railway.

Replaces GitHub Actions scheduled briefs (which were arriving 2+ hours
late). This in-process scheduler fires within seconds of the target time.

CRITICAL: HTTP handlers MUST return fast (<2 sec). Briefs run for 60-90 sec
and Telegram retries webhook deliveries that don't return quickly, which
caused multiple parallel briefs and token burn. So /run, /telegram, and
the scheduler all dispatch briefs to a background thread via _spawn_brief().

A per-brief-type lock prevents two of the same brief running concurrently.

Scheduled times come from the Google Sheet's Config tab so they survive
redeploys and can be edited from Telegram via /settime. Defaults:

    BRIEF_TIME_PREMARKET   = 15:30   (KSA, Mon-Fri)
    BRIEF_TIME_MIDSESSION  = 19:30
    BRIEF_TIME_PRECLOSE    = 22:30
    BRIEF_TIME_EOD         = 23:00

Public surface:

    GET  /                 health check
    GET  /status           scheduler diagnostic (next/last runs, current times)
    POST /telegram         Telegram webhook (commands)
    POST /run/<brief>      manual brief trigger, ?token=$RUN_TOKEN
    POST /reschedule       reload times from Config tab, ?token=$RUN_TOKEN

Env vars:
    RUN_TOKEN           secret for /run and /reschedule
    PUBLIC_URL          this service's public URL, used for self-ping
    BRIEF_TIMEOUT_SEC   max seconds a brief may run (default 300)
"""
import os
import sys
import threading
import time
import traceback
from datetime import datetime

import requests
import schedule
from flask import Flask, request, jsonify

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("MOCK_MODE", "false")
os.environ.setdefault("ACTIVE_MARKETS", "US")

from core import commands, telegram_client, sheets
from core.logger import log_event, get_log_buffer
from config import (TELEGRAM_CHAT_ID, KSA_TZ,
                    WATCHER_PRICE_INTERVAL_MIN, WATCHER_NEWS_INTERVAL_MIN)
import main as bot_main


app = Flask(__name__)

RUN_TOKEN = os.environ.get("RUN_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
BRIEF_TIMEOUT_SEC = int(os.environ.get("BRIEF_TIMEOUT_SEC", "300"))
SELF_PING_INTERVAL_SEC = 5 * 60
SCHEDULE_TICK_SEC = 30
KSA_TZ_STR = "Asia/Riyadh"

DEFAULT_TIMES = {
    "premarket":  "15:30",
    "midsession": "19:30",
    "preclose":   "22:30",
    "eod":        "23:00",
}
CONFIG_KEY_PREFIX = "BRIEF_TIME_"

_last_runs = {}              # {brief: iso ksa timestamp of last completion}
_last_started = {}           # {brief: iso ksa timestamp of last start}
_active_times = {}
_scheduler_started = False
_scheduler_lock = threading.Lock()

# Per-brief running locks: prevent two of the same brief running at once.
# Each entry is a threading.Lock acquired non-blocking before _execute_brief.
_brief_locks = {b: threading.Lock() for b in DEFAULT_TIMES.keys()}
_currently_running = set()   # set of brief names currently executing
_running_lock = threading.Lock()

# Phase D.5 — watcher state. Single shared lock across price + news ticks
# so they can't double-run; on collision the loser logs "tick skipped"
# and exits (no queueing — this is the explicit design choice).
_watcher_lock = threading.Lock()
_last_watcher_runs = {}      # {"price"|"news": iso ksa of last completion}
_last_watcher_started = {}   # {"price"|"news": iso ksa of last start}
_active_watcher_intervals = {}  # {"price": min, "news": min}


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "trading-bot-webhook"})


@app.route("/status", methods=["GET"])
def status():
    jobs = []
    for j in schedule.get_jobs():
        jobs.append({
            "tag": list(j.tags)[0] if j.tags else "?",
            "next_run_utc": j.next_run.isoformat() if j.next_run else None,
        })
    with _running_lock:
        running = sorted(_currently_running)
    return jsonify({
        "status": "ok",
        "scheduler_started": _scheduler_started,
        "active_times_ksa": _active_times,
        "jobs": jobs,
        "last_started_ksa": _last_started,
        "last_completed_ksa": _last_runs,
        "currently_running": running,
        "watcher_intervals_min": _active_watcher_intervals,
        "watcher_last_started_ksa": _last_watcher_started,
        "watcher_last_completed_ksa": _last_watcher_runs,
        "now_utc": datetime.utcnow().isoformat(),
        "now_ksa": datetime.now(KSA_TZ).isoformat(),
    })


def _check_token() -> bool:
    if not RUN_TOKEN:
        return False
    token = request.args.get("token") or request.headers.get("X-Run-Token", "")
    return token == RUN_TOKEN


@app.route("/run/<brief_type>", methods=["POST", "GET"])
def manual_run(brief_type: str):
    """Spawn a brief in the background, return immediately."""
    if not RUN_TOKEN:
        return jsonify({"ok": False, "error": "RUN_TOKEN not configured"}), 503
    if not _check_token():
        return jsonify({"ok": False, "error": "invalid token"}), 401
    if brief_type not in DEFAULT_TIMES:
        return jsonify({"ok": False, "error": f"unknown brief: {brief_type}"}), 400

    spawned, reason = _spawn_brief(brief_type, source="http")
    if spawned:
        return jsonify({"ok": True, "spawned": brief_type}), 202
    return jsonify({"ok": False, "error": reason, "brief": brief_type}), 409


@app.route("/run/watcher_price", methods=["POST", "GET"])
def manual_run_watcher_price():
    return manual_run_watcher("price")


@app.route("/run/watcher_news", methods=["POST", "GET"])
def manual_run_watcher_news():
    return manual_run_watcher("news")


@app.route("/run/watcher/<mode>", methods=["POST", "GET"])
def manual_run_watcher(mode: str):
    """Manually fire one watcher tick. Same skip-if-busy semantics as
    the scheduled job — if the lock is held, returns 409."""
    if not RUN_TOKEN:
        return jsonify({"ok": False, "error": "RUN_TOKEN not configured"}), 503
    if not _check_token():
        return jsonify({"ok": False, "error": "invalid token"}), 401
    if mode not in ("price", "news"):
        return jsonify({"ok": False, "error": f"unknown mode: {mode}"}), 400

    spawned, reason = _spawn_watcher_tick(mode, source="http")
    if spawned:
        return jsonify({"ok": True, "spawned": f"watcher_{mode}"}), 202
    return jsonify({"ok": False, "error": reason,
                    "mode": mode}), 409


@app.route("/cancel/watcher", methods=["POST", "GET"])
def cancel_watcher_endpoint():
    """Request cancellation of an in-flight watcher run. Cooperative:
    in-flight Sonnet finishes; further tickers are skipped."""
    if not RUN_TOKEN:
        return jsonify({"ok": False, "error": "RUN_TOKEN not configured"}), 503
    if not _check_token():
        return jsonify({"ok": False, "error": "invalid token"}), 401

    if _watcher_lock.acquire(blocking=False):
        # Lock was free → nothing running
        _watcher_lock.release()
        return jsonify({"ok": False, "error": "not running"}), 409

    from core import analyst as analyst_mod
    flag_was_new = analyst_mod.request_cancel("watcher")
    log_event("INFO", "watcher",
              f"Cancellation requested for watcher via HTTP "
              f"(new={flag_was_new})")
    return jsonify({"ok": True, "was_already_pending": not flag_was_new}), 202


@app.route("/reschedule", methods=["POST", "GET"])
def reschedule_endpoint():
    if not RUN_TOKEN:
        return jsonify({"ok": False, "error": "RUN_TOKEN not configured"}), 503
    if not _check_token():
        return jsonify({"ok": False, "error": "invalid token"}), 401
    rebuild_schedule()
    return jsonify({"ok": True, "active_times_ksa": _active_times}), 200


@app.route("/cancel/<brief_type>", methods=["POST", "GET"])
def cancel_endpoint(brief_type: str):
    """Request cancellation of a running brief. Cooperative: the in-flight
    Claude call finishes; subsequent tickers are skipped."""
    if not RUN_TOKEN:
        return jsonify({"ok": False, "error": "RUN_TOKEN not configured"}), 503
    if not _check_token():
        return jsonify({"ok": False, "error": "invalid token"}), 401
    if brief_type not in DEFAULT_TIMES:
        return jsonify({"ok": False, "error": f"unknown brief: {brief_type}"}), 400

    with _running_lock:
        is_running = brief_type in _currently_running

    if not is_running:
        return jsonify({"ok": False, "error": "not running",
                        "brief": brief_type}), 409

    # Lazy import: analyst is part of the bot package, not the webhook
    from core import analyst as analyst_mod
    flag_was_new = analyst_mod.request_cancel(brief_type)
    log_event("INFO", "scheduler",
              f"Cancellation requested for {brief_type} via HTTP "
              f"(new={flag_was_new})")
    return jsonify({
        "ok": True,
        "brief": brief_type,
        "was_already_pending": not flag_was_new,
    }), 202


@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    """Telegram updates. MUST return fast — long work goes to background.

    Handles three update types:
      1. /command messages (existing behavior)
      2. Threaded replies to a previous brief — looked up via MessageMap
         and routed to the follow-up handler
      3. callback_query (button taps) — for deep-dive on tap (Q2). Only
         scaffolded here; full handler lands when Q2 is built.
    """
    try:
        update = request.get_json()
        if not update:
            return jsonify({"ok": True}), 200

        # --- Case 3: button-tap callback ---
        cb = update.get("callback_query")
        if cb:
            return _handle_callback_query(cb)

        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(TELEGRAM_CHAT_ID):
            return jsonify({"ok": True, "ignored": "wrong chat"}), 200

        text = (msg.get("text") or "").strip()
        if not text:
            return jsonify({"ok": True, "ignored": "no text"}), 200

        # Sheet init can be slow — keep it in the request only when needed
        try:
            sheets.initialize_sheet()
        except Exception as e:
            log_event("WARN", "webhook", f"Sheet init warn: {e}")

        # --- Case 1: /command ---
        if text.startswith("/"):
            reply = commands._dispatch(text)
            if reply:
                telegram_client.send_message(reply)
            return jsonify({"ok": True, "processed": text.split()[0]}), 200

        # --- Case 2: threaded reply to a brief ---
        reply_to = msg.get("reply_to_message")
        if reply_to:
            parent_id = reply_to.get("message_id")
            return _handle_threaded_reply(parent_id, text,
                                          msg.get("message_id"),
                                          chat_id=chat_id)

        # Otherwise: free-text not in reply context, ignore
        return jsonify({"ok": True, "ignored": "non-command, non-reply"}), 200

    except Exception as e:
        log_event("ERROR", "webhook", f"Webhook error: {e}")
        try:
            telegram_client.send_error_alert(f"Webhook error: {e}")
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500


def _handle_threaded_reply(parent_message_id: int, question_text: str,
                           reply_message_id: int, chat_id: str = None):
    """User replied to one of our brief messages with a question.
    Look up the parent's RecIDs and dispatch to followup."""
    if not parent_message_id:
        return jsonify({"ok": True, "ignored": "no parent id"}), 200

    rec_ids = sheets.get_recids_for_message(parent_message_id)
    if not rec_ids:
        # Not a brief we tracked. Could be a /command's reply or an old brief
        # before MessageMap existed. Tell the user politely.
        telegram_client.send_message(
            "🤔 I don't have context for that message. "
            "If you want to ask about a recent recommendation, use:\n"
            "<code>/ask RecID your question</code>",
            reply_to_message_id=reply_message_id,
        )
        return jsonify({"ok": True, "no_context": True}), 200

    # If multiple RecIDs in the brief, use the first one. (User can
    # be more specific via /ask if they want a different ticker.)
    rec_id = rec_ids[0]
    log_event("INFO", "webhook",
              f"Threaded reply to msg {parent_message_id} -> rec {rec_id}")

    from core import followup
    result = followup.answer_followup(rec_id, question_text,
                                       chat_id=chat_id or str(TELEGRAM_CHAT_ID))
    if not result.get("ok"):
        telegram_client.send_message(
            f"❌ {result.get('error', 'Unknown error')}",
            reply_to_message_id=reply_message_id,
        )
        return jsonify({"ok": False, "error": result.get("error")}), 200

    note = ""
    if len(rec_ids) > 1:
        note = (f"\n<i>(answering about {result['ticker']}; "
                f"this brief covered {len(rec_ids)} tickers — "
                f"use /ask RecID for others)</i>")

    telegram_client.send_message(
        f"💬 <b>Re:</b> {result['ticker']} ({result['action']})\n"
        f"─────────────\n"
        f"{result['answer']}{note}\n\n"
        f"<i>💰 ${result['cost_usd']:.4f}</i>",
        reply_to_message_id=reply_message_id,
    )
    return jsonify({"ok": True, "answered": rec_id}), 200


def _handle_callback_query(cb: dict):
    """Inline-button tap on a brief. Routes by callback_data prefix.

    Currently supported:
      deepdive:<RecID>  → fetch stored deep dive from Recommendations,
                          render and send as a threaded reply.

    Always acknowledges the tap (Telegram requires this so the user's
    button stops spinning).
    """
    cb_id = cb.get("id", "")
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    parent_msg_id = msg.get("message_id")
    cb_chat_id = str((msg.get("chat") or {}).get("id", ""))

    log_event("INFO", "webhook", f"Callback received: data={data}")

    # Security: ignore button taps from any chat that isn't ours
    if cb_chat_id and cb_chat_id != str(TELEGRAM_CHAT_ID):
        telegram_client.answer_callback_query(cb_id, text="Not authorized")
        return jsonify({"ok": True, "ignored": "wrong chat"}), 200

    # Acknowledge fast — Telegram times out the spinner if we're slow
    telegram_client.answer_callback_query(cb_id, text="Loading deep dive…")

    # Route by prefix
    if data.startswith("deepdive:"):
        rec_id = data[len("deepdive:"):].strip()
        return _send_deep_dive(rec_id, parent_msg_id)

    # Unknown callback — be honest, don't pretend it worked
    telegram_client.send_message(
        f"⚠️ Unknown button action: <code>{data[:60]}</code>",
        reply_to_message_id=parent_msg_id,
    )
    return jsonify({"ok": True, "unknown_callback": data[:30]}), 200


def _send_deep_dive(rec_id: str, parent_msg_id):
    """Fetch a stored recommendation and render its deep_dive section.

    No new Claude call — the deep dive was generated when the brief ran;
    we're just retrieving it from the Recommendations sheet. This is the
    'simpler middle path' choice: instant tap response, $0 cost, but the
    deep dive is as-of-brief-time (not refreshed with current price/news).
    """
    if not rec_id or "-" not in rec_id:
        telegram_client.send_message(
            f"⚠️ Bad RecID in button: <code>{rec_id[:60]}</code>",
            reply_to_message_id=parent_msg_id,
        )
        return jsonify({"ok": False, "error": "bad rec_id"}), 200

    rec = sheets.read_recommendation(rec_id)
    if not rec:
        telegram_client.send_message(
            f"⚠️ Couldn't find <code>{rec_id}</code> in Recommendations.\n"
            f"<i>Older briefs (before this deploy) don't have stored deep "
            f"dives.</i>",
            reply_to_message_id=parent_msg_id,
        )
        return jsonify({"ok": False, "error": "not found"}), 200

    # Try to extract the structured deep_dive from RawJSON (preferred —
    # has technical/news/macro/reasoning split). Fall back to flat
    # Reasoning column if RawJSON is missing or unparsable.
    import json
    raw_json = rec.get("RawJSON", "") or ""
    deep = {}
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            deep = parsed.get("deep_dive", {}) or {}
        except (json.JSONDecodeError, TypeError, ValueError):
            deep = {}

    ticker = rec.get("Ticker", "?")
    action = rec.get("Action", "?")
    when = f"{rec.get('Date', '')} {rec.get('Time_KSA', '')}"

    lines = [
        f"📖 <b>Deep dive: {ticker}</b> ({action})",
        f"<i>From {rec.get('BriefType', '?')} · {when} KSA</i>",
        "─────────────",
    ]

    if deep.get("technical"):
        lines.append(f"• <b>Chart:</b> {deep['technical']}")
    if deep.get("news"):
        lines.append(f"• <b>News:</b> {deep['news']}")
    if deep.get("macro"):
        lines.append(f"• <b>Macro:</b> {deep['macro']}")
    if deep.get("reasoning"):
        lines.append(f"• <b>Reasoning:</b> {deep['reasoning']}")
    elif rec.get("Reasoning"):
        # Fallback: use the flat Reasoning column
        lines.append(f"• <b>Reasoning:</b> {rec['Reasoning']}")

    sl = deep.get("stop_loss") or rec.get("StopLoss")
    tgt = deep.get("target") or rec.get("Target")
    if sl or tgt:
        bits = []
        if sl: bits.append(f"Stop {sl}")
        if tgt: bits.append(f"Target {tgt}")
        lines.append(f"• <b>Levels:</b> {' · '.join(bits)}")

    warnings = deep.get("warnings") or []
    if warnings:
        lines.append(f"• <b>⚠️ Warnings:</b> {'; '.join(warnings)}")

    one_line = rec.get("OneLinePlan", "")
    if one_line:
        lines.append("")
        lines.append(f"<i>{one_line}</i>")

    if len(lines) <= 3:
        # If we have a row but no deep-dive content at all, say so honestly
        lines.append("<i>No deep-dive content stored for this rec.</i>")

    lines.append("")
    lines.append(
        f"<i>💡 Reply to this for follow-up · "
        f"or <code>/ask {rec_id} ...</code></i>"
    )

    telegram_client.send_message(
        "\n".join(lines),
        reply_to_message_id=parent_msg_id,
    )
    log_event("INFO", "webhook", f"Sent deep dive for {rec_id}")
    return jsonify({"ok": True, "rec_id": rec_id}), 200


# ---------------------------------------------------------------------------
# Brief execution — async with dedup
# ---------------------------------------------------------------------------

def _spawn_brief(brief_type: str, source: str) -> tuple:
    """Spawn brief in a daemon thread. Returns (spawned: bool, reason: str).

    Spawn is REJECTED if the same brief is already running. Different briefs
    can run in parallel.
    """
    if brief_type not in DEFAULT_TIMES:
        return False, f"unknown brief: {brief_type}"

    lock = _brief_locks[brief_type]
    if not lock.acquire(blocking=False):
        log_event("INFO", "scheduler",
                  f"Skipping {brief_type} ({source}): already running")
        return False, "already running"

    # We hold the lock; the thread releases it when done.
    with _running_lock:
        _currently_running.add(brief_type)
    _last_started[brief_type] = datetime.now(KSA_TZ).isoformat()

    def _runner():
        try:
            _execute_brief(brief_type, source=source)
        finally:
            with _running_lock:
                _currently_running.discard(brief_type)
            lock.release()

    t = threading.Thread(target=_runner, daemon=True,
                         name=f"brief-{brief_type}")
    t.start()
    log_event("INFO", "scheduler", f"Spawned {brief_type} (source={source})")
    return True, "spawned"


def _execute_brief(brief_type: str, source: str = "schedule"):
    """Run a single brief with a hard timeout via watchdog thread."""
    log_event("INFO", "scheduler",
              f"Firing brief={brief_type} source={source}")

    # Watchdog: if the brief runs longer than BRIEF_TIMEOUT_SEC, log and alert.
    # We can't actually kill the worker thread cleanly in CPython, but we can
    # detect runaway briefs and surface them. The Telegram message itself
    # serves as the natural "I'm done" signal.
    timed_out = {"flag": False}

    def _watchdog():
        time.sleep(BRIEF_TIMEOUT_SEC)
        if not timed_out["flag"]:  # if main thread didn't clear, we're stuck
            log_event("ERROR", "scheduler",
                      f"{brief_type} exceeded {BRIEF_TIMEOUT_SEC}s timeout")
            try:
                telegram_client.send_error_alert(
                    f"⚠️ <b>{brief_type}</b> brief exceeded "
                    f"{BRIEF_TIMEOUT_SEC}s and may be stuck. "
                    f"Check Railway logs."
                )
            except Exception:
                pass

    wd = threading.Thread(target=_watchdog, daemon=True,
                          name=f"watchdog-{brief_type}")
    wd.start()

    try:
        sheets.initialize_sheet()
        # Anything that isn't a scheduled cron tick is a "manual" trigger:
        # /run from Telegram, /run/<brief> HTTP, or any other ad-hoc source.
        manual = (source != "schedule")
        if brief_type == "premarket":
            bot_main.run_premarket_brief()
        elif brief_type == "midsession":
            bot_main.run_midsession_check(manual_trigger=manual)
        elif brief_type == "preclose":
            bot_main.run_preclose_verdict()
        elif brief_type == "eod":
            bot_main.run_eod_summary()
        else:
            log_event("ERROR", "scheduler", f"Unknown brief: {brief_type}")
            return
        _last_runs[brief_type] = datetime.now(KSA_TZ).isoformat()
        log_event("INFO", "scheduler", f"Done brief={brief_type}")

    except Exception as e:
        err = f"Brief {brief_type} crashed: {e}\n{traceback.format_exc()}"
        log_event("ERROR", "scheduler", err)
        try:
            telegram_client.send_error_alert(err[:1500])
        except Exception:
            pass
    finally:
        timed_out["flag"] = True  # tell watchdog we finished in time
        try:
            buf = get_log_buffer()
            if buf:
                sheets.append_logs(buf)
        except Exception as e:
            print(f"Could not flush logs: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def _is_weekday_ksa() -> bool:
    return datetime.now(KSA_TZ).weekday() < 5


def _scheduled_runner(brief_type: str):
    if not _is_weekday_ksa():
        log_event("INFO", "scheduler",
                  f"Skipping {brief_type}: weekend in KSA")
        return
    _spawn_brief(brief_type, source="schedule")


# ---------------------------------------------------------------------------
# Watcher tick scheduling
# ---------------------------------------------------------------------------

def _spawn_watcher_tick(mode: str, source: str) -> tuple:
    """Try to acquire the shared watcher lock and run one tick in a
    background thread. If the lock is already held, log "tick skipped"
    and return False — DO NOT QUEUE.

    Returns (spawned: bool, reason: str).
    """
    if mode not in ("price", "news"):
        return False, f"unknown mode: {mode}"

    if not _watcher_lock.acquire(blocking=False):
        # The shared lock means a previous price OR news tick is still
        # running. Per spec: skip this tick, log it, do not queue.
        log_event("INFO", "watcher",
                  f"watcher tick skipped (mode={mode}, source={source}): "
                  f"lock held")
        return False, "lock held"

    with _running_lock:
        _currently_running.add(f"watcher_{mode}")
    _last_watcher_started[mode] = datetime.now(KSA_TZ).isoformat()

    def _runner():
        try:
            from core import watcher
            try:
                watcher.run_watcher_check(mode)
            except Exception as e:
                err = f"watcher tick crashed (mode={mode}): {e}\n{traceback.format_exc()}"
                log_event("ERROR", "watcher", err)
                try:
                    telegram_client.send_error_alert(err[:1500])
                except Exception:
                    pass
            _last_watcher_runs[mode] = datetime.now(KSA_TZ).isoformat()
        finally:
            with _running_lock:
                _currently_running.discard(f"watcher_{mode}")
            _watcher_lock.release()
            try:
                buf = get_log_buffer()
                if buf:
                    sheets.append_logs(buf)
            except Exception as e:
                print(f"watcher log flush failed: {e}", file=sys.stderr)

    t = threading.Thread(target=_runner, daemon=True,
                         name=f"watcher-{mode}")
    t.start()
    return True, "spawned"


def _scheduled_watcher_runner(mode: str):
    """Cron entry point — gate at the boundary, then spawn."""
    if not _is_weekday_ksa():
        log_event("INFO", "watcher",
                  f"watcher tick skipped (mode={mode}): weekend in KSA")
        return
    _spawn_watcher_tick(mode, source="schedule")


def _self_ping():
    if not PUBLIC_URL:
        return
    try:
        requests.get(f"{PUBLIC_URL}/", timeout=10)
    except Exception as e:
        print(f"Self-ping failed: {e}", file=sys.stderr)


def _scheduler_loop():
    log_event("INFO", "scheduler", "Scheduler thread started")
    last_ping = time.time()
    while True:
        try:
            schedule.run_pending()
            if time.time() - last_ping >= SELF_PING_INTERVAL_SEC:
                _self_ping()
                last_ping = time.time()
        except Exception as e:
            print(f"Scheduler tick error: {e}", file=sys.stderr)
        time.sleep(SCHEDULE_TICK_SEC)


def _validate_hhmm(s: str) -> bool:
    """Return True if string is exactly HH:MM (zero-padded), valid 24h time."""
    try:
        s = s.strip()
        if len(s) != 5 or s[2] != ":":
            return False
        h, m = int(s[0:2]), int(s[3:5])
        return 0 <= h < 24 and 0 <= m < 60
    except (ValueError, AttributeError):
        return False


def _load_times_from_config() -> dict:
    times = dict(DEFAULT_TIMES)
    try:
        cfg = sheets.read_config()
        for brief in DEFAULT_TIMES.keys():
            key = f"{CONFIG_KEY_PREFIX}{brief.upper()}"
            val = cfg.get(key)
            if val and _validate_hhmm(str(val)):
                times[brief] = str(val).strip()
    except Exception as e:
        log_event("WARN", "scheduler",
                  f"Failed to read config times, using defaults: {e}")
    return times


def _load_watcher_intervals_from_config() -> dict:
    """Read WATCHER_PRICE_INTERVAL_MIN and WATCHER_NEWS_INTERVAL_MIN from
    the Config tab. Sheet wins; Python consts (from config.py) are the
    fallback. Both values clamped to [1, 1440] to keep the scheduler sane.
    """
    intervals = {
        "price": WATCHER_PRICE_INTERVAL_MIN,
        "news": WATCHER_NEWS_INTERVAL_MIN,
    }
    try:
        cfg = sheets.read_config() or {}
        for mode, key in (("price", "WATCHER_PRICE_INTERVAL_MIN"),
                          ("news", "WATCHER_NEWS_INTERVAL_MIN")):
            raw = cfg.get(key)
            if raw is None or str(raw).strip() == "":
                continue
            try:
                n = int(str(raw).strip())
            except (ValueError, TypeError):
                log_event("WARN", "watcher",
                          f"{key} not int ({raw!r}); using {intervals[mode]}")
                continue
            if 1 <= n <= 1440:
                intervals[mode] = n
            else:
                log_event("WARN", "watcher",
                          f"{key}={n} out of range [1,1440]; "
                          f"using {intervals[mode]}")
    except Exception as e:
        log_event("WARN", "watcher",
                  f"Failed to read watcher intervals, using defaults: {e}")
    return intervals


def rebuild_schedule():
    global _active_times, _active_watcher_intervals
    with _scheduler_lock:
        schedule.clear()
        # Brief jobs (existing)
        times = _load_times_from_config()
        for brief, hhmm in times.items():
            schedule.every().day.at(hhmm, KSA_TZ_STR).do(
                _scheduled_runner, brief
            ).tag(brief)
        _active_times = times

        # Watcher jobs (Phase D.5). Two scheduled jobs as the spec
        # requires; both share _watcher_lock so they can never overlap
        # — on collision the later one logs "tick skipped" and exits.
        # The market-hours / pause / enabled gates live inside
        # watcher.run_watcher_check, not here, so the schedule itself
        # stays simple and the gating decisions are observable in Logs.
        intervals = _load_watcher_intervals_from_config()
        schedule.every(intervals["price"]).minutes.do(
            _scheduled_watcher_runner, "price"
        ).tag("watcher_price")
        schedule.every(intervals["news"]).minutes.do(
            _scheduled_watcher_runner, "news"
        ).tag("watcher_news")
        _active_watcher_intervals = intervals

        log_event("INFO", "scheduler",
                  f"Rebuilt schedule: briefs={times}, "
                  f"watcher={intervals}")


def start_scheduler():
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    rebuild_schedule()
    t = threading.Thread(target=_scheduler_loop, daemon=True,
                         name="bot-scheduler")
    t.start()


# Boot scheduler at import time so gunicorn launches it.
start_scheduler()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
