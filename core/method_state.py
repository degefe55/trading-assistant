"""
Phase G.2 — option-method state machine.

Owns the per-direction signal lifecycle:

    NO_SETUP   →   PRE_SIGNAL   →   TRACKING   →   DONE   →   (NO_SETUP again)
                  pre-alert        entry alert     setup-end /
                                                   invalidation /
                                                   user-cancel

One MethodSignalTracker singleton holds the in-memory state for both
directions (call + put). Every transition is mirrored to the
MethodSignals sheet so a Railway redeploy mid-trade can rehydrate the
tracker and keep watching the same signal.

Pure rule-engine bits live in core.method_option. This module is the
side-effect layer: levels computation, Telegram alerts, sheet writes,
state transitions. No AI calls — cost per tick is $0.
"""
import math
import threading
from datetime import datetime

from config import (KSA_TZ, METHOD_TICKER, METHOD_MAX_DAILY_SIGNALS)
from core import method_option, sheets, telegram_client
from core.logger import log_event


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NO_SETUP = "NO_SETUP"
PRE_SIGNAL = "PRE_SIGNAL"
TRACKING = "TRACKING"
DONE = "DONE"

# How many 5m bars back we look for the structural swing low/high used
# to size TP1 and place the stop. Mirrors method_option.is_trend_*.
_STRUCTURAL_LOOKBACK_5M = 30


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------

class MethodSignalTracker:
    """Stateful per-direction tracker. One instance per process; built
    via get_tracker(). Not safe for concurrent ticks — the runner holds
    a single per-process method lock so handle_tick is serialized."""

    def __init__(self):
        # Per-direction in-memory state. Each value is either:
        #   {"state": NO_SETUP}
        # or
        #   {"state": PRE_SIGNAL|TRACKING, "signal_id": ..., "row": {...}}
        # `row` is the mirror of the latest sheet row written, used for
        # in-memory lookups (TP hit flags, stop, etc.) so we don't read
        # the sheet on every tick.
        self._dirs = {
            "call": {"state": NO_SETUP, "signal_id": None, "row": None},
            "put":  {"state": NO_SETUP, "signal_id": None, "row": None},
        }
        self._rehydrated = False

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def rehydrate_from_sheets(self):
        """Called once on startup. Pulls active rows from MethodSignals
        and rebuilds in-memory state so a redeploy keeps tracking the
        same signal instead of forgetting it.

        If two active rows exist for the same direction (shouldn't
        happen, but Sheets isn't transactional), the most recent wins
        and the older is force-DONE in memory only.
        """
        if self._rehydrated:
            return
        self._rehydrated = True
        try:
            active = sheets.read_active_method_signals()
        except Exception as e:
            log_event("WARN", "method", f"Rehydrate read failed: {e}")
            return

        for row in active:
            direction = str(row.get("Direction", "")).strip().lower()
            state = str(row.get("State", "")).strip().upper()
            signal_id = str(row.get("SignalID", "")).strip()
            if direction not in self._dirs:
                continue
            if state not in (PRE_SIGNAL, TRACKING) or not signal_id:
                continue
            # Last-write-wins by signal_id lexicographic order (encodes
            # date+time, so newer signals sort later).
            current = self._dirs[direction]
            if (current["signal_id"] is None
                    or signal_id > current["signal_id"]):
                self._dirs[direction] = {
                    "state": state,
                    "signal_id": signal_id,
                    "row": dict(row),
                }
        log_event("INFO", "method",
                  f"Rehydrated tracker: "
                  f"call={self._dirs['call']['state']}, "
                  f"put={self._dirs['put']['state']}")

    def state_snapshot(self) -> dict:
        """Diagnostic snapshot for /method status."""
        return {
            d: {
                "state": v["state"],
                "signal_id": v["signal_id"],
            }
            for d, v in self._dirs.items()
        }

    def state_snapshot_rich(self) -> dict:
        """Like state_snapshot but also includes the trigger/stop/tp1
        from the in-memory row, when present. Used by /menu Method
        dashboard for the 'CALL: PRE_SIGNAL @ 7232.50' lines."""
        out = {}
        for d, v in self._dirs.items():
            row = v.get("row") or {}
            out[d] = {
                "state": v["state"],
                "signal_id": v["signal_id"],
                "trigger": row.get("TriggerPrice"),
                "stop": row.get("StopPrice"),
                "tp1": row.get("TP1"),
            }
        return out

    def force_reset(self):
        """In-memory wipe. Caller (e.g. /method reset) is responsible
        for the sheet-side reset_method_signals_active call."""
        for d in self._dirs:
            self._dirs[d] = {"state": NO_SETUP, "signal_id": None,
                             "row": None}

    # -----------------------------------------------------------------
    # Per-tick driver
    # -----------------------------------------------------------------

    def handle_tick(self, evaluation: dict, bars_1m: list,
                    bars_5m: list, bars_10m: list,
                    ticker: str = None) -> dict:
        """Drive both call and put state machines for one tick.

        Returns a small dict useful for runner-level logging:
          {"call": {...}, "put": {...}}  (transitions actually made,
                                          alerts sent, etc.)
        """
        ticker = ticker or METHOD_TICKER
        details = (evaluation or {}).get("details", {}) or {}
        last_close = _last_close(bars_1m)
        out = {"call": {}, "put": {}}

        for direction in ("call", "put"):
            try:
                out[direction] = self._handle_direction(
                    direction=direction,
                    eval_state=(evaluation or {}).get(f"{direction}_state",
                                                      NO_SETUP),
                    details=details,
                    bars_1m=bars_1m,
                    bars_5m=bars_5m,
                    last_close=last_close,
                    ticker=ticker,
                )
            except Exception as e:
                log_event("ERROR", "method",
                          f"{direction} tick handling crashed: {e}")
                out[direction] = {"error": str(e)}
        return out

    # -----------------------------------------------------------------
    # Internal: per-direction transition logic
    # -----------------------------------------------------------------

    def _handle_direction(self, direction: str, eval_state: str,
                          details: dict, bars_1m: list, bars_5m: list,
                          last_close, ticker: str) -> dict:
        cur = self._dirs[direction]
        cur_state = cur["state"]

        # --- in NO_SETUP: maybe spawn a new pre-signal ---
        if cur_state == NO_SETUP:
            if eval_state == PRE_SIGNAL:
                return self._open_pre_signal(direction, details, bars_5m,
                                             last_close, ticker)
            # Direct NO_SETUP → ENTRY (rare: setup forms and triggers in
            # the same minute). Treat as: open pre-signal then advance to
            # entry on the same tick; the user gets one entry alert.
            if eval_state == "ENTRY_SIGNAL":
                opened = self._open_pre_signal(direction, details, bars_5m,
                                                last_close, ticker,
                                                silent=True)
                if opened.get("opened"):
                    return self._promote_to_entry(direction, details,
                                                   bars_5m, last_close,
                                                   ticker)
                return opened
            return {"state": NO_SETUP, "noop": True}

        # --- in PRE_SIGNAL: watch for entry trigger or fade ---
        if cur_state == PRE_SIGNAL:
            if eval_state == "ENTRY_SIGNAL":
                return self._promote_to_entry(direction, details, bars_5m,
                                               last_close, ticker)
            if eval_state == NO_SETUP:
                # Setup faded before entry. Close out the pre-signal
                # silently (no Telegram noise) so the user only hears
                # about live, actionable signals.
                self._close_signal(direction, reason="faded",
                                   notify=False)
                return {"state": NO_SETUP, "transition": "pre→done(fade)"}
            return {"state": PRE_SIGNAL, "noop": True}

        # --- in TRACKING: watch TPs, stop, and setup-end ---
        if cur_state == TRACKING:
            return self._handle_tracking(direction, eval_state, details,
                                          last_close, ticker)

        # Unknown — defensive reset
        log_event("WARN", "method",
                  f"{direction} unknown state {cur_state!r}; resetting")
        self._dirs[direction] = {"state": NO_SETUP, "signal_id": None,
                                 "row": None}
        return {"state": NO_SETUP, "reset": True}

    # -----------------------------------------------------------------
    # Transitions
    # -----------------------------------------------------------------

    def _open_pre_signal(self, direction: str, details: dict,
                          bars_5m: list, last_close, ticker: str,
                          silent: bool = False) -> dict:
        """NO_SETUP → PRE_SIGNAL. Daily-cap check first; abort if hit."""
        now = datetime.now(KSA_TZ)
        date_str = now.strftime("%Y-%m-%d")

        cd = sheets.read_method_cooldown(date_str, direction) or {}
        try:
            count_today = int(cd.get("SetupCount", 0) or 0)
        except (ValueError, TypeError):
            count_today = 0
        if count_today >= METHOD_MAX_DAILY_SIGNALS:
            log_event("INFO", "method",
                      f"{direction} pre-signal suppressed: daily cap "
                      f"hit ({count_today}/{METHOD_MAX_DAILY_SIGNALS})")
            return {"state": NO_SETUP, "capped": True}

        # Build signal_id: YYYYMMDD-HHMM-METHOD-CALL or -PUT
        signal_id = (f"{now.strftime('%Y%m%d-%H%M')}-METHOD-"
                     f"{direction.upper()}")

        fractal_key = "fractal_high_1m" if direction == "call" \
            else "fractal_low_1m"
        fractal = details.get(fractal_key) or {}
        trigger_level = _safe_num(
            fractal.get("high" if direction == "call" else "low")
        )

        levels = _structural_levels(direction, bars_5m,
                                     entry_price=last_close)
        # Persist the pre-signal row even if levels are partial — the
        # entry transition will overwrite with concrete prices.
        row = {
            "SignalID": signal_id,
            "Date": date_str,
            "Time_KSA": now.strftime("%H:%M:%S"),
            "Direction": direction,
            "State": PRE_SIGNAL,
            "TriggerPrice": trigger_level if trigger_level is not None else "",
            "StopPrice": levels.get("stop") or "",
            "TP1": levels.get("tp1") or "",
            "TP2": "",
            "TP3": "",
            "TP1Hit": "FALSE",
            "TP2Hit": "FALSE",
            "TP3Hit": "FALSE",
            "InvalidatedAt": "",
            "StateUpdatedAt": now.strftime("%H:%M:%S"),
            "Source": "method_option",
        }
        sheets.write_method_state(signal_id, row)
        sheets.bump_method_cooldown(date_str, direction, "LastPreSignalAt")
        sheets.bump_method_cooldown(date_str, direction, "SetupCount")

        self._dirs[direction] = {"state": PRE_SIGNAL,
                                 "signal_id": signal_id, "row": row}

        if not silent:
            _send_pre_signal_alert(ticker, direction, trigger_level,
                                   levels)
            log_event("INFO", "method",
                      f"{direction} PRE_SIGNAL opened {signal_id} "
                      f"trigger={trigger_level} stop={levels.get('stop')}")
        return {"state": PRE_SIGNAL, "opened": True,
                "signal_id": signal_id}

    def _promote_to_entry(self, direction: str, details: dict,
                          bars_5m: list, last_close, ticker: str) -> dict:
        """PRE_SIGNAL → TRACKING. Recompute levels using the actual
        entry price and write the entry alert."""
        # TODO (2026-05-09): _promote_to_entry is dead code in webhook mode (Phase G.4+).
        # The polling path was retired when Pine Script took over detection.
        # Consider deleting this function, _handle_direction, and handle_tick polling
        # logic in a focused refactor session. Until then, keep strings consistent
        # with G.5.0 reality so the dead path doesn't lie.
        cur = self._dirs[direction]
        signal_id = cur["signal_id"] or _make_signal_id(direction)

        if last_close is None:
            log_event("WARN", "method",
                      f"{direction} entry promotion skipped: no last close")
            return {"state": cur["state"], "skipped": "no_price"}

        levels = _structural_levels(direction, bars_5m,
                                     entry_price=last_close)
        fractal_key = "fractal_high_1m" if direction == "call" \
            else "fractal_low_1m"
        fractal = details.get(fractal_key) or {}
        trigger_level = _safe_num(
            fractal.get("high" if direction == "call" else "low")
        )

        now = datetime.now(KSA_TZ)
        date_str = now.strftime("%Y-%m-%d")
        row = (cur.get("row") or {}).copy()
        row.update({
            "SignalID": signal_id,
            "Date": date_str,
            "Time_KSA": row.get("Time_KSA") or now.strftime("%H:%M:%S"),
            "Direction": direction,
            "State": TRACKING,
            "TriggerPrice": last_close,
            "StopPrice": levels.get("stop") or row.get("StopPrice") or "",
            "TP1": levels.get("tp1") or "",
            "TP2": "",   # VWAP +2σ — Phase G.3
            "TP3": "",   # VWAP +3σ — Phase G.3
            "TP1Hit": "FALSE",
            "TP2Hit": "FALSE",
            "TP3Hit": "FALSE",
            "InvalidatedAt": "",
            "StateUpdatedAt": now.strftime("%H:%M:%S"),
            "Source": "method_option",
        })
        sheets.write_method_state(signal_id, row)
        sheets.bump_method_cooldown(date_str, direction, "LastEntryAt")

        self._dirs[direction] = {"state": TRACKING,
                                 "signal_id": signal_id, "row": row}

        # Persist to Recommendations so /list of recent calls includes
        # method-option signals alongside watcher / brief recs.
        try:
            sheets.append_recommendation({
                "rec_id": signal_id,  # reuse SignalID as RecID for cross-ref
                "brief_type": "method_option",
                "ticker": ticker,
                "market": "US",
                "price_at_call": last_close,
                "source": "method_option",
                "analysis": {
                    "action": f"{direction.upper()} (option-method)",
                    "confidence": "rule-based",
                    "action_urgent": True,
                    "one_line_plan": (
                        f"{direction.upper()} entry at {last_close} "
                        f"(1m close {'>'  if direction == 'call' else '<'} "
                        f"{trigger_level}); "
                        f"stop {levels.get('stop')}, TP1 {levels.get('tp1')}"
                    ),
                    "action_price": last_close,
                    "risk_score": 3,
                    "halal_ai_signal": "",
                    "deep_dive": {
                        "stop_loss": levels.get("stop"),
                        "target": levels.get("tp1"),
                        "reasoning": (
                            f"Trend 5m {direction}, 5m close through "
                            f"{trigger_level} (index-side fractal break). "
                            f"Range = fractal_high - swing_low; TP1 is 1:1 "
                            f"projected. VWAP-derived TP2/TP3 not wired in "
                            f"Phase G.2."
                        ),
                    },
                },
                "data_snapshot": {
                    "news_count": 0,
                    "top_news": [],
                },
            })
        except Exception as e:
            log_event("WARN", "method",
                      f"Recommendations append failed for {signal_id}: {e}")

        _send_entry_alert(ticker, direction, signal_id, last_close,
                          trigger_level, levels)
        log_event("INFO", "method",
                  f"{direction} ENTRY {signal_id} @ {last_close}")
        return {"state": TRACKING, "promoted": True,
                "signal_id": signal_id}

    def _handle_tracking(self, direction: str, eval_state: str,
                         details: dict, last_close, ticker: str) -> dict:
        """In TRACKING: check stops, TPs, and setup-end. Setup-end is
        when the structural trend or 10m MACD flips against the side."""
        cur = self._dirs[direction]
        signal_id = cur["signal_id"]
        row = (cur.get("row") or {}).copy()
        if not row or last_close is None:
            return {"state": TRACKING, "noop": True}

        stop = _safe_num(row.get("StopPrice"))
        tp1 = _safe_num(row.get("TP1"))

        # Invalidation: 1m close past the stop in the wrong direction.
        if stop is not None:
            invalidated = (last_close <= stop) if direction == "call" \
                else (last_close >= stop)
            if invalidated:
                row["State"] = DONE
                row["InvalidatedAt"] = datetime.now(KSA_TZ).strftime(
                    "%H:%M:%S")
                row["StateUpdatedAt"] = row["InvalidatedAt"]
                sheets.write_method_state(signal_id, row)
                self._dirs[direction] = {"state": NO_SETUP,
                                         "signal_id": None, "row": None}
                _send_invalidated_alert(ticker, direction, signal_id, stop)
                log_event("INFO", "method",
                          f"{direction} INVALIDATED {signal_id} "
                          f"@ {last_close} (stop {stop})")
                return {"state": NO_SETUP, "transition": "tracking→done(stop)"}

        # TP1 hit — only fire once per signal
        if (tp1 is not None
                and str(row.get("TP1Hit", "FALSE")).upper() != "TRUE"):
            tp1_hit = (last_close >= tp1) if direction == "call" \
                else (last_close <= tp1)
            if tp1_hit:
                row["TP1Hit"] = "TRUE"
                row["StateUpdatedAt"] = datetime.now(KSA_TZ).strftime(
                    "%H:%M:%S")
                sheets.write_method_state(signal_id, row)
                self._dirs[direction]["row"] = row
                _send_tp_hit_alert(ticker, direction, "TP1", tp1)
                log_event("INFO", "method",
                          f"{direction} TP1 HIT {signal_id} @ {last_close}")

        # Setup-end: trend on 5m or MACD on 10m flipped against us.
        if direction == "call":
            trend_ok = bool(details.get("trend_5m_bullish", False))
            macd_ok = bool(details.get("macd_10m_bullish_aligned", False))
        else:
            trend_ok = bool(details.get("trend_5m_bearish", False))
            macd_ok = bool(details.get("macd_10m_bearish_aligned", False))

        if not trend_ok or not macd_ok:
            row["State"] = DONE
            row["StateUpdatedAt"] = datetime.now(KSA_TZ).strftime(
                "%H:%M:%S")
            sheets.write_method_state(signal_id, row)
            self._dirs[direction] = {"state": NO_SETUP, "signal_id": None,
                                     "row": None}
            _send_setup_end_alert(ticker, direction, signal_id)
            log_event("INFO", "method",
                      f"{direction} SETUP_END {signal_id} "
                      f"(trend_ok={trend_ok}, macd_ok={macd_ok})")
            return {"state": NO_SETUP, "transition": "tracking→done(setup-end)"}

        return {"state": TRACKING, "noop": True}

    def _close_signal(self, direction: str, reason: str = "",
                      notify: bool = False):
        """Force-close the active signal for `direction`. Used by
        pre-signal fade, /method reset, and the 🛑 cancel button."""
        cur = self._dirs[direction]
        signal_id = cur.get("signal_id")
        if not signal_id:
            self._dirs[direction] = {"state": NO_SETUP, "signal_id": None,
                                     "row": None}
            return
        row = (cur.get("row") or {}).copy()
        row["SignalID"] = signal_id
        row["State"] = DONE
        row["StateUpdatedAt"] = datetime.now(KSA_TZ).strftime("%H:%M:%S")
        if reason:
            row["InvalidatedAt"] = (row.get("InvalidatedAt")
                                    or row["StateUpdatedAt"])
        sheets.write_method_state(signal_id, row)
        self._dirs[direction] = {"state": NO_SETUP, "signal_id": None,
                                 "row": None}
        log_event("INFO", "method",
                  f"{direction} signal {signal_id} closed (reason={reason})")
        if notify:
            telegram_client.send_message(
                f"🛑 <b>{METHOD_TICKER} {direction.upper()}</b> "
                f"tracking cancelled.",
            )

    def cancel_signal_by_id(self, signal_id: str) -> bool:
        """Honor the 🛑 inline button. Returns True if the signal was
        active in this process."""
        for d, v in self._dirs.items():
            if v.get("signal_id") == signal_id:
                self._close_signal(d, reason="user-cancelled", notify=True)
                return True
        return False

    # -----------------------------------------------------------------
    # Phase G.4 — TradingView webhook driver
    # -----------------------------------------------------------------

    def handle_webhook_event(self, payload: dict) -> dict:
        """Dispatch one TradingView alert payload. Idempotent on
        re-delivery — each handler checks current state before acting,
        so retries during the same lifecycle are safe."""
        event = str(payload.get("event", "")).strip().lower()
        direction = str(payload.get("direction", "")).strip().lower()
        if direction not in ("call", "put"):
            log_event("WARN", "method",
                      f"webhook bad direction: {direction!r}")
            return {"ok": False, "error": "bad direction"}
        if not event:
            log_event("WARN", "method", "webhook missing event")
            return {"ok": False, "error": "missing event"}

        ticker = _ticker_from_payload(payload)

        if event == "pre_signal":
            return self._webhook_pre_signal(direction, payload, ticker)
        if event == "entry":
            return self._webhook_entry(direction, payload, ticker)
        if event in ("tp1_hit", "tp2_hit", "tp3_hit"):
            return self._webhook_tp_hit(direction, event, payload, ticker)
        if event == "invalidated":
            return self._webhook_invalidated(direction, payload, ticker)
        if event == "setup_ended":
            return self._webhook_setup_ended(direction, payload, ticker)

        log_event("WARN", "method", f"webhook unknown event: {event!r}")
        return {"ok": False, "error": f"unknown event: {event}"}

    def _webhook_pre_signal(self, direction: str, payload: dict,
                            ticker: str) -> dict:
        now = datetime.now(KSA_TZ)
        date_str = now.strftime("%Y-%m-%d")

        cd = sheets.read_method_cooldown(date_str, direction) or {}
        try:
            count_today = int(cd.get("SetupCount", 0) or 0)
        except (ValueError, TypeError):
            count_today = 0
        if count_today >= METHOD_MAX_DAILY_SIGNALS:
            log_event("INFO", "method",
                      f"{direction} pre_signal suppressed: daily cap "
                      f"({count_today}/{METHOD_MAX_DAILY_SIGNALS})")
            return {"ok": True, "capped": True}

        signal_id = _make_signal_id(direction)
        trigger = _safe_num(payload.get("trigger_price"))
        stop = _safe_num(payload.get("stop_price"))
        tp1 = _safe_num(payload.get("tp1"))
        tp2 = _safe_num(payload.get("tp2"))
        tp3 = _safe_num(payload.get("tp3"))

        row = {
            "SignalID": signal_id,
            "Date": date_str,
            "Time_KSA": now.strftime("%H:%M:%S"),
            "Direction": direction,
            "State": PRE_SIGNAL,
            "TriggerPrice": trigger if trigger is not None else "",
            "StopPrice": stop if stop is not None else "",
            "TP1": tp1 if tp1 is not None else "",
            "TP2": tp2 if tp2 is not None else "",
            "TP3": tp3 if tp3 is not None else "",
            "TP1Hit": "FALSE",
            "TP2Hit": "FALSE",
            "TP3Hit": "FALSE",
            "InvalidatedAt": "",
            "StateUpdatedAt": now.strftime("%H:%M:%S"),
            "Source": "tradingview_webhook",
        }
        sheets.write_method_state(signal_id, row)
        sheets.bump_method_cooldown(date_str, direction, "LastPreSignalAt")
        sheets.bump_method_cooldown(date_str, direction, "SetupCount")

        self._dirs[direction] = {"state": PRE_SIGNAL,
                                 "signal_id": signal_id, "row": row}

        levels = {"stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3}
        _send_pre_signal_alert(ticker, direction, trigger, levels)
        log_event("INFO", "method",
                  f"{direction} PRE_SIGNAL (webhook) {signal_id} "
                  f"trigger={trigger} stop={stop}")
        return {"ok": True, "opened": True, "signal_id": signal_id}

    def _webhook_entry(self, direction: str, payload: dict,
                       ticker: str) -> dict:
        cur = self._dirs[direction]
        cur_state = cur.get("state", NO_SETUP)
        signal_id = cur.get("signal_id")

        now = datetime.now(KSA_TZ)
        date_str = now.strftime("%Y-%m-%d")

        # Idempotency: duplicate "entry" while already TRACKING is a noop.
        if cur_state == TRACKING and signal_id:
            log_event("INFO", "method",
                      f"{direction} entry ignored: already TRACKING "
                      f"{signal_id}")
            return {"ok": True, "noop": "already_tracking"}

        # No prior pre_signal: open inline (TradingView may collapse the
        # two events on a fast setup). Daily cap still gates here so an
        # entry-without-pre-signal can't exceed the limit.
        if cur_state == NO_SETUP or not signal_id:
            cd = sheets.read_method_cooldown(date_str, direction) or {}
            try:
                count_today = int(cd.get("SetupCount", 0) or 0)
            except (ValueError, TypeError):
                count_today = 0
            if count_today >= METHOD_MAX_DAILY_SIGNALS:
                log_event("INFO", "method",
                          f"{direction} entry suppressed: daily cap "
                          f"({count_today}/{METHOD_MAX_DAILY_SIGNALS})")
                return {"ok": True, "capped": True}
            signal_id = _make_signal_id(direction)
            sheets.bump_method_cooldown(date_str, direction, "SetupCount")

        trigger = _safe_num(payload.get("trigger_price"))
        stop = _safe_num(payload.get("stop_price"))
        tp1 = _safe_num(payload.get("tp1"))
        tp2 = _safe_num(payload.get("tp2"))
        tp3 = _safe_num(payload.get("tp3"))

        row = (cur.get("row") or {}).copy()
        row.update({
            "SignalID": signal_id,
            "Date": row.get("Date") or date_str,
            "Time_KSA": row.get("Time_KSA") or now.strftime("%H:%M:%S"),
            "Direction": direction,
            "State": TRACKING,
            "TriggerPrice": trigger if trigger is not None
                            else row.get("TriggerPrice", ""),
            "StopPrice": stop if stop is not None
                         else row.get("StopPrice", ""),
            "TP1": tp1 if tp1 is not None else row.get("TP1", ""),
            "TP2": tp2 if tp2 is not None else row.get("TP2", ""),
            "TP3": tp3 if tp3 is not None else row.get("TP3", ""),
            "TP1Hit": row.get("TP1Hit", "FALSE"),
            "TP2Hit": row.get("TP2Hit", "FALSE"),
            "TP3Hit": row.get("TP3Hit", "FALSE"),
            "InvalidatedAt": "",
            "StateUpdatedAt": now.strftime("%H:%M:%S"),
            "Source": "tradingview_webhook",
        })
        sheets.write_method_state(signal_id, row)
        sheets.bump_method_cooldown(date_str, direction, "LastEntryAt")

        self._dirs[direction] = {"state": TRACKING,
                                 "signal_id": signal_id, "row": row}

        try:
            sheets.append_recommendation({
                "rec_id": signal_id,
                "brief_type": "method_option",
                "ticker": ticker,
                "market": "US",
                "price_at_call": trigger,
                "source": "tradingview_webhook",
                "analysis": {
                    "action": f"{direction.upper()} (option-method)",
                    "confidence": "rule-based",
                    "action_urgent": True,
                    "one_line_plan": (
                        f"{direction.upper()} entry at {trigger}; "
                        f"stop {stop}, TP1 {tp1}, TP2 {tp2}, TP3 {tp3}"
                    ),
                    "action_price": trigger,
                    "risk_score": 3,
                    "halal_ai_signal": "",
                    "deep_dive": {
                        "stop_loss": stop,
                        "target": tp1,
                        "reasoning": (
                            f"TradingView Pine Script {direction} entry. "
                            f"Levels supplied by chart logic."
                        ),
                    },
                },
                "data_snapshot": {"news_count": 0, "top_news": []},
            })
        except Exception as e:
            log_event("WARN", "method",
                      f"Recommendations append failed for {signal_id}: {e}")

        levels = {"stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3}
        _send_entry_alert(ticker, direction, signal_id, trigger,
                          trigger, levels)
        log_event("INFO", "method",
                  f"{direction} ENTRY (webhook) {signal_id} @ {trigger}")
        return {"ok": True, "promoted": True, "signal_id": signal_id}

    def _webhook_tp_hit(self, direction: str, event: str,
                        payload: dict, ticker: str) -> dict:
        cur = self._dirs[direction]
        signal_id = cur.get("signal_id")
        if not signal_id:
            log_event("INFO", "method",
                      f"{direction} {event} ignored: no active signal")
            return {"ok": True, "noop": "no_active_signal"}

        tp_label = event.replace("_hit", "").upper()    # tp1_hit → TP1
        flag_field = f"{tp_label}Hit"
        row = (cur.get("row") or {}).copy()
        if str(row.get(flag_field, "FALSE")).upper() == "TRUE":
            log_event("INFO", "method",
                      f"{direction} {tp_label} already hit for {signal_id}")
            return {"ok": True, "noop": "already_hit"}

        tp_level = (_safe_num(payload.get(tp_label.lower()))
                    or _safe_num(row.get(tp_label)))

        # G.5.1 — defence in depth: reject TP hits whose level is on
        # the wrong side of the stored entry trigger. CALL TPs must be
        # > trigger; PUT TPs must be < trigger. Backstop against a bad
        # Pine deploy that emits sub-entry TPs (see method-alert-bugs.md
        # Root cause #1) without spamming the chat. If we can't compare
        # (either side missing), fall through to the existing path.
        trigger = _safe_num(row.get("TriggerPrice"))
        if tp_level is not None and trigger is not None:
            wrong_side = ((direction == "call" and tp_level <= trigger)
                          or (direction == "put" and tp_level >= trigger))
            if wrong_side:
                log_event("WARN", "method",
                          f"{direction} {tp_label} rejected: level "
                          f"{tp_level} on wrong side of entry "
                          f"{trigger} for {signal_id}")
                return {"ok": True, "noop": "wrong_side_tp",
                        "signal_id": signal_id}

        row[flag_field] = "TRUE"
        row["StateUpdatedAt"] = datetime.now(KSA_TZ).strftime("%H:%M:%S")
        sheets.write_method_state(signal_id, row)
        self._dirs[direction]["row"] = row

        _send_tp_hit_alert(ticker, direction, tp_label, tp_level)
        log_event("INFO", "method",
                  f"{direction} {tp_label} HIT (webhook) {signal_id}")
        return {"ok": True, "tp_hit": tp_label, "signal_id": signal_id}

    def _webhook_invalidated(self, direction: str, payload: dict,
                             ticker: str) -> dict:
        cur = self._dirs[direction]
        signal_id = cur.get("signal_id")
        if not signal_id:
            log_event("INFO", "method",
                      f"{direction} invalidated ignored: no active signal")
            return {"ok": True, "noop": "no_active_signal"}

        row = (cur.get("row") or {}).copy()
        now_str = datetime.now(KSA_TZ).strftime("%H:%M:%S")
        row["State"] = DONE
        row["InvalidatedAt"] = now_str
        row["StateUpdatedAt"] = now_str
        sheets.write_method_state(signal_id, row)

        stop = (_safe_num(payload.get("stop_price"))
                or _safe_num(row.get("StopPrice")))

        self._dirs[direction] = {"state": NO_SETUP, "signal_id": None,
                                 "row": None}
        _send_invalidated_alert(ticker, direction, signal_id, stop)
        log_event("INFO", "method",
                  f"{direction} INVALIDATED (webhook) {signal_id}")
        return {"ok": True, "invalidated": True, "signal_id": signal_id}

    def _webhook_setup_ended(self, direction: str, payload: dict,
                             ticker: str) -> dict:
        cur = self._dirs[direction]
        signal_id = cur.get("signal_id")
        if not signal_id:
            log_event("INFO", "method",
                      f"{direction} setup_ended ignored: no active signal")
            return {"ok": True, "noop": "no_active_signal"}

        row = (cur.get("row") or {}).copy()
        now_str = datetime.now(KSA_TZ).strftime("%H:%M:%S")
        row["State"] = DONE
        row["StateUpdatedAt"] = now_str
        sheets.write_method_state(signal_id, row)

        self._dirs[direction] = {"state": NO_SETUP, "signal_id": None,
                                 "row": None}
        _send_setup_end_alert(ticker, direction, signal_id)
        log_event("INFO", "method",
                  f"{direction} SETUP_END (webhook) {signal_id}")
        return {"ok": True, "setup_ended": True, "signal_id": signal_id}


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_tracker = None
_tracker_lock = threading.Lock()

# Serializes TradingView webhook events. webhook/app.py spawns one
# daemon thread per inbound alert, so entry/tp1_hit/tp2_hit can race
# inside the tracker — producing TP-hit Telegram messages BEFORE the
# entry message on a fast bar. Holding this lock for the full handler
# preserves Pine's emit order across sheet writes and Telegram sends.
_webhook_event_lock = threading.Lock()


def get_tracker() -> MethodSignalTracker:
    global _tracker
    if _tracker is not None:
        return _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = MethodSignalTracker()
            _tracker.rehydrate_from_sheets()
    return _tracker


def handle_webhook_event(payload: dict) -> dict:
    """Module-level entry point for the TradingView webhook. Routes to
    the singleton tracker. Caller (the webhook endpoint) runs this in a
    background thread so the HTTP response can return < 200ms.

    Serialized via _webhook_event_lock: concurrent threads block here
    and execute in arrival order. Pine emits entry → tp1_hit → tp2_hit
    on a single bar; without this gate the Telegram sends could land
    out of order, which is what the May 11 2026 alert sequence showed.
    """
    if not isinstance(payload, dict):
        log_event("WARN", "method", "webhook payload not a dict")
        return {"ok": False, "error": "payload not dict"}
    with _webhook_event_lock:
        return get_tracker().handle_webhook_event(payload)


def get_market_direction() -> tuple:
    """Derive a one-line market-direction summary from the tracker's
    per-direction state. Returns (emoji_label, descriptor) — e.g.
    ('📈 BULLISH', 'CALL setup active'). NEUTRAL when both directions
    are idle (NO_SETUP/DONE).

    Used by /method status and the /menu Method dashboard so they
    can show the same one-liner."""
    try:
        snap = get_tracker().state_snapshot() or {}
    except Exception as e:
        log_event("WARN", "method",
                  f"market_direction tracker read failed: {e}")
        return ("⚪ NEUTRAL", "no setup")
    active = (PRE_SIGNAL, TRACKING)
    call_state = (snap.get("call") or {}).get("state", NO_SETUP)
    put_state = (snap.get("put") or {}).get("state", NO_SETUP)
    if call_state in active:
        return ("📈 BULLISH", "CALL setup active")
    if put_state in active:
        return ("📉 BEARISH", "PUT setup active")
    return ("⚪ NEUTRAL", "no setup")


def get_today_counters() -> dict:
    """Tally today's MethodSignals activity per direction. Used by the
    /menu Method dashboard.

    Returns:
        {
          "call": {"setups": N, "entries": N, "tp1_hits": N,
                   "tp2_hits": N, "tp3_hits": N, "invalidations": N},
          "put":  {... same shape ...},
        }

    Reads up to 200 most-recent rows (bounded by daily cap of
    METHOD_MAX_DAILY_SIGNALS × 2 directions) and filters by today's
    Date. Counters use the row state, not the strict event sequence —
    e.g. a row that reached TRACKING is counted as both a setup and
    an entry."""
    today = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    rows = sheets.read_method_signals(limit=200) or []
    out = {"call": {"setups": 0, "entries": 0,
                    "tp1_hits": 0, "tp2_hits": 0, "tp3_hits": 0,
                    "invalidations": 0},
           "put":  {"setups": 0, "entries": 0,
                    "tp1_hits": 0, "tp2_hits": 0, "tp3_hits": 0,
                    "invalidations": 0}}
    for r in rows:
        if str(r.get("Date", "")).strip() != today:
            continue
        d = str(r.get("Direction", "")).strip().lower()
        if d not in out:
            continue
        out[d]["setups"] += 1
        state = str(r.get("State", "")).strip().upper()
        # Anything that reached TRACKING (now or later) was an entry —
        # a row currently in DONE state may have been TRACKING before
        # being closed. Use TP/Invalidation flags as proxy for "did
        # this signal advance past PRE_SIGNAL".
        tp1_hit = str(r.get("TP1Hit", "")).strip().upper() == "TRUE"
        tp2_hit = str(r.get("TP2Hit", "")).strip().upper() == "TRUE"
        tp3_hit = str(r.get("TP3Hit", "")).strip().upper() == "TRUE"
        invalidated = bool(str(r.get("InvalidatedAt", "")).strip())
        if state == "TRACKING" or tp1_hit or tp2_hit or tp3_hit \
                or invalidated:
            out[d]["entries"] += 1
        if tp1_hit: out[d]["tp1_hits"] += 1
        if tp2_hit: out[d]["tp2_hits"] += 1
        if tp3_hit: out[d]["tp3_hits"] += 1
        if invalidated: out[d]["invalidations"] += 1
    return out


def get_webhook_health() -> dict:
    """Shared by /method debug and the /menu Method dashboard. Returns
    a dict with last-webhook + last-alert timestamps, the 24h verdict,
    and the current toggle / secret state. Single source of truth so
    the two surfaces never drift."""
    from datetime import timedelta
    from config import TRADINGVIEW_WEBHOOK_SECRET
    from core import method_runner

    secret_set = bool(TRADINGVIEW_WEBHOOK_SECRET)
    method_on = method_runner.is_method_enabled()

    last_webhook_row = sheets.get_last_log_row(module="webhook_tv") or {}
    last_method_row = sheets.get_last_log_row(module="method") or {}

    def _ts(row):
        return str(row.get("Timestamp", "") or "").strip()

    ts_w = _ts(last_webhook_row)
    ts_m = _ts(last_method_row)
    last_received = max(ts_w, ts_m) if (ts_w or ts_m) else ""

    rows = sheets.read_method_signals(limit=1) or []
    last_signal = rows[0] if rows else {}
    last_signal_at = ""
    if last_signal:
        d = str(last_signal.get("Date", "") or "").strip()
        t = str(last_signal.get("Time_KSA", "") or "").strip()
        last_signal_at = f"{d} {t}".strip()
    last_signal_id = (str(last_signal.get("SignalID", "") or "").strip()
                      or "")
    last_signal_dir = (str(last_signal.get("Direction", "") or "")
                       .strip().upper())
    last_signal_trigger = str(last_signal.get("TriggerPrice", "")
                              or "").strip()

    healthy_24h = False
    age_str = ""
    if last_received:
        try:
            ts = datetime.strptime(
                last_received[:19],
                "%Y-%m-%d %H:%M:%S").replace(tzinfo=KSA_TZ)
            delta = datetime.now(KSA_TZ) - ts
            healthy_24h = delta < timedelta(hours=24)
            mins = int(delta.total_seconds() // 60)
            if mins < 60:
                age_str = f"{mins}m ago"
            elif mins < 1440:
                age_str = f"{mins // 60}h ago"
            else:
                age_str = f"{mins // 1440}d ago"
        except (ValueError, TypeError):
            pass

    return {
        "secret_set": secret_set,
        "method_enabled": method_on,
        "last_received": last_received,
        "last_received_age": age_str,
        "healthy_24h": healthy_24h,
        "last_signal_at": last_signal_at,
        "last_signal_id": last_signal_id,
        "last_signal_dir": last_signal_dir,
        "last_signal_trigger": last_signal_trigger,
    }


# ---------------------------------------------------------------------------
# Internal helpers — levels & primitives
# ---------------------------------------------------------------------------

def _safe_num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _last_close(bars):
    if not bars:
        return None
    last = bars[-1]
    if not isinstance(last, dict):
        return None
    return _safe_num(last.get("close"))


def _structural_levels(direction: str, bars_5m: list,
                        entry_price=None) -> dict:
    """Compute swing-low / swing-high and the 5m fractal that anchors
    the prior consolidation, then derive TP1 + stop.

    Phase G.2 simplification — uses the most recent 5m fractal of the
    relevant kind plus the structural extreme over the last 30 5m bars.
    The full friend's-method spec wants the fractal *before* the swing;
    most of the time the most-recent fractal lines up. Document this
    when wiring VWAP-based TP2/TP3 in Phase G.3.

    Returns: {"stop": float|None, "tp1": float|None,
              "fractal_level": float|None, "swing_level": float|None}
    """
    out = {"stop": None, "tp1": None, "fractal_level": None,
           "swing_level": None}
    if not bars_5m:
        return out

    if direction == "call":
        fractal = method_option.detect_fractal_high(bars_5m)
        fractal_level = _safe_num((fractal or {}).get("high"))
        # Swing low = lowest 5m low in the lookback window
        swing = None
        for b in bars_5m[-_STRUCTURAL_LOOKBACK_5M:]:
            if not isinstance(b, dict):
                continue
            l = _safe_num(b.get("low"))
            if l is None:
                continue
            if swing is None or l < swing:
                swing = l
        out["fractal_level"] = fractal_level
        out["swing_level"] = swing
        if swing is not None:
            out["stop"] = swing
        if fractal_level is not None and swing is not None and entry_price is not None:
            rng = fractal_level - swing
            if rng > 0:
                out["tp1"] = round(float(entry_price) + rng, 4)
        return out

    # put
    fractal = method_option.detect_fractal_low(bars_5m)
    fractal_level = _safe_num((fractal or {}).get("low"))
    swing = None
    for b in bars_5m[-_STRUCTURAL_LOOKBACK_5M:]:
        if not isinstance(b, dict):
            continue
        h = _safe_num(b.get("high"))
        if h is None:
            continue
        if swing is None or h > swing:
            swing = h
    out["fractal_level"] = fractal_level
    out["swing_level"] = swing
    if swing is not None:
        out["stop"] = swing
    if fractal_level is not None and swing is not None and entry_price is not None:
        rng = swing - fractal_level
        if rng > 0:
            out["tp1"] = round(float(entry_price) - rng, 4)
    return out


def _make_signal_id(direction: str) -> str:
    now = datetime.now(KSA_TZ)
    return (f"{now.strftime('%Y%m%d-%H%M')}-METHOD-"
            f"{direction.upper()}")


def _ticker_from_payload(payload: dict) -> str:
    """Extract a display ticker from the payload. TradingView sends
    'CAPITALCOM:US500' style symbols — strip the exchange prefix.
    Falls back to METHOD_TICKER if symbol is missing."""
    sym = str(payload.get("symbol", "") or "").strip()
    if sym:
        if ":" in sym:
            sym = sym.split(":", 1)[1]
        return sym or METHOD_TICKER
    return METHOD_TICKER


# ---------------------------------------------------------------------------
# Telegram alerts
# ---------------------------------------------------------------------------

def _fmt(v, dp=2):
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):.{dp}f}"
    except (ValueError, TypeError):
        return str(v)


def _send_pre_signal_alert(ticker, direction, trigger_level, levels):
    side = direction.upper()
    op = "above" if direction == "call" else "below"

    # G.5.1 — Pine emits trigger_price=null when the 1m fractal hasn't
    # printed yet on a fresh setup. Render "waiting for level" rather
    # than "Watching for 5m close below —", which reads as broken.
    has_level = (isinstance(trigger_level, (int, float))
                 and not isinstance(trigger_level, bool)
                 and math.isfinite(float(trigger_level)))

    if has_level:
        watch_line = (f"Watching for 5m close {op} "
                      f"<code>{_fmt(trigger_level)}</code>")
    else:
        watch_line = ("Watching for next 1m fractal break "
                      "(waiting for level).")

    text = (
        f"⚡ <b>PRE-SIGNAL — {ticker} {side}</b>\n"
        f"Setup forming: index fractal break (G.5.0)\n"
        f"{watch_line}\n"
        f"Stand by for entry signal."
    )
    try:
        telegram_client.send_message(text)
    except Exception as e:
        log_event("WARN", "method", f"pre-signal Telegram failed: {e}")


def _send_entry_alert(ticker, direction, signal_id, entry_price,
                      trigger_level, levels):
    side = direction.upper()
    op = "above" if direction == "call" else "below"
    keyboard = [[{
        "text": "🛑 Cancel tracking",
        "callback_data": f"method_cancel:{signal_id}",
    }]]

    tp2 = levels.get("tp2")
    tp3 = levels.get("tp3")
    tp2_line = (f"TP2: <code>{_fmt(tp2)}</code>"
                if tp2 not in (None, "")
                else "TP2: VWAP +2σ <i>(not available)</i>")
    tp3_line = (f"TP3: <code>{_fmt(tp3)}</code>"
                if tp3 not in (None, "")
                else "TP3: VWAP +3σ <i>(not available)</i>")

    text = (
        f"🟢 <b>ENTRY SIGNAL — {ticker} {side}</b>\n"
        f"Trigger: <code>{_fmt(entry_price)}</code> "
        f"(5m close {op} <code>{_fmt(trigger_level)}</code>)\n"
        f"Stop: <code>{_fmt(levels.get('stop'))}</code>\n"
        f"TP1: <code>{_fmt(levels.get('tp1'))}</code>\n"
        f"{tp2_line}\n"
        f"{tp3_line}\n"
        f"<i>SignalID: {signal_id}</i>"
    )
    try:
        msg_id = telegram_client.send_message(text, inline_keyboard=keyboard)
        if msg_id:
            try:
                sheets.record_message_recids(msg_id, "method_option",
                                             [signal_id])
            except Exception as e:
                log_event("WARN", "method",
                          f"MessageMap write failed for entry alert: {e}")
    except Exception as e:
        log_event("WARN", "method", f"entry alert Telegram failed: {e}")


def _send_tp_hit_alert(ticker, direction, tp_label, tp_level):
    text = (
        f"🎯 <b>{tp_label} HIT — {ticker} {direction.upper()}</b>\n"
        f"Price reached <code>{_fmt(tp_level)}</code>. "
        f"Consider taking profit."
    )
    try:
        telegram_client.send_message(text)
    except Exception as e:
        log_event("WARN", "method", f"TP-hit Telegram failed: {e}")


def _send_invalidated_alert(ticker, direction, signal_id, stop_level):
    side_word = "below" if direction == "call" else "above"
    text = (
        f"❌ <b>INVALIDATED — {ticker} {direction.upper()}</b>\n"
        f"Price closed {side_word} stop at "
        f"<code>{_fmt(stop_level)}</code>. Signal cancelled.\n"
        f"<i>SignalID: {signal_id}</i>"
    )
    try:
        telegram_client.send_message(text)
    except Exception as e:
        log_event("WARN", "method", f"invalidated Telegram failed: {e}")


def _send_setup_end_alert(ticker, direction, signal_id):
    text = (
        f"🔚 <b>SETUP ENDED — {ticker} {direction.upper()}</b>\n"
        f"Trend or MACD flipped — tracking stopped.\n"
        f"<i>SignalID: {signal_id}</i>"
    )
    try:
        telegram_client.send_message(text)
    except Exception as e:
        log_event("WARN", "method", f"setup-end Telegram failed: {e}")
