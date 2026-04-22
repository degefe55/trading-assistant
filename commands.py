"""
Claude API client.
Wraps calls to Haiku (filter) and Sonnet (analysis).
Tracks tokens + cost per call, logs everything.
"""
import os
from anthropic import Anthropic
from config import (ANTHROPIC_API_KEY, CLAUDE_FILTER_MODEL, CLAUDE_ANALYST_MODEL,
                    MAX_OUTPUT_TOKENS_FILTER, MAX_OUTPUT_TOKENS_ANALYST,
                    ACTIVE_PROMPTS)
from core.logger import log_event


# Approximate pricing per 1M tokens (as of 2026; Haiku/Sonnet-4.6 class)
PRICING = {
    CLAUDE_FILTER_MODEL: {"input": 1.00, "output": 5.00},   # Haiku 4.5
    CLAUDE_ANALYST_MODEL: {"input": 3.00, "output": 15.00}, # Sonnet 4.6
}


_client = None


def _get_client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost of a call."""
    p = PRICING.get(model, {"input": 3.00, "output": 15.00})
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


def load_prompt(prompt_key: str) -> str:
    """Load a prompt file by key. Falls back gracefully if file missing."""
    filename = ACTIVE_PROMPTS.get(prompt_key)
    if not filename:
        log_event("ERROR", "claude_client", f"No prompt configured for '{prompt_key}'")
        return ""
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts", filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        log_event("ERROR", "claude_client", f"Prompt file not found: {path}")
        return ""


def call_filter(system: str, user: str) -> tuple[str, dict]:
    """
    Cheap Haiku call - used for relevance scoring.
    Returns (text_response, metadata).
    """
    client = _get_client()
    try:
        resp = client.messages.create(
            model=CLAUDE_FILTER_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS_FILTER,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = resp.content[0].text if resp.content else ""
        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        cost = _calculate_cost(CLAUDE_FILTER_MODEL, in_tok, out_tok)
        meta = {
            "model": CLAUDE_FILTER_MODEL,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_usd": cost,
            "prompt_version": ACTIVE_PROMPTS.get("filter"),
        }
        log_event("INFO", "claude_client", "Haiku filter call",
                  data={"in": in_tok, "out": out_tok},
                  tokens=in_tok + out_tok, cost=cost)
        return text, meta
    except Exception as e:
        log_event("ERROR", "claude_client", f"Filter call failed: {e}")
        return "", {"error": str(e)}


def call_analyst(system: str, user: str) -> tuple[str, dict]:
    """
    Sonnet call - used for full analysis.
    Returns (text_response, metadata).
    """
    client = _get_client()
    try:
        resp = client.messages.create(
            model=CLAUDE_ANALYST_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS_ANALYST,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = resp.content[0].text if resp.content else ""
        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        cost = _calculate_cost(CLAUDE_ANALYST_MODEL, in_tok, out_tok)
        meta = {
            "model": CLAUDE_ANALYST_MODEL,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cost_usd": cost,
            "prompt_version": ACTIVE_PROMPTS.get("analyst"),
        }
        log_event("INFO", "claude_client", "Sonnet analyst call",
                  data={"in": in_tok, "out": out_tok},
                  tokens=in_tok + out_tok, cost=cost)
        return text, meta
    except Exception as e:
        log_event("ERROR", "claude_client", f"Analyst call failed: {e}")
        return "", {"error": str(e)}
