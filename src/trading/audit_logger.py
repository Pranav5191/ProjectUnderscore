import csv
import os
import logging
from datetime import datetime

class AuditLogger:
    def __init__(self):
        self.filepath = os.path.join(os.path.dirname(__file__), f"paper_trades_{datetime.now().strftime('%Y%m%d')}.csv")
        if not os.path.exists(self.filepath):
            with open(self.filepath, 'w', newline='') as f:
                csv.writer(f).writerow([
                    'trace_id','tick_timestamp', 'exec_wall_time', 'security_id', 
                    'action', 'fill_price', 'qty', 'latency_ms', 'slippage', 'booked_pnl', 'total_balance'
                ])

    def log_trade(self, trace_id, tick_time, sec_id, action, price, qty, latency, slippage, pnl, balance):
        with open(self.filepath, 'a', newline='') as f:
            csv.writer(f).writerow([
                trace_id, tick_time, datetime.now().isoformat(), sec_id, 
                action, price, qty, latency, slippage, pnl, balance
            ])

def setup_signal_logger():
    """Sets up a dedicated file logger for system signals."""
    logger = logging.getLogger("SignalLogger")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # Stops it from spamming the console
    
    # Create logs directory if it doesn't exist
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    
    filepath = os.path.join(log_dir, f"signals_{datetime.now().strftime('%Y%m%d')}.log")
    
    # Prevent adding multiple handlers if the engine restarts
    if not logger.handlers:
        fh = logging.FileHandler(filepath)
        fh.setLevel(logging.INFO)
        # Format: [TIME] Trace: 123 | Sec: 2885 | Action: BUY | Conf: 0.95 | Qty: 5 | Status: EXECUTED
        formatter = logging.Formatter('%(asctime)s | %(message)s', datefmt='%H:%M:%S')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        
    return logger