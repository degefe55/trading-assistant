# Trading Assistant — Phase A

Personal trading bot for US equities via Awaed (Albilad Capital).
Built Claude-powered, runs on GitHub Actions (free), alerts via Telegram.

---

## Status

**Phase A — deployed and running in MOCK_MODE**
- Multi-market architecture (US active, Saudi dormant)
- Live data sources wired (Twelve Data, Marketaux, FRED, Yahoo fallback)
- Mock data complete for offline testing
- Full pipeline: data → technical analysis → Claude Sonnet → Telegram
- Schedule: 3:30 PM, 7:30 PM, 10:30 PM, 11:00 PM KSA Mon–Fri
- Halal dual-signal (approved list + AI assessment, no refusal)
- Currency awareness (USD + SAR)
- Google Sheet auto-initializes on first run
- Debug mode + cost tracking

**Phase B/C pending:** Trade logging commands (/buy /sell /pnl), weekly self-review, advanced P&L

---

## Deployment (one-time, ~20 min)

### 1. Confirm GitHub Secrets are set

Go to `Settings → Secrets and variables → Actions` — you should already have:
- `ANTHROPIC_API_KEY`
- `TWELVE_DATA_KEY`
- `MARKETAUX_KEY`
- `FRED_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GOOGLE_SHEET_URL`

### 2. Add `GOOGLE_SA_JSON` secret (one more needed)

For the bot to write to your Google Sheet, it needs a service account. One-time setup:

1. Go to: https://console.cloud.google.com/
2. Create new project: "trading-bot"
3. Enable APIs: Google Sheets API + Google Drive API
4. Create service account: IAM & Admin → Service Accounts → Create
5. Click the service account → Keys tab → Add key → JSON
6. Download the JSON file
7. Open it in a text editor, copy the entire content
8. Add GitHub Secret named `GOOGLE_SA_JSON` with that full JSON as value
9. From the downloaded JSON, find the `client_email` field (looks like `bot@trading-bot.iam.gserviceaccount.com`)
10. Open your Google Sheet → Share → paste that email → give **Editor** access

### 3. Paste the code files into your GitHub repo

You'll paste each file I've built. Use the GitHub web editor:
- Your repo → "Add file" → "Create new file"
- Type filename (with folder path if needed, e.g. `core/logger.py`)
- Paste content
- Commit

Files to paste (in this order):
1. `requirements.txt`
2. `config.py`
3. `main.py`
4. `markets/us/config.py`
5. `markets/saudi/config.py`
6. `core/mock_sources.py`
7. `core/live_sources.py`
8. `core/data_router.py`
9. `core/logger.py`
10. `core/technical.py`
11. `core/halal.py`
12. `core/sheets.py`
13. `core/claude_client.py`
14. `core/telegram_client.py`
15. `core/analyst.py`
16. `core/brief_composer.py`
17. `prompts/v1_analyst.txt`
18. `prompts/v1_filter.txt`
19. `prompts/v1_weekly_review.txt`
20. `prompts/v1_trade_report.txt`
21. `.github/workflows/trading-bot.yml`

Note: GitHub creates folders automatically when you type the path in the filename.

### 4. First test run (mock mode, free)

1. Go to your repo → **Actions** tab
2. Click "Trading Bot" workflow
3. Click "Run workflow" button (right side)
4. Leave defaults: `brief_type = test`, `mock_mode = true`
5. Click green "Run workflow"
6. Within ~60 seconds, you get a Telegram brief

### 5. Verify the brief looks good

You should see:
- Mock mode warning at top
- Market context (S&P, Nasdaq, VIX, oil, etc.)
- Your SPWO position with Claude's analysis
- Watchlist (SPUS, IBIT) with analysis
- Cost footer (~$0.02 for test run)

### 6. Add your SPWO position to the Sheet

The first test run will create all tabs automatically.
Then open the Sheet → `Positions` tab → add row 2:

| Ticker | Market | Shares | AvgCost_USD | AvgCost_SAR | StopLoss | Target | DateOpened | Sector | HalalStatus |
|--------|--------|--------|-------------|-------------|----------|--------|------------|--------|-------------|
| SPWO   | US     | 39     | 30.76       | 115.35      | 30.76    | 31.68  | 2026-04-18 | International ETF | Halal |

Optionally add focus stocks in `Focus` tab:

| Ticker | Market | DateAdded | CustomNotes |
|--------|--------|-----------|-------------|
| AAPL   | US     | 2026-04-18 | |
| NVDA   | US     | 2026-04-18 | |
| MSFT   | US     | 2026-04-18 | |

### 7. Run another test to see with positions loaded

Run workflow again. Now the brief should reference your SPWO position.

### 8. Go live

When you're happy with mock briefs:
1. Actions → Run workflow
2. Set `mock_mode = false`
3. Click Run
4. First LIVE brief arrives

After that, the bot runs automatically on schedule Mon–Fri.

---

## Operating the bot

### Check what the bot did

- **Telegram:** All briefs land here
- **Actions tab:** Run logs visible for each execution
- **Sheet `Logs` tab:** Structured events with timestamps, costs

### Update your positions

Phase A = manual edit of `Positions` tab.
Phase B will add `/buy` and `/sell` Telegram commands.

### Change bot behavior

Edit `Config` tab in the Sheet. Settings the bot respects:
- `MAX_POSITION_PCT` — max % in one stock
- `MAX_SECTOR_PCT` — max % in one sector
- `RISK_TOLERANCE` — low / medium / high
- `EARNINGS_BUFFER_DAYS` — warn if earnings within N days
- `USD_TO_SAR` — currency rate (adjust if peg changes)

### Pause the bot

Easiest: edit `.github/workflows/trading-bot.yml` in GitHub web editor, comment out the `schedule:` block, commit. Bot stops running.

### Prompt improvements

Prompts live in `prompts/v1_*.txt`. To change:
1. Copy `v1_analyst.txt` → `v2_analyst.txt`
2. Edit v2
3. Edit `config.py` → update `ACTIVE_PROMPTS["analyst"]` to `"v2_analyst.txt"`
4. Commit — bot uses v2 going forward
5. Revert by flipping back to v1 (no code lost)

---

## Architecture

```
GitHub Actions (cron)
        ↓
      main.py
        ↓
┌───────────────────────────────────────┐
│ 1. sheets.read_positions()             │
│ 2. For each ticker:                    │
│    • data_router.get_price()           │
│    • data_router.get_price_history()   │
│    • data_router.get_news()            │
│    • data_router.get_macro()           │
│    • technical.full_technical_snapshot()│
│    • halal.check_halal_listed()        │
│ 3. analyst.analyze_ticker()            │
│    → Claude Sonnet call                │
│ 4. brief_composer.compose_*()          │
│ 5. telegram_client.send_message()      │
│ 6. sheets.append_logs()                │
└───────────────────────────────────────┘
```

### Mock/live toggle

`config.MOCK_MODE` determines data source:
- `true` → reads from `core.mock_sources` (fake but realistic)
- `false` → reads from `core.live_sources` (real APIs)

Every module imports through `core.data_router` which routes automatically.
Change mode with the `MOCK_MODE` env var — no code changes needed.

### Multi-market

`config.ACTIVE_MARKETS` controls which markets run:
- `"US"` → only US (current default)
- `"SAUDI"` → only Saudi (dormant — data source not connected)
- `"US,SAUDI"` → both

Each market has its own config in `markets/<code>/config.py`:
- Trading hours
- Halal list
- Default watchlist
- Macro indicators
- Schedule

Adding Japan or another market = add `markets/japan/config.py` with same structure.

---

## Cost expectations

Per scheduled run (live mode):
- Pre-market brief (~6 tickers analyzed): ~$0.05
- Mid-session (1–3 tickers if triggered): ~$0.02
- Pre-close (positions only): ~$0.02
- EOD summary (no Claude calls): $0.00

**Expected monthly total: $3–6 USD.**

Cost footer appears in every Telegram brief.
`DAILY_COST_ALERT_USD` in `config.py` triggers an error alert if exceeded.

---

## Troubleshooting

### Bot didn't send a Telegram message

1. Check Actions tab → click the failed run → read logs
2. Most common: Google Sheet not shared with service account
3. Next most common: typo in a GitHub Secret name

### Claude returns empty response

- Check `ANTHROPIC_API_KEY` is correct
- Check your Anthropic account has credits
- Check rate limits (unlikely at our volume)

### Mock mode vs live mode confusion

Every Telegram brief shows `⚠️ MOCK MODE` at top if mock is on.
If you see this and expected live data, the `MOCK_MODE` env var is still true.

### Google Sheet not updating

- Verify `GOOGLE_SA_JSON` secret is set correctly
- Verify service account email has Editor access to the Sheet
- Check `Logs` tab in Sheet for `sheets` module errors

---

## What's next (Phase B / Phase C)

**Phase B — Trade management:**
- `/buy TICKER SHARES PRICE` and `/sell TICKER SHARES PRICE` Telegram commands
- Auto position update + new avg cost calculation
- TradeLog entries with reasons
- `/pnl` and `/status` commands
- Daily + cumulative P&L in EOD brief

**Phase C — Self-improvement loop:**
- Outcome grading on every recommendation (24h, 1wk follow-up)
- Weekly Claude self-review with pattern detection
- Excellent-call / terrible-call post-mortems
- Human-approved prompt updates (`/approve` `/deny`)

Both build on top of what's in Phase A. No refactoring needed.
