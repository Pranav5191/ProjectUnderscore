import os
import json
import redis
import numpy as np
import pandas as pd
import lightgbm as lgb

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../"))
MODEL_PATH = os.path.join(project_root, "models", "macro_regime_v1.txt")
CSV_PATH = os.path.join(project_root, "historical_macro_matrix.csv")

class RegimeOracle:
    def __init__(self):
        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(f"[ORACLE ERROR] Model artifact not found at {MODEL_PATH}")
        
        # Load the compiled LightGBM model into memory
        self.model = lgb.Booster(model_file=MODEL_PATH)
        self.feature_names = self.model.feature_name()
        
        self.r = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True
        )
        
        self.historical_df = None
        if os.path.exists(CSV_PATH):
            self.historical_df = pd.read_csv(CSV_PATH)
            
            # Bulletproof Timezone Normalization
            parsed_time = pd.to_datetime(self.historical_df['timestamp'])
            
            # If the CSV saved with a timezone (like +05:30), safely convert and strip it
            if parsed_time.dt.tz is not None:
                parsed_time = parsed_time.dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
                
            self.historical_df['timestamp'] = parsed_time
            self.historical_df.set_index('timestamp', inplace=True)
            self.historical_df.sort_index(inplace=True)

    def get_regime(self, tick_ts_ms=None) -> int:
        features_dict = None
        tick_time_str = "LIVE"
        
        # ─── REPLAY / HISTORICAL LOOKUP ───
        if tick_ts_ms:
            # Convert incoming epoch ticks to naive IST
            tick_dt = pd.to_datetime(tick_ts_ms, unit='ms', utc=True).tz_convert('Asia/Kolkata').tz_localize(None)
            tick_time_str = str(tick_dt.time())
            
            # Check if tick timestamp falls WITHIN historical CSV dates
            if self.historical_df is not None and tick_dt <= self.historical_df.index[-1]:
                minute = tick_dt.minute
                floored_minute = (minute // 2) * 2
                boundary = tick_dt.replace(minute=floored_minute, second=0, microsecond=0)
                
                try:
                    idx = self.historical_df.index.get_indexer([boundary], method='pad')[0]
                    if idx != -1:
                        features_dict = self.historical_df.iloc[idx].to_dict()
                except Exception:
                    pass

        # ─── LIVE MARKET FALLBACK ───
        if not features_dict:
            raw_data = self.r.get("macro_features_latest")
            if raw_data:
                features_dict = json.loads(raw_data)
        
        # Fail-safe circuit breaker: If no macro data exists, the pipeline is broken.
        if not features_dict:
            print(f"[ORACLE CRITICAL] {tick_time_str} | Data missing or Redis offline. Bypassing ML.")
            return None  # Return None instead of 0 to signal a critical fault
            
        # Format features to match model expectations
        feature_vector = [features_dict.get(col, 0.0) for col in self.feature_names]
        X = np.array(feature_vector, dtype=np.float32).reshape(1, -1)
        
        probs = self.model.predict(X)[0]
        prob_bear, prob_chop, prob_bull = probs[0], probs[1], probs[2]
        
        # ─── TWO-STAGE DECISION FORMULA ───
        # 1. Chop Gate: If Chop is dominant or exceeds neutral expectation (33.3%), veto.
        if prob_chop >= 0.333 or (prob_chop > prob_bull and prob_chop > prob_bear):
            regime_output = 0
        else:
            # 2. Directional Margin Gate: Require at least a 5% delta between Bull and Bear
            directional_edge = prob_bull - prob_bear
            
            if directional_edge >= 0.05:
                regime_output = 1   # Clear Bullish Dominance
            elif directional_edge <= -0.05:
                regime_output = -1  # Clear Bearish Dominance
            else:
                regime_output = 0   # Ambiguous / Deadlock (Veto)
            
        # ─── X-RAY VISION LOGGING ───
        regime_name = "BULL" if regime_output == 1 else "BEAR" if regime_output == -1 else "CHOP"
        print(f"  [ORACLE AI] {tick_time_str} | Output: {regime_name} | Edge: {abs(prob_bull - prob_bear)*100:.1f}% | (Bull: {prob_bull*100:.1f}%, Bear: {prob_bear*100:.1f}%, Chop: {prob_chop*100:.1f}%)")
        
        return regime_output