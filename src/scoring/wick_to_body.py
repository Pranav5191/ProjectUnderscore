import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class WickToBodyScorer(BaseScorer):
    def __init__(self, max_wick_ratio: float = 4.0):
        # max_wick_ratio = 4.0: If the total wicks are 4x larger than the real body,
        # the stock is highly chaotic/indecisive, and gets heavily penalized.
        self.max_wick_ratio = max_wick_ratio

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail
        if len(df) < 1:
            return 0.0
            
        # 2. Vectorized Wick Math
        body = (df['close'] - df['open']).abs()
        total_range = df['high'] - df['low']
        total_wicks = total_range - body
        
        current_body = body.iloc[-1]
        current_wicks = total_wicks.iloc[-1]
        
        # 3. Zero Division Protection
        # If the body is 0 (exact Doji), we use 1e-6 to prevent a crash
        divisor = current_body if current_body > 0 else 1e-6
        
        # 4. Calculate Raw Ratio
        # E.g., Wicks total ₹8, Body is ₹2. raw_ratio = 4.0.
        raw_ratio = current_wicks / divisor
        
        # 5. Normalize and Invert (Penalty System)
        # Cap the ratio at max_wick_ratio (4.0).
        capped_ratio = min(raw_ratio, self.max_wick_ratio)
        
        # Divide by 4.0. A ratio of 4.0 becomes a 1.0 penalty.
        penalty_score = capped_ratio / self.max_wick_ratio
        
        # Invert: High penalty = 0.0 final score. 
        final_score = 1.0 - penalty_score
        
        # 6. Enforce Contract Bounds
        return self.clamp(final_score)