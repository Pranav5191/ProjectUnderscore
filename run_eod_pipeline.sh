#!/bin/bash
cd /home/ubuntu/project_pipeline
source venv/bin/activate

LOG_FILE="logs/eod_pipeline_$(date +%Y%m%d).log"
mkdir -p logs

python3 -m src.utils.tg_notifier "EOD MARKET SHUTDOWN" "START" "Stopping live intraday daemons & syncing EOD database..."

pkill -f "src/ingestor/ws_daemon.py" || true
pkill -f "src/features/feature_engine.py" || true
pkill -f "src/trading/strategy_engine.py" || true
pkill -f "src/trading/order_manager.py" || true

if [ -f "daily_sync.py" ]; then
    python3 daily_sync.py >> "$LOG_FILE" 2>&1
fi

if python3 quant_screener.py >> "$LOG_FILE" 2>&1; then
    python3 -m src.utils.tg_notifier "EOD QUANT SCREENER" "SUCCESS" "Daily SQLite DB synced & tomorrow's Top 50 candidate pool generated."
else
    python3 -m src.utils.tg_notifier "EOD QUANT SCREENER" "WARN" "quant_screener.py reported a warning. Check $LOG_FILE."
fi
