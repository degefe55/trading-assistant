"""
Telegram webhook + in-process scheduler for Railway.

Replaces GitHub Actions scheduled briefs (which were arriving 2+ hours
late due to GitHub cron queue delays). This in-process scheduler fires
within seconds of the target time.

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
    RUN_TOKEN   secret for /run and /reschedule
    PUBLIC_URL  this service's public URL, used for self-ping
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
SELF_PING_INTERVAL_SEC = 5 * 60
SCHEDULE_TICK_SEC = 30
KSA_TZ_STR = "Asia/Riyadh"

# Default brief times in KSA. Overridden by Config tab values if present.
DEFAULT_TIMES = {
    "premarket":  "15:30",
    "midsession": "19:30",
    "preclose":   "22:30",
    "eod":        "23:00",
}
CONFIG_KEY_PREFIX = "BRIEF_TIME_"  # e.g. BRIEF_TIME_PREMARKET

_last_runs = {}
_active_times = {}
_scheduler_started = False
_scheduler_lock = threading.Lock()


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
    return jsonify({
        "status": "ok",
        "scheduler_started": _scheduler_started,
        "active_times_ksa": _active_times,
        "jobs": jobs,
        "last_runs_ksa": _last_runs,
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
    if not RUN_TOKEN:
        return jsonify({"ok": False, "error": "RUN_TOKEN not configured"}), 503
    if not _check_token():
        return jsonify({"ok": False, "error": "invalid token"}), 401
    if brief_type not in ("premarket", "midsession", "preclose", "eod"):
        return jsonify({"ok": False, "error": f"unknown brief: {brief_type}"}), 400

    log_event("INFO", "scheduler", f"Manual /run/{brief_type} via HTTP")
    try:
        _execute_brief(brief_type, source="http")
        return jsonify({"ok": True, "ran": brief_type}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/reschedule", methods=["POST", "GET"])
def reschedule_endpoint():
    """Reload times from Config tab and rebuild jobs."""
    if not RUN_TOKEN:
        return jsonify({"ok": False, "error": "RUN_TOKEN not configured"}), 503
    if not _check_token():
        return jsonify({"ok": False, "error": "invalid token"}), 401
    rebuild_schedule()
    return jsonify({"ok": True, "active_times_ksa": _active_times}), 200


@app.route("/telegram", methods=["POST"])
def telegram_webhook():
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
# Scheduler
# ---------------------------------------------------------------------------

def _is_weekday_ksa() -> bool:
    """KSA Mon-Fri only. weekday(): Mon=0..Sun=6."""
    return datetime.now(KSA_TZ).weekday() < 5


def _execute_brief(brief_type: str, source: str = "schedule"):
    started = datetime.now(KSA_TZ)
    log_event("INFO", "scheduler", f"Firing brief={brief_type} source={source}")
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
        _last_runs[brief_type] = started.isoformat()
        log_event("INFO", "scheduler", f"Done brief={brief_type}")
    except Exception as e:
        err = f"Scheduler crashed running {brief_type}: {e}\n{traceback.format_exc()}"
        log_event("ERROR", "scheduler", err)
        try:
            telegram_client.send_error_alert(err[:1500])
        except Exception:
            pass
    finally:
        try:
            buf = get_log_buffer()
            if buf:
                sheets.append_logs(buf)
        except Exception as e:
            print(f"Could not flush logs: {e}", file=sys.stderr)


def _scheduled_runner(brief_type: str):
    if not _is_weekday_ksa():
        log_event("INFO", "scheduler", f"Skipping {brief_type}: weekend in KSA")
        return
    _execute_brief(brief_type, source="schedule")


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
    """Return True if string is exactly HH:MM (zero-padded) with valid 24h time."""
    try:
        s = s.strip()
        if len(s) != 5 or s[2] != ":":
            return False
        h, m = int(s[0:2]), int(s[3:5])
        return 0 <= h < 24 and 0 <= m < 60
    except (ValueError, AttributeError):
        return False


def _load_times_from_config() -> dict:
    """Read brief times from sheet Config tab. Falls back to defaults."""
    times = dict(DEFAULT_TIMES)
    try:
        cfg = sheets.read_config()
        for brief in DEFAULT_TIMES.keys():
            key = f"{CONFIG_KEY_PREFIX}{brief.upper()}"
            val = cfg.get(key)
            if val and _validate_hhmm(str(val)):
                times[brief] = str(val).strip()
    except Exception as e:
        log_event("WARN", "scheduler", f"Failed to read config times, using defaults: {e}")
    return times


def rebuild_schedule():
    """Clear and re-register all jobs with current Config-tab times.

    Called: once at startup, and whenever times change via /settime.
    Safe to call from any thread.
    """
    global _active_times
    with _scheduler_lock:
        schedule.clear()
        times = _load_times_from_config()
        for brief, hhmm in times.items():
            schedule.every().day.at(hhmm, KSA_TZ_STR).do(
                _scheduled_runner, brief
            ).tag(brief)
        _active_times = times
        log_event("INFO", "scheduler",
                  f"Rebuilt schedule: {times}")


def start_scheduler():
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    rebuild_schedule()
    t = threading.Thread(target=_scheduler_loop, daemon=True, name="bot-scheduler")
    t.start()


# Boot scheduler at import time so gunicorn launches it.
start_scheduler()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
