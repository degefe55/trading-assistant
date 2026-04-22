"""
Telegram webhook handler — always-on Flask service for Railway.
Receives Telegram updates instantly when user sends /buy, /sell, /pnl, etc.
Runs 24/7 on Railway free tier.

Deploy: connect this file's repo root to Railway, it auto-detects.
"""
import os
import sys
from flask import Flask, request, jsonify

# Add parent directory to path so we can import the bot's core modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set MOCK_MODE off by default for webhook (production)
os.environ.setdefault("MOCK_MODE", "false")
os.environ.setdefault("ACTIVE_MARKETS", "US")

from core import commands, telegram_client, sheets
from core.logger import log_event
from config import TELEGRAM_CHAT_ID


app = Flask(__name__)


@app.route("/", methods=["GET"])
def health():
    """Health check endpoint - Railway pings this."""
    return jsonify({"status": "ok", "service": "trading-bot-webhook"})


@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    """Telegram sends user messages here. Process and reply instantly."""
    try:
        update = request.get_json()
        if not update:
            return jsonify({"ok": True}), 200

        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))

        # Only accept messages from the owner's chat
        if chat_id != str(TELEGRAM_CHAT_ID):
            return jsonify({"ok": True, "ignored": "wrong chat"}), 200

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return jsonify({"ok": True, "ignored": "not a command"}), 200

        # Initialize sheets connection once
        try:
            sheets.initialize_sheet()
        except Exception as e:
            log_event("WARN", "webhook", f"Sheet init warn: {e}")

        # Dispatch command
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
