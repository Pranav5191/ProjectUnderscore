#!/bin/bash

# 1. Delete Log folders and files older than 48 hours
find /home/ubuntu/project_pipeline/logs -mindepth 1 -mtime +1 -exec rm -rf {} +

# 2. Delete Telegram CSV archives older than 48 hours
find /home/ubuntu/project_pipeline/archives -name "*.csv" -type f -mtime +1 -exec rm -f {} +

# 3. Delete database rows older than 2 days
docker exec alpha_postgres psql -U alpha_user -d alpha_market_data -c "DELETE FROM market_ticks WHERE timestamp < NOW() - INTERVAL '2 days';"
