"""
Follow-up handler. Shared between:
  - /ask <RecID> <question>  (commands.py)
  - Threaded reply on a brief Telegram message (webhook/app.py)

Loads the recommendation by RecID, builds context, calls Haiku, returns
the answer. No streaming, no tool use — just one cheap call per question.
"""
from core import sheets, claude_client
from core.logger import log_event


MAX_QUESTION_LEN = 500


def answer_followup(rec_id: str, question: str) -> dict:
    """Answer a follow-up question about a recommendation.

    Args:
        rec_id: identifier of the recommendation, e.g. 20260430-1530-PRE-SPWO
        question: free-text from the user, max ~500 chars

    Returns:
        dict with keys:
            ok (bool)
            answer (str)         on success: Claude's answer
            error (str)          on failure: short error message
            cost_usd (float)     0 on failure
            ticker (str)         from the rec, for display
            action (str)         from the rec, for display
    """
    if not rec_id:
        return {"ok": False, "error": "No RecID provided", "cost_usd": 0}
    if not question or not question.strip():
        return {"ok": False, "error": "No question provided", "cost_usd": 0}

    question = question.strip()[:MAX_QUESTION_LEN]

    rec = sheets.read_recommendation(rec_id)
    if not rec:
        return {"ok": False,
                "error": f"RecID <code>{rec_id}</code> not found",
                "cost_usd": 0}

    # Build the context block from the rec row. Keep it small — this
    # is a cheap Haiku call, not a full analyst rerun.
    context_lines = [
        f"RecID: {rec.get('RecID', '')}",
        f"Ticker: {rec.get('Ticker', '')}",
        f"Time: {rec.get('Date', '')} {rec.get('Time_KSA', '')} KSA",
        f"Brief type: {rec.get('BriefType', '')}",
        f"Action: {rec.get('Action', '')} (confidence: {rec.get('Confidence', '')})",
        f"Urgent: {rec.get('Urgent', '')}",
        f"One-line plan: {rec.get('OneLinePlan', '')}",
        f"Price at call: {rec.get('PriceAtCall', '')}",
        f"Action price: {rec.get('ActionPrice', '')}",
        f"Stop loss: {rec.get('StopLoss', '')}",
        f"Target: {rec.get('Target', '')}",
        f"Risk score: {rec.get('RiskScore', '')}",
        f"News headline (top): {rec.get('TopNewsHeadline', '')}",
        f"Reasoning: {rec.get('Reasoning', '')}",
    ]
    context_block = "\n".join(context_lines)

    # Load prompt template
    try:
        template = claude_client.load_prompt("followup")
    except Exception as e:
        log_event("ERROR", "followup", f"Could not load prompt: {e}")
        return {"ok": False, "error": "Internal: prompt template missing",
                "cost_usd": 0}

    # Substitute. Using simple replace because the template uses {REC_CONTEXT}
    # and {QUESTION} as plain placeholders, NOT Python format spec.
    user_msg = template.replace("{REC_CONTEXT}", context_block) \
                       .replace("{QUESTION}", question)

    # System prompt is just a one-liner; the meat is in user_msg
    system = "You answer trading follow-up questions concisely."

    text, meta = claude_client.call_filter(system, user_msg)
    if not text or "error" in meta:
        return {"ok": False,
                "error": meta.get("error", "Empty response from Claude"),
                "cost_usd": meta.get("cost_usd", 0)}

    return {
        "ok": True,
        "answer": text.strip(),
        "cost_usd": meta.get("cost_usd", 0),
        "ticker": rec.get("Ticker", ""),
        "action": rec.get("Action", ""),
    }
