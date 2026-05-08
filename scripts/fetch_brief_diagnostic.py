"""
Brief-drought diagnostic. Read-only Sheet/Logs dump.

Prints five sections to stdout in plain text (no markdown):

  A. Last 10 rows of the Recommendations tab.
  B. Action breakdown over the last 30 Recommendations rows.
  C. Last 5 rows of the Reports tab.
  D. WARN/ERROR log rows (module in {brief, scout, analyst}) in the
     last 7 days.
  E. All log rows for module='scout' in the last 7 days.

Reuses core.sheets helpers where they exist (read_recent_logs). The
Recommendations and Reports tabs have no public reader yet, so we go
through sheets._get_spreadsheet() directly with get_all_records() —
acceptable for a one-shot diagnostic, not something to take a
dependency on.

Run on Railway:
  railway run -s trading-assistant python scripts/fetch_brief_diagnostic.py

Or in a Railway shell session for the trading-assistant service:
  python scripts/fetch_brief_diagnostic.py
"""
import os
import sys
from datetime import datetime, timedelta

# Make repo-root importable when run from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import KSA_TZ                          # noqa: E402
from core import sheets                            # noqa: E402


SECTION_BAR = "=" * 72
SUB_BAR = "-" * 72


def _read_tab_records(tab_name: str) -> list:
    """get_all_records() for a single tab, or [] on any failure.

    No public reader exists for Recommendations or Reports — fine for a
    read-only diagnostic; not a pattern to copy into the live bot."""
    ss = sheets._get_spreadsheet()
    if ss is None:
        print(f"[ERROR] Could not open spreadsheet for tab {tab_name!r}. "
              f"Check GOOGLE_SA_JSON / GOOGLE_SHEET_URL env vars.",
              file=sys.stderr)
        return []
    try:
        ws = ss.worksheet(tab_name)
        return ws.get_all_records()
    except Exception as e:
        print(f"[ERROR] Reading tab {tab_name!r} failed: {e}",
              file=sys.stderr)
        return []


def _trunc(s, n: int) -> str:
    s = "" if s is None else str(s)
    s = s.replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _section_header(letter: str, title: str):
    print()
    print(SECTION_BAR)
    print(f"SECTION {letter} — {title}")
    print(SECTION_BAR)


def section_a_last_10_recs():
    _section_header("A", "Last 10 rows of Recommendations tab")
    rows = _read_tab_records("Recommendations")
    if not rows:
        print("(no Recommendations rows)")
        return
    tail = rows[-10:]
    for i, r in enumerate(tail, start=1):
        date = r.get("Date", "")
        ticker = r.get("Ticker", "")
        action = r.get("Action", "")
        confidence = r.get("Confidence", "")
        reasoning = _trunc(r.get("Reasoning", ""), 250)
        print(f"Row {i} | {date} | {ticker} | {action} | "
              f"{confidence} | {reasoning}")


def section_b_action_breakdown_30():
    _section_header("B", "Action breakdown over last 30 Recommendations rows")
    rows = _read_tab_records("Recommendations")
    if not rows:
        print("(no Recommendations rows)")
        return
    tail = rows[-30:]
    counts = {"BUY": 0, "HOLD": 0, "WAIT": 0, "SELL": 0, "OTHER": 0}
    for r in tail:
        a = (r.get("Action") or "").strip().upper()
        if a in counts:
            counts[a] += 1
        else:
            counts["OTHER"] += 1
    print(
        f"Action breakdown last {len(tail)} rows: "
        f"BUY={counts['BUY']}, HOLD={counts['HOLD']}, "
        f"WAIT={counts['WAIT']}, SELL={counts['SELL']}, "
        f"OTHER={counts['OTHER']}"
    )


def section_c_last_5_reports():
    _section_header("C", "Last 5 rows of Reports tab")
    rows = _read_tab_records("Reports")
    if not rows:
        print("(no Reports rows)")
        return
    tail = rows[-5:]
    # Reports schema: Date | Ticker | RecID | CallQuality |
    #   WhatHappened | KeyFactor | LessonLearned
    # The user asked for "BriefType (or CallQuality)" — there is no
    # BriefType column on Reports; CallQuality is the closest fit.
    for i, r in enumerate(tail, start=1):
        date = r.get("Date", "")
        quality = r.get("CallQuality", "")
        ticker = r.get("Ticker", "")
        what = _trunc(r.get("WhatHappened", ""), 300)
        print(f"Row {i} | {date} | CallQuality={quality} | "
              f"Ticker={ticker} | {what}")


def _seven_day_cutoff_str() -> str:
    """YYYY-MM-DD seven days ago in KSA timezone — first 10 chars of
    Logs.Timestamp are 'YYYY-MM-DD' (see core/logger.py:33), so a string
    compare is enough."""
    cutoff = datetime.now(KSA_TZ) - timedelta(days=7)
    return cutoff.strftime("%Y-%m-%d")


def _recent_logs_window(limit: int = 5000) -> list:
    """Pull the most-recent N rows of the Logs tab via the existing
    helper. 5000 is generous for 7 days of activity but cheap on the
    wire — read_recent_logs() does a single ranged fetch."""
    return sheets.read_recent_logs(limit=limit)


def section_d_warn_error_brief_scout_analyst():
    _section_header(
        "D",
        "WARN/ERROR logs in last 7 days for modules brief/scout/analyst",
    )
    rows = _recent_logs_window()
    if not rows:
        print("(no log rows returned)")
        return
    cutoff = _seven_day_cutoff_str()
    target_modules = ("brief", "scout", "analyst")
    matches = []
    for r in rows:
        ts = (r.get("Timestamp") or "").strip()
        if not ts or ts[:10] < cutoff:
            continue
        level = (r.get("Level") or "").strip().upper()
        if level not in ("WARN", "ERROR"):
            continue
        module = (r.get("Module") or "").strip().lower()
        if not any(t in module for t in target_modules):
            continue
        matches.append(r)
    if not matches:
        print("No WARN/ERROR rows in 7 days for brief/scout/analyst modules.")
        return
    print(f"({len(matches)} rows)")
    for r in matches:
        ts = (r.get("Timestamp") or "")[:10]
        module = r.get("Module", "")
        level = r.get("Level", "")
        msg = _trunc(r.get("Event", ""), 200)
        print(f"{ts} | {module} | {level} | {msg}")


def section_e_scout_all_levels():
    _section_header("E", "All scout-module logs in last 7 days")
    rows = _recent_logs_window()
    if not rows:
        print("(no log rows returned)")
        return
    cutoff = _seven_day_cutoff_str()
    matches = []
    for r in rows:
        ts = (r.get("Timestamp") or "").strip()
        if not ts or ts[:10] < cutoff:
            continue
        module = (r.get("Module") or "").strip().lower()
        if module != "scout":
            continue
        matches.append(r)
    if not matches:
        print("(no scout-module log rows in last 7 days)")
        return
    print(f"({len(matches)} rows)")
    for r in matches:
        ts = (r.get("Timestamp") or "")[:16]
        level = r.get("Level", "")
        msg = _trunc(r.get("Event", ""), 200)
        print(f"{ts} | scout | {level} | {msg}")


def main():
    print("Brief drought diagnostic — generated", datetime.now(KSA_TZ)
          .strftime("%Y-%m-%d %H:%M KSA"))
    print(SUB_BAR)
    section_a_last_10_recs()
    section_b_action_breakdown_30()
    section_c_last_5_reports()
    section_d_warn_error_brief_scout_analyst()
    section_e_scout_all_levels()
    print()
    print(SUB_BAR)
    print("(end of diagnostic)")


if __name__ == "__main__":
    main()
