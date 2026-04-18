"""
Logger.
Writes to:
1. stdout (visible in GitHub Actions run logs)
2. Google Sheet 'Logs' tab (visible to you on phone)
Every bot action is traceable.
"""
import sys
from datetime import datetime
from config import KSA_TZ, DEBUG_MODE

# Local log buffer flushed to Sheet at end of each run
_log_buffer = []


def log_event(level: str, module: str, event: str, data: dict = None,
              tokens: int = 0, cost: float = 0.0):
    """Record an event. Flushes to Sheet at end of run."""
    ts = datetime.now(KSA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    entry = {
        "timestamp": ts,
        "level": level,
        "module": module,
        "event": event,
        "data": _truncate_data(data) if data else "",
        "tokens": tokens,
        "cost_usd": round(cost, 6),
    }
    _log_buffer.append(entry)

    # Also print to stdout for GitHub Actions visibility
    icon = {"INFO": "ℹ️", "WARN": "⚠️", "ERROR": "❌", "DEBUG": "🔍"}.get(level, "•")
    line = f"{icon} [{ts}] {module}: {event}"
    if data and (DEBUG_MODE or level in ("WARN", "ERROR")):
        line += f" | data={entry['data']}"
    print(line, file=sys.stderr if level == "ERROR" else sys.stdout)


def _truncate_data(data: dict, max_len: int = 500) -> str:
    """Flatten data to string, truncate if very long."""
    s = str(data)
    return s if len(s) <= max_len else s[:max_len] + "..."


def get_log_buffer() -> list:
    """Return collected logs for this run."""
    return _log_buffer.copy()


def clear_log_buffer():
    """Reset log buffer at start of run."""
    _log_buffer.clear()


def get_run_summary() -> dict:
    """Summarize this run: errors, costs, token usage."""
    total_tokens = sum(e["tokens"] for e in _log_buffer)
    total_cost = sum(e["cost_usd"] for e in _log_buffer)
    errors = [e for e in _log_buffer if e["level"] == "ERROR"]
    warns = [e for e in _log_buffer if e["level"] == "WARN"]
    return {
        "total_events": len(_log_buffer),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 4),
        "error_count": len(errors),
        "warn_count": len(warns),
        "errors": [f"{e['module']}: {e['event']}" for e in errors],
    }
