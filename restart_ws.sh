#!/bin/bash
cd /home/ubuntu/project_pipeline

# 1. Stop both processes
tmux send-keys -t hft_live:1.0 C-c
tmux send-keys -t hft_live:1.2 C-c

# 2. Wait 3 seconds, then update symbols.json dynamically
sleep 3
source venv/bin/activate && python3 update_symbols.py

# 3. Start both processes with the new top 12 stocks
tmux send-keys -t hft_live:1.0 "source venv/bin/activate && python3 -m src.pipeline.websocket_client" Enter
tmux send-keys -t hft_live:1.2 "source venv/bin/activate && python3 -m src.trading.strategy_engine" Enter
