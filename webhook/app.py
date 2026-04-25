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
from config import TELEGRAM_CHAT_ID, KSA_TZ
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


@app.route("/reschedule", methods=["POST", "GET"])
def reschedule_endpoint():
    if not RUN_TOKEN:
        return jsonify({"ok": False, "error": "RUN_TOKEN not configured"}), 503
    if not _check_token():
        return jsonify({"ok": False, "error": "invalid token"}), 401
    rebuild_schedule()
    return jsonify({"ok": True, "active_times_ksa": _active_times}), 200


@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    """Telegram commands. MUST return fast — long work goes to background."""
    try:
        update = request.get_json()
        if not update:
            return jsonify({"ok": True}), 200

        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id != str(TELEGRAM_CHAT_ID):
            return jsonify({"ok": True, "ignored": "wrong chat"}), 200

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return jsonify({"ok": True, "ignored": "not a command"}), 200

        # Sheet init can be slow — keep it in the request only when needed
        try:
            sheets.initialize_sheet()
        except Exception as e:
            log_event("WARN", "webhook", f"Sheet init warn: {e}")

        reply = commands._dispatch(text)
        if reply:
            telegram_client.send_message(reply)

        return jsonify({"ok": True, "processed": text.split()[0]}), 200

    except Exception as e:
        log_event("ERROR", "webhook", f"Webhook error: {e}")
        try:
            telegram_client.send_error_alert(f"Webhook error: {e}")
        except Exception:
            pass
        return jsonify({"ok": False, "error": str(e)}), 500


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
        if brief_type == "premarket":
            bot_main.run_premarket_brief()
        elif brief_type == "midsession":
            bot_main.run_midsession_check()
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


def rebuild_schedule():
    global _active_times
    with _scheduler_lock:
        schedule.clear()
        times = _load_times_from_config()
        for brief, hhmm in times.items():
            schedule.every().day.at(hhmm, KSA_TZ_STR).do(
                _scheduled_runner, brief
            ).tag(brief)
        _active_times = times
        log_event("INFO", "scheduler", f"Rebuilt schedule: {times}")


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
