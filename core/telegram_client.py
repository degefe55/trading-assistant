"""
Telegram client.
Sends alerts/briefs to your chat.

Phase changes:
- send_message() now returns the Telegram message_id on success (int) or
  None on failure. Backward-compatible truthiness: any int is truthy, so
  callers using `if telegram_client.send_message(...)` still work. Callers
  that want to track which RecIDs a message covered (for threaded
  follow-up replies) keep the returned id and call
  sheets.record_message_recids() with it.
- send_message() now accepts optional reply_to_message_id (int) and
  inline_keyboard (list of lists of dicts) kwargs.
- answer_callback_query() added so we can acknowledge inline-button taps.
"""
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from core.logger import log_event


TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send_message(text: str, parse_mode: str = "HTML",
                 disable_preview: bool = True,
                 reply_to_message_id: int = None,
                 inline_keyboard: list = None):
    """Send a message to the configured chat.

    Returns:
        int   - Telegram message_id on success (truthy)
        None  - on failure (falsy)

    For long text (>4000 chars), splits into parts. Returns the
    message_id of the FIRST part — threaded replies thus land on the
    start of the brief, which feels natural.

    The inline_keyboard arg, if provided, is attached only to the LAST
    part of a split message so action buttons appear at the very end.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log_event("ERROR", "telegram", "Token or chat_id missing")
        return None

    if len(text) > 4000:
        parts = _split_message(text, 3900)
        first_id = None
        last_idx = len(parts) - 1
        for i, part in enumerate(parts):
            kb = inline_keyboard if i == last_idx else None
            rt = reply_to_message_id if i == 0 else None
            mid = _send_one(part, parse_mode, disable_preview, rt, kb)
            if mid is None:
                return None
            if first_id is None:
                first_id = mid
        return first_id

    return _send_one(text, parse_mode, disable_preview,
                     reply_to_message_id, inline_keyboard)


def _send_one(text, parse_mode, disable_preview,
              reply_to_message_id, inline_keyboard):
    """Send a single (under-limit) message. Returns message_id or None."""
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    if inline_keyboard:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}

    try:
        r = requests.post(f"{TG_BASE}/sendMessage", json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        result = data.get("result") or {}
        msg_id = result.get("message_id")
        log_event("INFO", "telegram",
                  f"Sent message ({len(text)} chars, id={msg_id})")
        return msg_id
    except Exception as e:
        log_event("ERROR", "telegram", f"Send failed: {e}")
        return None


def _split_message(text: str, chunk_size: int) -> list:
    """Split long message at line boundaries."""
    lines = text.split("\n")
    chunks, current = [], []
    size = 0
    for line in lines:
        if size + len(line) + 1 > chunk_size and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_error_alert(error: str, context: dict = None):
    """Send an error alert to the chat."""
    msg = f"🚨 <b>Bot Error</b>\n\n{error}"
    if context:
        msg += f"\n\nContext: <code>{str(context)[:500]}</code>"
    return send_message(msg)


def get_updates(offset: int = 0) -> list:
    """Fetch new messages (for command-polling mode).

    Conflicts with webhook setup — Telegram returns 409 if a webhook is
    configured. The webhook path in webhook/app.py is the authoritative
    command channel.
    """
    if not TELEGRAM_BOT_TOKEN:
        return []
    try:
        r = requests.get(
            f"{TG_BASE}/getUpdates",
            params={"offset": offset, "timeout": 0},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        log_event("ERROR", "telegram", f"Get updates failed: {e}")
        return []


def answer_callback_query(callback_query_id: str, text: str = None,
                          show_alert: bool = False) -> bool:
    """Acknowledge a button tap so the user's button-spinner stops.

    Args:
        text: optional toast notification text shown to the user
        show_alert: True for a popup, False for a brief toast (default)
    """
    if not TELEGRAM_BOT_TOKEN:
        return False
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]
        payload["show_alert"] = show_alert
    try:
        r = requests.post(f"{TG_BASE}/answerCallbackQuery",
                          json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        log_event("WARN", "telegram", f"answerCallbackQuery failed: {e}")
        return False
