import csv
import os
import logging
from datetime import datetime

class AuditLogger:
    def __init__(self):
        self.filepath = os.path.join(os.path.dirname(__file__), f"paper_trades_{datetime.now().strftime('%Y%m%d')}.csv")
        if not os.path.exists(self.filepath):
            with open(self.filepath, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow([
                    'trace_id', 'tick_timestamp', 'exec_wall_time', 'security_id', 'symbol',
                    'action', 'fill_price', 'qty', 'notional_value', 'trigger_source', 'trigger_reason',
                    'latency_ms', 'slippage', 'gross_pnl', 'transaction_taxes', 'net_pnl', 'total_balance'
                ])

    def log_trade(
        self, trace_id, tick_time, sec_id, action, price, qty, latency, slippage,
        gross_pnl, taxes, net_pnl, balance, symbol="", trigger_source="", trigger_reason=""
    ):
        with open(self.filepath, 'a', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow([
                trace_id, tick_time, datetime.now().isoformat(), sec_id, symbol or f"TOK_{sec_id}",
                action, round(price, 2), qty, round(price * qty, 2),
                trigger_source or "SYSTEM", trigger_reason or "STANDARD",
                round(latency, 2), round(slippage, 4),
                round(gross_pnl, 2), round(taxes, 2), round(net_pnl, 2), round(balance, 2)
            ])

def setup_signal_logger():
    logger = logging.getLogger("SignalLogger")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    filepath = os.path.join(log_dir, f"signals_{datetime.now().strftime('%Y%m%d')}.log")

    if not logger.handlers:
        fh = logging.FileHandler(filepath, encoding='utf-8')
        fh.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s | %(message)s', datefmt='%H:%M:%S')
        fh.setFormatter(formatter)
        logger.addHandler(fh)

    return logger
