#!/bin/bash
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
source venv/bin/activate

LOG_FILE="logs/eod_pipeline_$(date +%Y%m%d).log"
mkdir -p logs

python3 -m src.utils.tg_notifier "EOD SCREENER PIPELINE" "START" "Syncing daily OHLCV database & running 19-factor ensemble screener..."
if python3 src/scripts/screener.py >> "$LOG_FILE" 2>&1; then
    python3 -m src.utils.tg_notifier "EOD QUANT SCREENER" "SUCCESS" "Daily SQLite DB synced & tomorrow's Top 50 candidate pool written to target_ticks.json."
else
    python3 -m src.utils.tg_notifier "EOD QUANT SCREENER" "ERROR" "screener.py failed! Check $LOG_FILE."
fi
