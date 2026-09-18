#!/bin/bash

# Ensure logs directory exists
mkdir -p /home/ubuntu/project_pipeline/logs

# 1. Delete Log folders and files older than 48 hours
find /home/ubuntu/project_pipeline/src/trading/logs -mindepth 1 -mtime +1 -exec rm -rf {} +

# 2. Delete paper trade CSVs older than 48 hours in the main folder
find /home/ubuntu/project_pipeline/src/trading -maxdepth 1 -name "paper_trades_*.csv" -type f -mtime +1 -exec rm -f {} +

# 3. Delete database rows older than 2 days
docker exec alpha_postgres psql -U alpha_user -d alpha_market_data -c "TRUNCATE TABLE market_ticks;"
# 4. sql data backup
find /home/ubuntu/project_pipeline/src/trading -maxdepth 1 -name "alpha_market_data_backup_*.sql.gz" -type f -mtime +0 -exec rm -f {} +
