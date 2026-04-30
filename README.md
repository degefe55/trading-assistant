# Trading Assistant — Full Handover
*Last updated: April 23, 2026*

---

## What This Is

A personal AI trading assistant that:
- Sends you scheduled Telegram briefs (pre-market, pre-close, EOD)
- Scouts the market for new opportunities every session
- Responds instantly to `/buy` `/sell` `/pnl` commands via Telegram
- Analyzes your positions using technical + news + macro data
- Runs 24/7 at zero cost to you beyond API tokens (~$5-8/month)

---

## System Architecture

```
Two services working together:

GitHub Actions (scheduled)          Railway.app (always-on)
─────────────────────────           ───────────────────────
• 3:30 PM KSA pre-market brief      • Listens for Telegram messages
• 7:30 PM KSA mid-session           • /buy /sell /pnl respond in 3-5 sec
• 10:30 PM KSA pre-close            • Runs 24/7 free
• 11:00 PM KSA EOD summary          • webhook/app.py

Both pull from: Twelve Data (prices), Marketaux (news), FRED (macro)
Both write to: Google Sheet (positions, logs, trades)
Both talk to: Telegram (via your bot @cyberb_trading_bot)
Analysis by: Claude Sonnet (Anthropic API)
```

---

## Repository Structure

```
trading-assistant/
├── config.py                    ← All toggles, API keys, settings
├── main.py                      ← Entry point for GitHub Actions
├── requirements.txt             ← Python dependencies
├── railway.toml                 ← Railway deployment config
│
├── core/
│   ├── analyst.py               ← Gathers data + calls Claude + parses JSON
│   ├── brief_composer.py        ← Formats Telegram messages
│   ├── claude_client.py         ← Haiku (filter) + Sonnet (analysis) calls
│   ├── commands.py              ← /buy /sell /pnl /status /pause /resume
│   ├── data_router.py           ← Routes to mock or live data sources
│   ├── halal.py                 ← Dual-signal Halal screening (list + AI)
│   ├── live_sources.py          ← Twelve Data, Marketaux, FRED, Yahoo
│   ├── logger.py                ← Structured logging to Sheet + stdout
│   ├── mock_sources.py          ← Fake data for offline testing
│   ├── scout.py                 ← Market scanner for new opportunities
│   ├── sheets.py                ← Google Sheet read/write
│   ├── technical.py             ← RSI, MACD, Bollinger, SMA, support/resistance
│   ├── telegram_client.py       ← Send messages, get updates
│   └── trades.py                ← Buy/sell logic, P&L calculation
│
├── markets/
│   ├── us/config.py             ← US market hours, halal list, watchlist
│   └── saudi/config.py          ← Saudi market (dormant, ready to activate)
│
├── prompts/
│   ├── v1_analyst.txt           ← Old prose prompt (backup)
│   ├── v2_analyst.txt           ← ACTIVE: returns JSON for structured briefs
│   ├── v1_filter.txt            ← Haiku relevance scoring
│   ├── v1_trade_report.txt      ← Post-trade analysis
│   └── v1_weekly_review.txt     ← Weekly pattern detection
│
└── webhook/
    └── app.py                   ← Flask webhook for Railway (instant commands)
```

---

## GitHub Secrets (all required)

Go to: Your repo → Settings → Secrets and variables → Actions

| Secret Name | What it is |
|-------------|-----------|
| `ANTHROPIC_API_KEY` | Claude API key (console.anthropic.com) |
| `TWELVE_DATA_KEY` | Price data (twelvedata.com) |
| `MARKETAUX_KEY` | Financial news (marketaux.com) |
| `FRED_KEY` | Macro indicators (fred.stlouisfed.org) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram user ID: 6314295427 |
| `GOOGLE_SHEET_URL` | "Anyone with link" editor URL of your Sheet |
| `GOOGLE_SA_JSON` | Service account JSON for Sheet writes |

**Railway needs the same 10 variables** (same values, plus MOCK_MODE=false and ACTIVE_MARKETS=US)

---

## Google Sheet Structure

Sheet name: `Trading Bot Data`

| Tab | Purpose | Who writes |
|-----|---------|-----------|
| `Positions` | Your open trades | You manually or via /buy /sell |
| `Focus` | Up to 3 deep-track stocks | You manually |
| `TradeLog` | All buy/sell history | Bot writes via /buy /sell |
| `Logs` | Every bot action with timestamps + costs | Bot writes every run |
| `Halal_Approved` | Confirmed Halal tickers | Pre-seeded, you can add |
| `Patterns` | Claude's weekly observations | Bot writes (Phase D) |
| `Reports` | Trade post-mortems | Bot writes (Phase D) |
| `Config` | Your settings | You edit |

### Positions tab columns (row 1 headers, exact spelling):
```
Ticker | Market | Shares | AvgCost_USD | AvgCost_SAR | StopLoss | Target | DateOpened | Sector | HalalStatus
```

### Your current position (row 2):
```
SPWO | US | 39 | 30.76 | 115.35 | 30.76 | 31.68 | 2026-04-18 | International ETF | Halal
```

### Focus tab (rows 2-4):
```
AAPL | US | 2026-04-18 |
NVDA | US | 2026-04-18 |
MSFT | US | 2026-04-18 |
```

### Config tab settings you can change:
| Setting | Current Value | What it does |
|---------|--------------|-------------|
| MAX_POSITION_PCT | 25 | Max % of portfolio in one stock |
| MAX_SECTOR_PCT | 60 | Max % in one sector |
| MAX_OPEN_POSITIONS | 5 | Max concurrent positions |
| EARNINGS_BUFFER_DAYS | 3 | Warn if earnings within N days |
| RISK_TOLERANCE | medium | Affects suggestions |
| USD_TO_SAR | 3.75 | Currency conversion |

---

## Schedule (Mon–Fri KSA, auto-runs via GitHub Actions)

| Time KSA | Brief type | What it does |
|----------|-----------|-------------|
| 3:30 PM | Pre-market | Full brief: positions + focus + watchlist + scouting |
| 7:30 PM | Mid-session | Silent unless price near stop/target (then alerts) |
| 10:30 PM | Pre-close | Hold overnight or exit? |
| 11:00 PM | EOD | Daily P&L summary |

---

## Telegram Commands (instant via Railway webhook)

| Command | Example | What it does |
|---------|---------|-------------|
| `/buy` | `/buy SPWO 10 31.40` | Log buy, update avg cost, confirm |
| `/sell` | `/sell SPWO 5 31.80` | Log sell, calculate P&L, confirm |
| `/pnl` | `/pnl` | Instant P&L snapshot (realized + unrealized) |
| `/status` | `/status` | Bot health, open positions count, pause state |
| `/pause` | `/pause` | Stop all scheduled briefs (vacation, weekends) |
| `/resume` | `/resume` | Re-enable scheduled briefs |
| `/help` | `/help` | Command menu |

---

## How to Use Day-to-Day

### Trading session (Mon–Fri)
1. **3:30 PM** — Brief arrives in Telegram. Read PLAN section (top). Scroll to DEEP DIVES only if you want detail.
2. **During session** — If bot alerts you (7:30 PM), a price crossed a threshold. Read it.
3. **When you execute a trade on Awaed** — Text the bot immediately:
   - `/buy SPWO 10 31.40` — bot logs it and updates your sheet
   - `/sell SPWO 5 31.80` — bot logs it and calculates your P&L
4. **10:30 PM** — Pre-close: hold or exit decision for each position
5. **11:00 PM** — EOD summary with P&L

### If you don't trade today
Just read the briefs. No action needed. Bot keeps running automatically.

---

## Brief Format Guide

```
🔔 PRE-MARKET BRIEF                    ← Always present
Monday Apr 20 · 15:30 KSA

🚨 URGENT                              ← Only if immediate action needed
─────────────
🔴 SELL SPWO @ $30.76 — stop hit

💼 POSITIONS                           ← Your current holdings
─────────────
🟢 SPWO ✅🟢  39sh · $30.76→$31.07  +12.09 (+1.0%)
         ↑ ↑ ↑
         │ │ └── Claude AI says Halal (🟢/🟡/🔴)
         │ └──── On approved Halal list (✅ or absent)
         └────── P&L direction (🟢 profit / 🔴 loss)

📊 MARKET                              ← Single line macro snapshot
─────────────
Fed 3.64% · 10Y 4.26% · Oil $100.72↑ · CPI 3.2%

⚡ PLAN                                ← One action per stock, quick scan
─────────────
🟡 SPWO: Hold toward $31.68; stop firm at $30.76 [M·risk 2/5]
⚪ AAPL: Wait for pullback below $271 [M·risk 3/5]
↑         ↑                                    ↑
│         └── What to do                       └── Confidence[M/H/L] · risk 1-5
└── Action: 🟢BUY 🔴SELL 🟡HOLD ⚪WAIT

🔭 SCOUTING                            ← New opportunities found today
─────────────
🟢 AVGO (score 9/10 · risk 3/5)       ← Only shown if score ≥ 7
    💡 Meta AI chip deal               ← The catalyst
    → Entry >$216, stop $211           ← The plan

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━          ← Stop here if you have your answer
🔍 DEEP DIVES                          ← Full reasoning (scroll only if needed)
(scroll past if done reading)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## Toggling Mock Mode

**For testing (no real API costs for data):**
GitHub → Actions → Trading Bot → Run workflow → set `mock_mode = true`

**For live trading (default on schedule):**
Schedule always runs with `mock_mode = false`

**To switch permanently:**
GitHub → Settings → Secrets → add `MOCK_MODE` = `true` or `false`
(or edit `config.py` default)

---

## How to Update Prompts (tune Claude's behavior)

1. Open `prompts/v2_analyst.txt` in GitHub
2. Make changes (e.g., "be more aggressive on breakouts", "always flag earnings within 7 days")
3. To keep the old version: copy `v2_analyst.txt` → create `v3_analyst.txt`
4. Update `config.py` line: `"analyst": "v3_analyst.txt"`
5. If v3 is worse: change back to `v2_analyst.txt` in config.py → instant rollback

---

## Cost Breakdown

| Service | Cost | Notes |
|---------|------|-------|
| Anthropic API | ~$5-8/month | Claude Sonnet calls during briefs |
| GitHub Actions | Free | Scheduled runs stay within free tier |
| Railway | Free | Free tier covers this tiny webhook |
| Twelve Data | Free | 800 calls/day, plenty for our usage |
| Marketaux | Free | 100 calls/day, enough for scheduled runs |
| FRED | Free | Always free |
| Google Sheets | Free | Storage for positions + logs |
| **Total** | **~$5-8/month** | Only Anthropic charges you |

To see daily cost: check `Logs` tab in Sheet — `Cost_USD` column.
Each Telegram brief shows cost in footer: `💰 Run cost: $0.0605`

---

## Activating Saudi Market (future)

When ready:
1. Open `markets/saudi/config.py` and add real data source
2. GitHub → Settings → Secrets → update `ACTIVE_MARKETS` from `US` to `US,SAUDI`
3. Bot automatically picks up Saudi schedule (Sun-Thu, 10 AM - 3 PM KSA)
4. Positions tab supports `Market` column — add `SAUDI` for Saudi stocks

---

## Troubleshooting

### "No brief arrived at scheduled time"
- Check Actions tab — did a run fire? If yes, was it green?
- If run is green but no Telegram message: check Logs tab for errors
- If no run at all: GitHub Actions may have been delayed (wait 30 min, check again)
- Force a test: Actions → Run workflow → `brief_type=test` → `mock_mode=false`

### "Command not responded to"
- Did you send the command to `@cyberb_trading_bot`?
- Is Railway still running? Visit your Railway URL in browser — should show `{"status":"ok"}`
- Railway dashboard → Deployments → check if service is active (green dot)
- If Railway crashed: click "Redeploy" in Railway dashboard

### "Bot prices don't match Awaed"
- If brief ran before 16:30 KSA: expected — US market not open yet, prices are last-close
- If brief ran during session: may be Twelve Data rate limit — check Logs tab for `twelve_data` errors
- Awaed live prices are always the source of truth for execution

### "SPWO shows 0 shares"
- Google Sheet Positions tab: check row 2 has correct data
- Check column C (Shares) is a number `39` not text `'39`
- Check column headers match exactly (case sensitive)

### Smart quote corruption (`invalid character '"'`)
- A file was copy-pasted from phone/chat instead of drag-drop uploaded
- Fix: download file from this repo → delete broken file in GitHub → re-upload via drag-drop
- Prevention: always use GitHub's "Upload files" feature, never paste code into web editor

### "Cache save failed" warning
- Harmless. Ignore it. GitHub caching quirk on free tier.

---

## What's Pending (Phase D — build after 2-3 weeks of real usage)

Don't build these yet. Wait until you have real trade data.

- **Outcome grading:** Bot scores its own recommendations 24h/1wk later vs. what actually happened
- **Weekly self-review:** Sunday 8 PM — Claude reads week of recommendations + outcomes, spots patterns
- **Post-trade report:** After every `/sell`, bot writes "what went right/wrong" with lesson
- **Prompt improvement loop:** Claude suggests prompt changes, you `/approve` or `/deny`

These need real trades and real outcomes to work. 2-3 weeks minimum before it's worth building.

---

## Key People / Contacts

- **You:** Engineer in Dammam, KSA. Trading through Awaed (عوائد) by Albilad Capital.
- **Bot:** @cyberb_trading_bot on Telegram
- **GitHub repo:** trading-assistant (private)
- **Railway service:** trading-assistant-production (your Railway dashboard)

---

## Quick Reference Card

```
MORNING ROUTINE:
  Read 3:30 PM brief → check PLAN section → act on Awaed → /buy to log

TRADE COMMANDS:
  /buy TICKER SHARES PRICE
  /sell TICKER SHARES PRICE
  /pnl
  /status
  /pause  /resume

IF SOMETHING BREAKS:
  1. Check Actions tab in GitHub
  2. Check Railway dashboard
  3. Check Logs tab in Google Sheet
  4. Force test: Actions → Run workflow → test + mock=false
  5. Bring logs to Claude chat for diagnosis

IF PRICES ARE WRONG:
  Trust Awaed. Always.

IF COMMANDS ARE SLOW:
  Railway may be sleeping. First command wakes it (30 sec). Next ones instant.
```

---

*This document is the source of truth for the bot. Paste it to Claude at the start of any session to resume with full context.*
