"""
Phase G.5 — diagnostic Haiku agent.

Scans the Logs tab on a schedule, groups recurring ERROR/WARN messages
by (module, level, first 50 chars of event), and when any group crosses
the threshold inside the time window, sends one Telegram alert per
fresh pattern with a Haiku-generated 1–2 sentence diagnosis.

Designed to be cheap and quiet:
- Reads the tail of the Logs tab via a single ranged sheets read
- Only calls Haiku when a pattern actually crosses the threshold
- Per-hour dedupe so a recurring problem doesn't spam the chat
- Whole tick is wrapped in try/except — the agent must never crash
  the scheduler thread that hosts it.

Manual entry point: diagnose_now(window_minutes=60), used by the
/diagnose Telegram command. Same logic, ungated by
DIAGNOSTIC_AGENT_ENABLED, with a wider default window.
"""
import threading
from datetime import datetime, timedelta

from config import KSA_TZ, DIAGNOSTIC_AGENT_ENABLED
from core import sheets, telegram_client, claude_client
from core.logger import log_event


def is_diagnostic_enabled() -> bool:
    """Phase B — Sheet-config wins, env fallback. Mirrors the
    method/watcher pattern so the /menu Toggles screen can flip
    DIAGNOSTIC_AGENT_ENABLED at runtime."""
    try:
        cfg = sheets.read_config() or {}
        raw = cfg.get("DIAGNOSTIC_AGENT_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip().lower() == "true"
    except Exception as e:
        log_event("WARN", "log_analyst",
                  f"Config read failed; falling back to env: {e}")
    return DIAGNOSTIC_AGENT_ENABLED


# Defaults — tunable via the function args (manual /diagnose uses 60m).
WINDOW_MINUTES = 15
PATTERN_THRESHOLD = 10
LOG_LOOKBACK_ROWS = 200


# In-memory dedupe keyed by (signature, hour_bucket). Once we alert
# on a (module, level, message-prefix) inside a given hour, we don't
# alert on it again until the next hour rolls. Keeps the chat quiet
# when a single error is rapid-firing for a sustained period.
_last_alerted_signature_set = set()
_last_alerted_lock = threading.Lock()


def _signature(module: str, level: str, event: str) -> tuple:
    return ((module or "").strip(),
            (level or "").strip().upper(),
            (event or "").strip()[:50])


def _hour_bucket(now=None) -> str:
    n = now or datetime.now(KSA_TZ)
    return n.strftime("%Y-%m-%d %H")


def _parse_log_ts(ts: str):
    """Parse a Logs-tab timestamp ('YYYY-MM-DD HH:MM:SS' in KSA) to a
    timezone-aware datetime. Returns None on parse failure."""
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts).strip(),
                                 "%Y-%m-%d %H:%M:%S").replace(
                                     tzinfo=KSA_TZ)
    except (ValueError, TypeError):
        return None


def run_log_analyst_tick(window_minutes: int = WINDOW_MINUTES,
                         source: str = "schedule") -> dict:
    """One pass over the Logs tab. Wrap-everything-in-try so that no
    exception in the analyst can take down the scheduler thread.

    Returns a status dict for diagnostics:
      {ran, rows, filtered, patterns, alerted, error?}
    """
    if source != "manual" and not is_diagnostic_enabled():
        return {"ran": False, "skipped": "disabled"}

    try:
        rows = sheets.read_recent_logs(limit=LOG_LOOKBACK_ROWS)
    except Exception as e:
        log_event("ERROR", "log_analyst",
                  f"read_recent_logs failed: {e}")
        return {"ran": False, "error": str(e)}

    if not rows:
        return {"ran": True, "rows": 0, "filtered": 0,
                "patterns": 0, "alerted": 0}

    now = datetime.now(KSA_TZ)
    cutoff = now - timedelta(minutes=window_minutes)

    filtered = []
    try:
        for r in rows:
            level = (r.get("Level") or "").strip().upper()
            if level not in ("ERROR", "WARN", "WARNING"):
                continue
            ts = _parse_log_ts(r.get("Timestamp", ""))
            if ts is None or ts < cutoff:
                continue
            filtered.append({
                "ts": ts,
                "module": str(r.get("Module") or ""),
                "level": level,
                "event": str(r.get("Event") or ""),
            })
    except Exception as e:
        log_event("ERROR", "log_analyst", f"row filter failed: {e}")
        return {"ran": False, "error": str(e)}

    groups = {}
    for r in filtered:
        sig = _signature(r["module"], r["level"], r["event"])
        groups.setdefault(sig, []).append(r)

    patterns = []
    for sig, items in groups.items():
        if len(items) < PATTERN_THRESHOLD:
            continue
        items.sort(key=lambda x: x["ts"])
        patterns.append({
            "signature": sig,
            "count": len(items),
            "first_ts": items[0]["ts"],
            "module": sig[0],
            "level": sig[1],
            "event_prefix": sig[2],
            "samples": items[:3],
        })

    bucket = _hour_bucket(now)
    fresh = []
    with _last_alerted_lock:
        for p in patterns:
            key = p["signature"] + (bucket,)
            if key not in _last_alerted_signature_set:
                fresh.append(p)
                _last_alerted_signature_set.add(key)

    sent = 0
    for p in fresh:
        try:
            _alert_pattern(p, window_minutes)
            sent += 1
        except Exception as e:
            log_event("ERROR", "log_analyst",
                      f"alert send failed for {p['signature']}: {e}")

    log_event("INFO", "log_analyst",
              f"tick ok ({source}): rows={len(rows)} "
              f"window={window_minutes}m filtered={len(filtered)} "
              f"patterns={len(patterns)} fresh={len(fresh)}")
    return {"ran": True, "rows": len(rows), "filtered": len(filtered),
            "patterns": len(patterns), "alerted": sent}


def _alert_pattern(p: dict, window_minutes: int) -> None:
    """Build a Telegram message for one pattern: count, first occurrence,
    a Haiku-generated 1–2 sentence summary + likely root cause."""
    samples_text = "\n".join(
        f"- [{s['ts'].strftime('%H:%M:%S')}] {s['event'][:200]}"
        for s in p["samples"]
    )
    user_prompt = (
        f"You are debugging a Python trading bot. The following error or "
        f"warning has occurred {p['count']} times in the last "
        f"{window_minutes} minutes (module={p['module']}, "
        f"level={p['level']}). Sample messages:\n\n{samples_text}\n\n"
        f"In 1–2 sentences: summarize the pattern and the most likely "
        f"root cause. Be concrete; if you're not sure, say so."
    )
    system_prompt = ("You are a senior software engineer triaging error "
                     "logs. Give terse, actionable diagnoses. "
                     "Avoid speculation when context is thin.")
    summary, _meta = claude_client.call_filter(system_prompt, user_prompt)
    summary = (summary or "(Haiku returned no summary)").strip()

    text = (
        f"🚨 <b>Log pattern detected</b>\n"
        f"<code>{p['module']}</code> · <code>{p['level']}</code> · "
        f"<code>{p['count']}×</code> in last {window_minutes}m\n"
        f"First seen: <code>{p['first_ts'].strftime('%H:%M:%S KSA')}</code>\n"
        f"─────────────\n"
        f"<i>{p['event_prefix']}…</i>\n\n"
        f"<b>Diagnosis:</b> {summary}"
    )
    telegram_client.send_message(text)


def diagnose_now(window_minutes: int = 60) -> dict:
    """Manual entry point used by the /diagnose Telegram command.
    Wider default window; ignores DIAGNOSTIC_AGENT_ENABLED so the user
    can always force a one-shot scan."""
    return run_log_analyst_tick(window_minutes=window_minutes,
                                source="manual")
