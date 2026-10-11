import os
import json
import redis
import numpy as np
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv

current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, "../../.env")
load_dotenv(dotenv_path=env_path)

# ─── ANCHOR CONFIGURATION ───
# Replace these values with the exact security IDs your broker assigns
ANCHOR_TOKENS = {
    "NIFTY_50": "99926000",
    "BANK_NIFTY": "99926009",
    "NIFTY_IT": "99926002",
    "RELIANCE": "2885",
    "HDFCBANK": "1333"
}
REVERSE_ANCHORS = {v: k for k, v in ANCHOR_TOKENS.items()}

# ─── HYPERPARAMETERS ───
RESAMPLE_INTERVAL_SEC = 120
ROLLING_WINDOW = 15          # 30 minutes (15 * 2-min bars) for correlation
MAX_MEMORY_ROWS = 100        # Caps DataFrame size to prevent memory leaks

class MacroMatrixBuilder:
    def __init__(self):
        self.r = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True
        )
        self.pubsub = self.r.pubsub()
        self.pubsub.subscribe("live_ticks")  # Ensure anchor ticks are routed here

        # State tracking for the current 2-minute bar
        self.current_boundary = None
        self.active_bar = self._init_empty_bar()
        
        # Historical DataFrame for rolling features
        columns = [f"{name}_{metric}" for name in ANCHOR_TOKENS.keys() for metric in ["open", "high", "low", "close", "vol"]]
        self.df = pd.DataFrame(columns=columns)

    def _init_empty_bar(self):
        """Initializes O(1) dictionary for ultra-fast tick aggregation."""
        return {
            name: {"open": None, "high": -np.inf, "low": np.inf, "close": None, "vol": 0}
            for name in ANCHOR_TOKENS.keys()
        }

    def _finalize_bar(self, timestamp_ms):
        """Pushes the aggregated dict to Pandas, calculates features, and prunes memory."""
        row_data = {}
        for name, data in self.active_bar.items():
            # Forward fill logic if an anchor had zero ticks in the window
            if data["open"] is None:
                last_close = self.df[f"{name}_close"].iloc[-1] if not self.df.empty else 0.0
                row_data.update({
                    f"{name}_open": last_close, f"{name}_high": last_close,
                    f"{name}_low": last_close, f"{name}_close": last_close, f"{name}_vol": 0
                })
            else:
                row_data.update({
                    f"{name}_open": data["open"], f"{name}_high": data["high"],
                    f"{name}_low": data["low"], f"{name}_close": data["close"], f"{name}_vol": data["vol"]
                })

        # Append to DataFrame
        idx = pd.to_datetime(self.current_boundary, unit='s')
        self.df.loc[idx] = row_data

        # Memory management: truncate DataFrame
        if len(self.df) > MAX_MEMORY_ROWS:
            self.df = self.df.iloc[-MAX_MEMORY_ROWS:]

        self._calculate_macro_features()

        # Reset active bar for the next interval
        self.active_bar = self._init_empty_bar()
        self.current_boundary = (timestamp_ms // 1000) // RESAMPLE_INTERVAL_SEC * RESAMPLE_INTERVAL_SEC

    def _calculate_macro_features(self):
        """Vectorized computation of ROC and Correlation on the historical matrix."""
        if len(self.df) < 2:
            return

        # 1. 2-Minute Rate of Change (ROC)
        for anchor in ["NIFTY_50", "BANK_NIFTY", "NIFTY_IT"]:
            col = f"{anchor}_close"
            self.df[f"{anchor}_ROC_2m"] = (self.df[col] / self.df[col].shift(1)) - 1.0

        # 2. Rolling Correlation (Reliance vs HDFC Bank)
        if len(self.df) >= ROLLING_WINDOW:
            self.df["HDFC_REL_CORR"] = self.df["HDFCBANK_close"].rolling(window=ROLLING_WINDOW).corr(self.df["RELIANCE_close"])
        else:
            self.df["HDFC_REL_CORR"] = np.nan

        latest_features = self.df.iloc[-1].to_dict()
        
        # Publish the finalized feature vector to Redis for the LightGBM Oracle
        self.r.set("macro_features_latest", json.dumps(latest_features))
        print(f"[{pd.to_datetime(self.current_boundary, unit='s')}] Macro Matrix Updated. Nifty ROC: {latest_features.get('NIFTY_50_ROC_2m', 0):.5f}")

    def run(self):
        print("[SYSTEM] Macro Matrix Pipeline Initialized. Awaiting ticks...")
        
        for message in self.pubsub.listen():
            if message['type'] != 'message':
                continue
                
            tick = json.loads(message['data'])
            sec_id = str(tick.get('security_id'))
            
            # Filter for macro anchors only
            anchor_name = REVERSE_ANCHORS.get(sec_id)
            if not anchor_name:
                continue

            tick_ts = tick.get('timestamp', 0)
            tick_price = tick.get('ltp', 0.0)
            tick_vol = max(tick.get('best_bid_vol', 0), tick.get('best_ask_vol', 0)) # Proxy for tick volume intensity
            
            tick_boundary = (tick_ts // 1000) // RESAMPLE_INTERVAL_SEC * RESAMPLE_INTERVAL_SEC

            # Initialize boundary on very first tick
            if self.current_boundary is None:
                self.current_boundary = tick_boundary

            # Boundary crossover detected -> Finalize current bar
            if tick_boundary > self.current_boundary:
                self._finalize_bar(tick_ts)

            # O(1) Aggregation Update
            anchor_data = self.active_bar[anchor_name]
            if anchor_data["open"] is None:
                anchor_data["open"] = tick_price
            
            anchor_data["high"] = max(anchor_data["high"], tick_price)
            anchor_data["low"] = min(anchor_data["low"], tick_price)
            anchor_data["close"] = tick_price
            anchor_data["vol"] += tick_vol

if __name__ == "__main__":
    builder = MacroMatrixBuilder()
    builder.run()