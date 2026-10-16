#!/bin/bash
cd /home/ubuntu/project_pipeline
source venv/bin/activate

LOG_FILE="logs/morning_pipeline_$(date +%Y%m%d).log"
mkdir -p logs

python3 -m src.utils.tg_notifier "PRE-MARKET PIPELINE" "START" "Starting 08:45 AM automated sequence (AI Orchestrator -> Symbol Sync -> 60d Micro-Bots -> Live Daemons)."

# Step 1: AI Orchestrator (fires its own START and SUCCESS pings)
if ! python3 -m src.scripts.ai_orchestrator >> "$LOG_FILE" 2>&1; then
    python3 -m src.utils.tg_notifier "AI CATALYST ORCHESTRATOR" "ERROR" "Crashed! Check $LOG_FILE. Continuing with existing target_ticks.json."
fi

# Step 2: Update symbols.json
if python3 update_symbols.py >> "$LOG_FILE" 2>&1; then
    python3 -m src.utils.tg_notifier "SYMBOL SYNC" "SUCCESS" "Locked 5 Macro Anchors + 25 Active Stocks into symbols.json."
else
    python3 -m src.utils.tg_notifier "SYMBOL SYNC" "ERROR" "update_symbols.py failed! Check $LOG_FILE."
    exit 1
fi

# Step 3: 60-Day Per-Stock Micro-Bot Trainer (fires its own START, SUCCESS, and ERROR pings)
if ! python3 -m src.ml.bot_trainer >> "$LOG_FILE" 2>&1; then
    exit 1
fi

# Step 4: Kill stale daemons and launch fresh live trading daemons
pkill -f "src/ingestor/ws_daemon.py" || true
pkill -f "src/features/feature_engine.py" || true
pkill -f "src/trading/strategy_engine.py" || true
pkill -f "src/trading/order_manager.py" || true
sleep 2

nohup python3 -m src.ingestor.ws_daemon >> logs/ws_daemon.log 2>&1 &
PID_WS=$!
nohup python3 -m src.features.feature_engine >> logs/feature_engine.log 2>&1 &
PID_FE=$!
nohup python3 -m src.trading.strategy_engine >> logs/strategy_engine.log 2>&1 &
PID_SE=$!
nohup python3 -m src.trading.order_manager >> logs/order_manager.log 2>&1 &
PID_OM=$!

sleep 3
if ps -p $PID_WS > /dev/null && ps -p$PID_FE > /dev/null && ps -p $PID_SE > /dev/null && ps -p$PID_OM > /dev/null; then
    python3 -m src.utils.tg_notifier "LIVE TRADING DAEMONS" "SUCCESS" "All 4 Engines Online & Armed for 09:15 AM!\nPIDs: WS=$PID_WS \vert{} FE=$PID_FE | SE=$PID_SE \vert{} OM=$PID_OM"
else
    python3 -m src.utils.tg_notifier "LIVE TRADING DAEMONS" "ERROR" "One or more daemons exited early! Check logs/strategy_engine.log."
fi
