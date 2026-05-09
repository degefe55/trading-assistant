"""
Pre-fix snapshot of every Google Sheet tab.

Reads each tab listed in core.sheets.SCHEMAS and writes it as a CSV
under backups/YYYY-MM-DD/<tab>.csv (date in KSA timezone). Failures
on one tab are logged but don't stop the others.

Run on Railway:
  railway run python scripts/backup_state.py

The backups/ folder is gitignored — see .gitignore. Pull the CSVs off
your machine yourself if you want them archived elsewhere.
"""
import csv
import os
import sys
from datetime import datetime

# Make repo-root importable when run from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import KSA_TZ                          # noqa: E402
from core import sheets                            # noqa: E402


def _today_dir() -> str:
    """backups/YYYY-MM-DD/ — created if missing. KSA date so the folder
    name matches the bot's own day boundary (briefs are KSA-time)."""
    date_str = datetime.now(KSA_TZ).strftime("%Y-%m-%d")
    out_dir = os.path.join(_ROOT, "backups", date_str)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _backup_tab(ss, tab_name: str, out_dir: str) -> tuple:
    """Read one tab and write its raw values to <tab_name>.csv.

    Uses get_all_values() (single API call, cheap, returns 2D list of
    raw cells) rather than get_all_records() — faster on large tabs
    like Logs and doesn't require the headers to round-trip cleanly.

    Returns (rows_written, csv_path) on success, or (None, error_str).
    """
    csv_path = os.path.join(out_dir, f"{tab_name}.csv")
    try:
        ws = ss.worksheet(tab_name)
    except Exception as e:
        return (None, f"worksheet open failed: {e}")
    try:
        values = ws.get_all_values()
    except Exception as e:
        return (None, f"get_all_values failed: {e}")
    try:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            for row in values:
                w.writerow(row)
    except Exception as e:
        return (None, f"csv write failed: {e}")
    # Subtract header row from the count so the printed number matches
    # what a human would call "rows in the tab".
    data_rows = max(0, len(values) - 1)
    return (data_rows, csv_path)


def main():
    ss = sheets._get_spreadsheet()
    if ss is None:
        print("[FATAL] Could not open spreadsheet. Check that "
              "GOOGLE_SA_JSON and GOOGLE_SHEET_URL are set in the "
              "environment (no need to print them — just confirm "
              "they exist).", file=sys.stderr)
        sys.exit(2)

    out_dir = _today_dir()
    print(f"Backup directory: {out_dir}")
    print(f"Started: {datetime.now(KSA_TZ).strftime('%Y-%m-%d %H:%M:%S KSA')}")
    print("-" * 72)

    failures = 0
    successes = 0
    for tab_name in sheets.SCHEMAS.keys():
        rows, info = _backup_tab(ss, tab_name, out_dir)
        if rows is None:
            print(f"tab={tab_name} ERROR: {info}")
            failures += 1
            continue
        print(f"tab={tab_name} rows={rows} saved={info}")
        successes += 1

    print("-" * 72)
    print(f"Done: {successes} ok, {failures} failed.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
