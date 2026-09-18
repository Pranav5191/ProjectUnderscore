import pandas as pd
from src.scoring.base import BaseScorer

class RateOfChangeScorer(BaseScorer):
    def __init__(self, period: int = 20, max_expected_move: float = 0.15):
        # A 15% move (0.15) over 20 trading days (1 month) is our institutional ceiling.
        # Anything moving faster than 15% a month gets clamped to a perfect 1.0.
        self.period = period
        self.max_expected_move = max_expected_move

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail
        # If we don't have enough data, we return 0.5 (perfectly neutral velocity)
        if len(df) < self.period + 1:
            return 0.5 
            
        # 2. Vectorized Point A to Point B Math
        df['close_n_ago'] = df['close'].shift(self.period)
        
        # Prevent division by zero if a data glitch has price at 0
        divisor = df['close_n_ago'].replace(0, 1e-6)
        
        # Raw percentage change formula: (New - Old) / Old
        df['roc'] = (df['close'] - df['close_n_ago']) / divisor
        
        # 3. Extract the current day's velocity
        current_roc = df['roc'].iloc[-1]
        
        # 4. Normalize the Score (Shift and Scale)
        # We have a range from -0.15 to +0.15. 
        # Total range width is 0.30 (max_expected_move * 2.0).
        # We shift the baseline by adding 0.15 so that -0.15 becomes 0.0, and 0.0 becomes 0.15.
        shifted_roc = current_roc + self.max_expected_move
        raw_score = shifted_roc / (self.max_expected_move * 2.0)
        
        # 5. Enforce Contract Bounds
        return self.clamp(raw_score)