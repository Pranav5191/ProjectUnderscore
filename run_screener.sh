#!/bin/bash

# 1. Establish the environment for cron
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
cd /home/ubuntu/project_pipeline

# 2. Zombie Killer: Clear any ghost screener processes
pkill -f "python3 src/scripts/screener.py" || true

# 3. Wake up tmux and clear old session
tmux start-server
tmux kill-session -t hft_screener 2>/dev/null

# 4. Create new session and launch the screener
tmux new-session -d -s hft_screener
tmux send-keys -t hft_screener "cd /home/ubuntu/project_pipeline && source venv/bin/activate && python3 src/scripts/screener.py" C-m
