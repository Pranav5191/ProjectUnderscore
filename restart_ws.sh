#!/bin/bash

# 1. Establish the environment for cron
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd /home/ubuntu/project_pipeline

# 2. Zombie Killer: Clear the board of any ghost processes
pkill -f "python3 -m src.ingest_main" || true
pkill -f "python3 -m src.pipeline.batch_writer" || true
pkill -f "python3 -m src.trading.strategy_engine" || true

# 3. Dynamic Update: Fetch today's top 12 stocks BEFORE starting the pipeline
source venv/bin/activate && python3 update_symbols.py

# 4. Wake up tmux and clear any broken sessions
tmux start-server
tmux kill-session -t hft_live 2>/dev/null

# 5. Create the new session
tmux new-session -d -s hft_live

# 6. Launch the 3-Pane Dashboard
tmux send-keys -t hft_live "cd /home/ubuntu/project_pipeline && source venv/bin/activate && python3 -m src.ingest_main" C-m
tmux split-window -t hft_live -h
tmux send-keys -t hft_live "cd /home/ubuntu/project_pipeline && source venv/bin/activate && python3 -m src.pipeline.batch_writer" C-m
tmux split-window -t hft_live -v
tmux send-keys -t hft_live "cd /home/ubuntu/project_pipeline && source venv/bin/activate && python3 -m src.trading.strategy_engine" C-m
