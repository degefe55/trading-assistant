name: Trading Bot

on:
  # Manual trigger for testing
  workflow_dispatch:
    inputs:
      brief_type:
        description: "Brief type to run"
        required: true
        default: "test"
        type: choice
        options:
          - test
          - premarket
          - midsession
          - preclose
          - eod
          - commands
      mock_mode:
        description: "Use mock data?"
        required: true
        default: "true"
        type: choice
        options:
          - "true"
          - "false"

  # Scheduled runs (UTC - KSA is UTC+3)
  # KSA 3:30 PM = UTC 12:30 -> premarket
  # KSA 7:30 PM = UTC 16:30 -> midsession
  # KSA 10:30 PM = UTC 19:30 -> preclose
  # KSA 11:00 PM = UTC 20:00 -> eod
  # Every 5 min during active hours -> commands polling
  schedule:
    - cron: "30 12 * * 1-5"   # premarket
    - cron: "30 16 * * 1-5"   # midsession
    - cron: "30 19 * * 1-5"   # preclose
    - cron: "0 20 * * 1-5"    # eod
    # Commands polling - every 5 min, 10am-11pm KSA Mon-Sat
    # (Saturday allowed so user can test /pnl over weekend)
    - cron: "*/5 7-20 * * 1-6"

jobs:
  run-bot:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Determine brief type from schedule
        id: brief
        run: |
          if [ "${{ github.event_name }}" == "schedule" ]; then
            HOUR_MIN="$(date -u +'%H%M')"
            case "$HOUR_MIN" in
              1230) echo "brief_type=premarket" >> $GITHUB_OUTPUT ;;
              1630) echo "brief_type=midsession" >> $GITHUB_OUTPUT ;;
              1930) echo "brief_type=preclose" >> $GITHUB_OUTPUT ;;
              2000) echo "brief_type=eod" >> $GITHUB_OUTPUT ;;
              *)    echo "brief_type=commands" >> $GITHUB_OUTPUT ;;
            esac
            echo "mock_mode=false" >> $GITHUB_OUTPUT
          else
            echo "brief_type=${{ github.event.inputs.brief_type }}" >> $GITHUB_OUTPUT
            echo "mock_mode=${{ github.event.inputs.mock_mode }}" >> $GITHUB_OUTPUT
          fi

      # Cache /tmp state across runs (command offset + pause flag)
      - name: Restore bot state
        uses: actions/cache/restore@v4
        with:
          path: |
            /tmp/telegram_state.txt
            /tmp/bot_paused.txt
          key: bot-state-${{ github.run_id }}
          restore-keys: |
            bot-state-

      - name: Run trading bot
        env:
          BRIEF_TYPE: ${{ steps.brief.outputs.brief_type }}
          MOCK_MODE: ${{ steps.brief.outputs.mock_mode }}
          DEBUG_MODE: "true"
          ACTIVE_MARKETS: "US"
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          TWELVE_DATA_KEY: ${{ secrets.TWELVE_DATA_KEY }}
          MARKETAUX_KEY: ${{ secrets.MARKETAUX_KEY }}
          FRED_KEY: ${{ secrets.FRED_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          GOOGLE_SHEET_URL: ${{ secrets.GOOGLE_SHEET_URL }}
          GOOGLE_SA_JSON: ${{ secrets.GOOGLE_SA_JSON }}
        run: |
          python main.py

      - name: Save bot state
        if: always()
        uses: actions/cache/save@v4
        with:
          path: |
            /tmp/telegram_state.txt
            /tmp/bot_paused.txt
          key: bot-state-${{ github.run_id }}
