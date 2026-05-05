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
                            f"Trend 5m {direction}, MACD 10m+1m aligned, "
                            f"1m close through {trigger_level}. "
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


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_tracker = None
_tracker_lock = threading.Lock()


def get_tracker() -> MethodSignalTracker:
    global _tracker
    if _tracker is not None:
        return _tracker
    with _tracker_lock:
        if _tracker is None:
            _tracker = MethodSignalTracker()
            _tracker.rehydrate_from_sheets()
    return _tracker


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
    text = (
        f"⚡ <b>PRE-SIGNAL — {ticker} {side}</b>\n"
        f"Setup forming: trend ✅ MACD 10m ✅ MACD 1m ✅\n"
        f"Watching for 1m close {op} <code>{_fmt(trigger_level)}</code>\n"
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
    text = (
        f"🟢 <b>ENTRY SIGNAL — {ticker} {side}</b>\n"
        f"Trigger: <code>{_fmt(entry_price)}</code> "
        f"(1m close {op} <code>{_fmt(trigger_level)}</code>)\n"
        f"Stop: <code>{_fmt(levels.get('stop'))}</code>\n"
        f"TP1: <code>{_fmt(levels.get('tp1'))}</code> "
        f"(1:1 measured move)\n"
        f"TP2: VWAP +2σ <i>(not available yet)</i>\n"
        f"TP3: VWAP +3σ <i>(not available yet)</i>\n"
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
