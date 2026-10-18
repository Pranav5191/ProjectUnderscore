#!/bin/bash
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

pkill -f "python3 -m src.ingest_main" || true
pkill -f "python3 -m src.pipeline.batch_writer" || true
pkill -f "python3 -m src.trading.strategy_engine" || true

source venv/bin/activate
python3 update_symbols.py
python3 -m src.ml.bot_trainer

tmux start-server
tmux kill-session -t hft_live 2>/dev/null || true
tmux new-session -d -s hft_live

# Pane 0 (Left): WebSocket Ingest
tmux send-keys -t hft_live "cd $PROJECT_DIR && source venv/bin/activate && python3 -m src.ingest_main" C-m

# Pane 1 (Top-Right): Postgres Batch Writer
tmux split-window -t hft_live -h
tmux send-keys -t hft_live "cd $PROJECT_DIR && source venv/bin/activate && python3 -m src.pipeline.batch_writer" C-m

# Pane 2 (Bottom-Right): Strategy Engine (Per-Stock Micro-Bots)
tmux split-window -t hft_live -v
tmux send-keys -t hft_live "cd $PROJECT_DIR && source venv/bin/activate && python3 -m src.trading.strategy_engine" C-m
