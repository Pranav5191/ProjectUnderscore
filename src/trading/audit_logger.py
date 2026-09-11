import csv
import os
from datetime import datetime

class AuditLogger:
    def __init__(self):
        self.filepath = os.path.join(os.path.dirname(__file__), f"paper_trades_{datetime.now().strftime('%Y%m%d')}.csv")
        if not os.path.exists(self.filepath):
            with open(self.filepath, 'w', newline='') as f:
                csv.writer(f).writerow([
                    'tick_timestamp', 'exec_wall_time', 'security_id', 
                    'action', 'fill_price', 'qty', 'latency_ms', 'slippage', 'booked_pnl', 'total_balance'
                ])

    def log_trade(self, tick_time, sec_id, action, price, qty, latency, slippage, pnl, balance):
        with open(self.filepath, 'a', newline='') as f:
            csv.writer(f).writerow([
                tick_time, datetime.now().isoformat(), sec_id, 
                action, price, qty, latency, slippage, pnl, balance
            ])