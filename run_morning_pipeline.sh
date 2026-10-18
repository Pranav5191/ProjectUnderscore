#!/bin/bash
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
source venv/bin/activate

LOG_FILE="logs/morning_pipeline_$(date +%Y%m%d).log"
mkdir -p logs

python3 -m src.utils.tg_notifier "PRE-MARKET PIPELINE" "START" "Starting 08:35 AM sequence (AI Orchestrator -> Symbol Sync -> 60d Micro-Bots -> Live Tmux)."

if ! python3 -m src.scripts.ai_orchestrator >> "$LOG_FILE" 2>&1; then
    python3 -m src.utils.tg_notifier "AI CATALYST ORCHESTRATOR" "WARN" "Warning in AI Orchestrator. Using existing target_ticks.json."
fi

./restart_ws.sh >> "$LOG_FILE" 2>&1
python3 -m src.utils.tg_notifier "LIVE TRADING ENGINE" "SUCCESS" "3-Pane Tmux (Ingester + BatchWriter + 25 Micro-Bots) Online & Armed for 09:15 AM!"
