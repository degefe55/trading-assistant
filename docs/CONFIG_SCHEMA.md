# Config Tab Schema

> **Last audited: 2026-05-08.**  Insurance: if the Google Sheet is ever
> corrupted, dropped, or migrated, this file + the codebase = full
> recovery of every runtime-configurable setting.
>
> To regenerate: re-run the audit prompt on Claude Code (search for
> `read_config` / `write_config` / `is_paused` calls across `core/`,
> `webhook/`, `main.py`).

The Config tab is a flat key/value store on a Google Sheet worksheet
named **Config**, with three columns: `Setting`, `Value`, `Description`.
Every key listed below is a `Setting` row. Every read goes through
`core.sheets.read_config()` (returns `{Setting: Value}` dict). Every
write goes through `core.sheets.write_config(setting, value)` (upserts).

The Config tab also stores the **per-market watchlists** (`WATCHLIST`,
`WATCHLIST_SA`) as comma- or semicolon-separated ticker lists in the
Value cell. Focus is **not** in Config — it lives in its own `Focus`
tab with one row per ticker (see "Adjacent storage" at the bottom).

---

## Active runtime keys

These are read by the bot during operation. Sheet wins; if the row is
missing, code falls back to the env-var or Python constant noted in the
"Default" column.

### Toggles (true/false)

| Key | Default | Read by | Written by | Notes |
|---|---|---|---|---|
| `PAUSED` | `false` (env: none — file fallback only) | `core.sheets.is_paused()` (used by `commands._is_paused`, `method_runner._is_paused`, `watcher._is_paused`, `main.py`) | `commands._cmd_pause` / `_cmd_resume`; `menu._flip_toggle("tp")`; one-shot migration in `webhook/app.py` boot | Phase A — replaced the legacy `/tmp/bot_paused.txt` file. When `true`, scheduled briefs / watcher / method polling all silently skip. |
| `WATCHER_ENABLED` | env `WATCHER_ENABLED` (defaults to `true`) | `watcher._settings()` via `_resolve()`; `menu._watcher_enabled()` | `commands._cmd_watcher` ("on"/"off"); `menu._flip_toggle("tw")` | Per-tick gate for the always-on price/news watcher. |
| `METHOD_ENABLED` | env `METHOD_ENABLED` (defaults to `false`) | `method_runner.is_method_enabled()` (consulted by `webhook/app.py /webhook/tradingview` and the dormant polling tick); `menu._method_enabled()`; `commands._cmd_method_status` | `commands._cmd_method` ("on"/"off"); `menu._flip_toggle("tm")`; `menu.method_action("on"/"off")` | Phase A gates the TradingView webhook on this flag (returns 403 `method_disabled` when false). |
| `DIAGNOSTIC_AGENT_ENABLED` | env `DIAGNOSTIC_AGENT_ENABLED` (defaults to `true`) | `log_analyst.is_diagnostic_enabled()` (consulted by every 30-min cron tick); `menu._diagnostic_enabled()` | `menu._flip_toggle("td")` | When `false`, the diagnostic Haiku agent skips its scheduled scans. Manual `/diagnose` ignores this flag. |

### Brief schedule (HH:MM, KSA)

| Key | Default | Read by | Written by | Notes |
|---|---|---|---|---|
| `BRIEF_TIME_PREMARKET` | `15:30` | `webhook/app.py _load_times_from_config` | `commands._cmd_settime`; `menu.apply_schedule_change` | US Mon–Fri |
| `BRIEF_TIME_MIDSESSION` | `19:30` | same | same | US Mon–Fri |
| `BRIEF_TIME_PRECLOSE` | `22:30` | same | same | US Mon–Fri |
| `BRIEF_TIME_EOD` | `23:00` | same | same | US Mon–Fri |
| `BRIEF_TIME_PREMARKET_SA` | `09:30` | same (Phase A fix) | same (Phase A fix) | Saudi Sun–Thu — pre-Phase-A this row was written by `/settime` but never read back |
| `BRIEF_TIME_MIDSESSION_SA` | `12:30` | same | same | Saudi Sun–Thu |
| `BRIEF_TIME_PRECLOSE_SA` | `14:30` | same | same | Saudi Sun–Thu |
| `BRIEF_TIME_EOD_SA` | `15:25` | same | same | Saudi Sun–Thu — intentionally 5 min before US premarket to avoid overlap |

`Value` must be `HH:MM` zero-padded (e.g. `09:30`, `22:45`). Validated by `_validate_hhmm` in both `commands.py` and `webhook/app.py`. Invalid values are silently ignored and the default applies.

### Watcher tuning

| Key | Default | Read by | Written by | Notes |
|---|---|---|---|---|
| `WATCHER_PRICE_INTERVAL_MIN` | env `WATCHER_PRICE_INTERVAL_MIN` (defaults to `30`) | `webhook/app.py _load_watcher_intervals_from_config` | (no Telegram surface — edit Sheet directly) | Clamped to `[1, 1440]` |
| `WATCHER_NEWS_INTERVAL_MIN` | env `WATCHER_NEWS_INTERVAL_MIN` (defaults to `60`) | same | same | Clamped to `[1, 1440]` |
| `WATCHER_DAILY_ALERT_CAP` | env `WATCHER_DAILY_ALERT_CAP` (defaults to `3`) | `watcher._settings()` | (no Telegram surface) | Per-ticker daily alert ceiling |
| `WATCHER_INCLUDE_WATCHLIST` | env `WATCHER_INCLUDE_WATCHLIST` (defaults to `false`) | `watcher._settings()` | (no Telegram surface) | When `true`, watcher analyzes watchlist tickers in addition to focus + positions |

### Watchlists (comma/semicolon-separated ticker lists)

| Key | Default | Read by | Written by | Notes |
|---|---|---|---|---|
| `WATCHLIST` | `markets.us.config.DEFAULT_WATCHLIST` | `core.sheets.read_watchlist(market="US")` | `core.sheets.write_watchlist(..., market="US")` (called from `commands._cmd_watch` / `_cmd_unwatch`; `menu.watchlist_action`) | US watchlist — comma-separated alphanumeric tickers, e.g. `AAPL,NVDA,META`. The legacy key with no suffix is the US list. |
| `WATCHLIST_SA` | `markets.saudi.config.DEFAULT_WATCHLIST` | same | same (writes with `; ` separator post-Bug-1 fix) | Saudi watchlist — semicolon-separated 4-digit Tadawul codes, e.g. `2222; 1321`. The semicolon avoids Google Sheets auto-interpreting `2222,1321` as the number 22,221,321. Reader accepts comma, semicolon, pipe, or whitespace. |

---

## Seeded-on-init keys (advisory; not currently read at runtime)

These rows are written when `sheets.initialize_sheet()` runs for the
first time on a fresh Sheet (`core/sheets.py:DEFAULT_CONFIG_ROWS`).
They mirror the `DEFAULT_RULES` dict in `config.py` so the user can
see and edit them — but the runtime code today reads `DEFAULT_RULES`
or other env vars, **not** these Sheet rows. If you change the Sheet,
nothing changes in behavior; document this gotcha for future-you.

| Key | Default | Status | Notes |
|---|---|---|---|
| `MAX_POSITION_PCT` | `25` | seeded only | Mirrors `config.DEFAULT_RULES["max_position_pct"]` |
| `MAX_SECTOR_PCT` | `60` | seeded only | Mirrors `config.DEFAULT_RULES["max_sector_pct"]` |
| `MAX_OPEN_POSITIONS` | `5` | seeded only | Mirrors `config.DEFAULT_RULES["max_open_positions"]` |
| `EARNINGS_BUFFER_DAYS` | `3` | seeded only | Mirrors `config.DEFAULT_RULES["earnings_buffer_days"]` |
| `RISK_TOLERANCE` | `medium` | seeded only | Free-form text; not consulted by code |
| `USD_TO_SAR` | `3.75` | seeded only — code reads `config.USD_TO_SAR` env var | Edit the Railway env var to actually change behavior |

**If you want to make any of these live, route the read through `sheets.read_config()` and add a fallback to `config.DEFAULT_RULES`.**

---

## Adjacent storage (not Config tab)

For completeness, the bot also stores runtime state in these tabs:

| Tab | Schema | Used by | Notes |
|---|---|---|---|
| `Logs` | `Timestamp, Level, Module, Event, Details, Tokens, Cost_USD` | `sheets.append_logs` (write) / `sheets.read_recent_logs` (read) / `log_analyst` / `sheets.get_last_log_row` | Auto-trimmed to `MAX_LOG_ROWS` (default 50000) every 6h |
| `Focus` | `Ticker, Market, DateAdded, CustomNotes` | `sheets.add_focus` / `sheets.remove_focus` / `sheets.read_focus` | Per-market focus list; max 3 per market, oldest auto-dropped |
| `Positions` | `Ticker, Market, Shares, AvgCost_USD, AvgCost_SAR, StopLoss, Target, DateOpened, Sector, HalalStatus` | `sheets.read_positions` / `sheets.update_position` etc. | Trade-state truth |
| `TradeLog` | `Date, Time, Ticker, Market, Action, Shares, Price_USD, Price_SAR, Reason, LinkedRecID, PnL_USD, PnL_SAR, RunningTotal_USD, RunningTotal_SAR` | `sheets.append_trade` (called by `trades.record_buy`/`record_sell`) | Append-only audit log |
| `Recommendations` | 21 cols incl. `RecID, Date, Time_KSA, BriefType, Ticker, Market, Action, …, RawJSON` | `sheets.append_recommendation` / `sheets.read_recommendation` | Every recommendation the bot makes (briefs, method-option signals) |
| `MessageMap` | `MessageID, Date, Time_KSA, BriefType, RecIDs` | `sheets.record_message_recids` / `sheets.get_recids_for_message` | Maps Telegram message_id → which RecIDs the message covered, for threaded replies |
| `WatcherCooldown` | `Ticker, Date, AlertCount` | `sheets.read_cooldowns_for_date` / `sheets.bump_cooldown` | Per-ticker daily alert counter |
| `MethodSignals` | `SignalID, Date, Time_KSA, Direction, State, TriggerPrice, StopPrice, TP1, TP2, TP3, TP1Hit, TP2Hit, TP3Hit, InvalidatedAt, StateUpdatedAt, Source` | `sheets.write_method_state` / `sheets.read_method_signals` / `sheets.read_active_method_signals` | One row per option-method signal lifecycle, upserted on each state transition |
| `MethodCooldown` | `Date, Direction, LastPreSignalAt, LastEntryAt, SetupCount` | `sheets.read_method_cooldown` / `sheets.bump_method_cooldown` / `sheets.reset_method_cooldown` | Per-direction daily setup counter (informational; no enforced cap as of G.5.10) |
| `Halal_Approved` | `Ticker, Market, Source, LastVerified` | (read by Saudi screening, currently advisory) | Manually maintained whitelist |
| `Patterns` / `Reports` | misc analysis outputs | weekly review prompts | Lower-priority |

---

## Recovery procedure

If the Sheet is gone or corrupted:

1. Create a new spreadsheet, share with the service account from
   `GOOGLE_SA_JSON`, set `GOOGLE_SHEET_URL` env var on Railway to the
   new URL.
2. Boot the bot — `sheets.initialize_sheet()` runs automatically on
   first startup and creates every tab listed under "Adjacent storage"
   with the right headers, plus the seeded Config rows.
3. Re-populate Config rows that aren't seeded — the toggles
   (`PAUSED`, `WATCHER_ENABLED`, `METHOD_ENABLED`,
   `DIAGNOSTIC_AGENT_ENABLED`) and any `BRIEF_TIME_*` overrides you
   want. Either via the bot:
   - `/pause` / `/resume` for `PAUSED`
   - `/watcher on|off` for `WATCHER_ENABLED`
   - `/method on|off` for `METHOD_ENABLED`
   - `/menu` → ⚙️ Toggles for `DIAGNOSTIC_AGENT_ENABLED` (also via
     env var)
   - `/settime BRIEF HH:MM` for any custom schedules
   - `/watch SYMBOL` to rebuild watchlists
   - `/focus SYMBOL` to rebuild focus list
4. Positions, TradeLog, Recommendations, and the method-signal history
   are gone if you don't have a Sheet backup. Position state can be
   re-entered via `/buy` for each open position.

The single environment input that holds the credentials needed for
recovery is `GOOGLE_SA_JSON` (service-account JSON for write access).
Without that, no recovery is possible.
