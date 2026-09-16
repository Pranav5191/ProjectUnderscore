#!/bin/bash
cd /home/ubuntu/project_pipeline

# 1. Send a literal "Ctrl+C" keystroke to Session 0, Window 0, Pane 0
tmux send-keys -t 0:0.0 C-c

# 2. Give it 3 seconds to gracefully disconnect and clear the port
sleep 3

# 3. Type the start command into Pane 0 and hit Enter
tmux send-keys -t 0:0.0 "python3 -m src.pipeline.websocket_client" Enter
