"""
Phase G.2 — option-method per-tick runner.

Single entry point `run_method_tick()` that the webhook scheduler
calls every METHOD_INTERVAL_SEC during US extended hours. Pulls ES
futures bars from Databento, calls the rule engine, then drives the
state machine.

ES (front-month e-mini S&P futures) replaced SPY in the Phase G data
migration: ES trades nearly 24h on Globex so the runner has real
data during pre/post US session, and ES is the underlying behind
TradingView's US500 chart that the friend's setup is read on. Trade
execution is still SPX options on IBKR — Databento is purely chart-
data for the rule engine.

All Telegram alerts and sheet writes live in core/method_state.py.
This module is the gate-and-glue layer:

    gates → databento fetch → method_option.evaluate_setup
                            → method_state.handle_tick

Pure data fetch + dispatch — no AI, $0 per tick.

Honored gates (in order, fail-fast):
  1. METHOD_ENABLED (Sheet wins; Python const fallback)
  2. /pause file absent (shared with briefs + watcher)
  3. Method hours window (Mon-Fri 11:00–23:55 KSA)
  4. Cancellation flag absent (analyst._cancel_flags["method"])
"""
import os
from datetime import datetime, time as dtime

from config import (KSA_TZ, METHOD_ENABLED, METHOD_INTERVAL_SEC,
                    METHOD_TICKER)
from core import (analyst, databento_client, method_option,
                  method_state, sheets)
from core.logger import log_event


PAUSE_FILE = "/tmp/bot_paused.txt"

# Phase G.4 — runner is dormant; the option method is now driven by
# TradingView Pine Script webhooks (see webhook/app.py
# /webhook/tradingview + core.method_state.handle_webhook_event).
# This file is preserved so the polling path can be re-enabled by
# flipping the flag and re-registering the scheduler job.
METHOD_RUNNER_DISABLED = True

# US extended-hours window in KSA. Wide enough to cover pre-market
# (≈11:00 KSA = 04:00 ET) through after-hours (≈23:55 KSA = 16:55 ET).
METHOD_OPEN_KSA = dtime(11, 0)
METHOD_CLOSE_KSA = dtime(23, 55)

# Track the last skip reason so the runner only logs on transitions
# (e.g. "we just left market hours") instead of once per tick. Set to
# None on first run so the first state change still emits a line.
_last_skip_state = None

# Bars per timeframe — enough to seed MACD-26 + 9 signal + structural
# 30-bar lookback comfortably.
_LOOKBACK_BARS = 100


def _is_paused() -> bool:
    # Phase A — pause state lives in the Config tab now.
    # PAUSE_FILE constant is kept for legacy diagnostic purposes only.
    from core import sheets
    return sheets.is_paused()


def _is_method_hours_now() -> bool:
    now = datetime.now(KSA_TZ)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return METHOD_OPEN_KSA <= t <= METHOD_CLOSE_KSA


def is_method_enabled() -> bool:
    """Sheet-config wins; Python const is the fallback. Mirrors the
    watcher pattern (so /method on writes Config, takes effect on the
    next tick).

    Public so the webhook handler can gate /webhook/tradingview on the
    same flag — without this, /method off only stops the dormant
    polling path and TradingView alerts keep firing."""
    try:
        cfg = sheets.read_config() or {}
        raw = cfg.get("METHOD_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip().lower() == "true"
    except Exception as e:
        log_event("WARN", "method",
                  f"Config read failed; falling back to env: {e}")
    return METHOD_ENABLED


def run_method_tick() -> dict:
    """One tick of the option method. Returns a small status dict
    suitable for /status and HTTP responses."""
    global _last_skip_state
    # Gate 0 (Phase G.4): dormant — option method is now webhook-driven.
    # Return silently; the scheduler also no longer registers this tick,
    # so reaching here means someone called it manually.
    if METHOD_RUNNER_DISABLED:
        return {"ran": False, "skipped_reason": "runner-disabled"}

    def _log_skip_once(reason: str):
        global _last_skip_state
        if _last_skip_state != reason:
            log_event("INFO", "method", f"tick skipped: {reason}")
            _last_skip_state = reason

    # Gate 1: enabled?
    if not is_method_enabled():
        _log_skip_once("disabled")
        return {"ran": False, "skipped_reason": "disabled"}

    # Gate 2: paused?
    if _is_paused():
        _log_skip_once("paused")
        return {"ran": False, "skipped_reason": "paused"}

    # Gate 3: hours window
    if not _is_method_hours_now():
        _log_skip_once("off-hours")
        return {"ran": False, "skipped_reason": "off-hours"}

    # We've cleared the gates — clear the skip-state cache so the next
    # gate change logs once.
    _last_skip_state = None

    # Gate 4: cancellation
    analyst.reset_cancel("method")
    try:
        analyst._check_cancelled("method")
    except analyst.BriefCancelled:
        log_event("WARN", "method", "tick aborted at start by /cancel method")
        return {"ran": False, "skipped_reason": "cancelled"}

    ticker = METHOD_TICKER
    bars_1m = databento_client.get_bars(ticker, 1,
                                        lookback_bars=_LOOKBACK_BARS)
    bars_5m = databento_client.get_bars(ticker, 5,
                                        lookback_bars=_LOOKBACK_BARS)
    bars_10m = databento_client.get_bars(ticker, 10,
                                         lookback_bars=_LOOKBACK_BARS)

    # Need *some* bars on each timeframe. The rule engine will already
    # degrade to NO_SETUP on insufficient data, but skipping early
    # saves a no-op pass through the tracker.
    if not bars_1m or not bars_5m or not bars_10m:
        log_event("WARN", "method",
                  f"tick skipped: bars empty "
                  f"(1m={len(bars_1m)}, 5m={len(bars_5m)}, "
                  f"10m={len(bars_10m)})")
        return {"ran": False, "skipped_reason": "no-bars",
                "bars_1m": len(bars_1m), "bars_5m": len(bars_5m),
                "bars_10m": len(bars_10m)}

    try:
        analyst._check_cancelled("method")
    except analyst.BriefCancelled:
        log_event("WARN", "method", "tick aborted post-fetch by /cancel method")
        return {"ran": False, "skipped_reason": "cancelled"}

    evaluation = method_option.evaluate_setup(bars_1m, bars_5m, bars_10m)

    tracker = method_state.get_tracker()
    transitions = tracker.handle_tick(evaluation, bars_1m, bars_5m,
                                      bars_10m, ticker=ticker)

    log_event("INFO", "method",
              f"tick ok ticker={ticker} "
              f"call_state={evaluation.get('call_state')} "
              f"put_state={evaluation.get('put_state')}")

    return {
        "ran": True,
        "ticker": ticker,
        "interval_sec": METHOD_INTERVAL_SEC,
        "evaluation": {
            "call_state": evaluation.get("call_state"),
            "put_state": evaluation.get("put_state"),
        },
        "transitions": transitions,
    }
