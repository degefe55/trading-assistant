"""
Telegram client.
Sends alerts/briefs to your chat.
Parses incoming commands for command mode (Phase B extends this).
"""
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from core.logger import log_event


TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send_message(text: str, parse_mode: str = "HTML", disable_preview: bool = True) -> bool:
    """Send a message to the configured chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log_event("ERROR", "telegram", "Token or chat_id missing")
        return False
    # Telegram has 4096 char limit - split if needed
    if len(text) > 4000:
        parts = _split_message(text, 3900)
        return all(send_message(p, parse_mode, disable_preview) for p in parts)
    try:
        r = requests.post(
            f"{TG_BASE}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": disable_preview,
            },
            timeout=15,
        )
        # Always capture Telegram's own error message, not just HTTP status
        if not r.ok:
            try:
                tg_err = r.json()
                description = tg_err.get("description", "no description")
                error_code = tg_err.get("error_code", r.status_code)
                log_event("ERROR", "telegram",
                          f"Send failed: [{error_code}] {description}",
                          data={"chat_id_masked": str(TELEGRAM_CHAT_ID)[:3] + "***",
                                "first_100_chars": text[:100]})
            except Exception:
                log_event("ERROR", "telegram", f"Send failed: HTTP {r.status_code}")
            # Fallback: try sending as plain text if HTML parsing was the issue
            if parse_mode == "HTML":
                log_event("INFO", "telegram", "Retrying as plain text...")
                return _send_plain_fallback(text)
            return False
        log_event("INFO", "telegram", f"Sent message ({len(text)} chars)")
        return True
    except Exception as e:
        log_event("ERROR", "telegram", f"Send exception: {e}")
        return False


def _send_plain_fallback(html_text: str) -> bool:
    """Strip HTML tags and retry as plain text. Last-ditch attempt."""
    import re
    plain = re.sub(r"<[^>]+>", "", html_text)
    plain = plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    try:
        r = requests.post(
            f"{TG_BASE}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": plain,
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if r.ok:
            log_event("INFO", "telegram", "Plain text fallback succeeded")
            return True
        tg_err = r.json() if r.content else {}
        log_event("ERROR", "telegram",
                  f"Plain text also failed: {tg_err.get('description', r.status_code)}")
        return False
    except Exception as e:
        log_event("ERROR", "telegram", f"Plain fallback exception: {e}")
        return False


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
    """Send a critical error notification."""
    msg = f"🚨 <b>Bot Error</b>\n\n{error}"
    if context:
        msg += f"\n\nContext: <code>{str(context)[:500]}</code>"
    send_message(msg)


def get_updates(offset: int = 0) -> list:
    """
    Fetch new messages (for command mode).
    Returns list of update dicts.
    Used by command polling script (Phase B).
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
