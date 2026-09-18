import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class GapRiskScorer(BaseScorer):
    def __init__(self, period: int = 20, max_gap_ratio: float = 0.50):
        # max_gap_ratio = 0.50: If 50% or more of a stock's total price 
        # movement happens overnight, we consider it completely toxic.
        self.period = period
        self.max_gap_ratio = max_gap_ratio

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail
        if len(df) < self.period + 1:
            return 0.0
            
        # 2. Vectorized True Range and Gap Magnitude
        df['prev_close'] = df['close'].shift(1)
        
        gap_magnitude = (df['open'] - df['prev_close']).abs()
        
        df['tr1'] = df['high'] - df['low']
        df['tr2'] = (df['high'] - df['prev_close']).abs()
        df['tr3'] = (df['low'] - df['prev_close']).abs()
        true_range = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        
        # 3. Sum over the period to prevent single-day earnings anomalies from ruining the math
        sum_gaps = gap_magnitude.rolling(window=self.period).sum().iloc[-1]
        sum_tr = true_range.rolling(window=self.period).sum().iloc[-1]
        
        # 4. Zero Division Protection
        if sum_tr == 0:
            return 0.0 # If True Range is zero for 20 days, the stock is halted/dead.
            
        # 5. Calculate Raw Ratio
        raw_gap_ratio = sum_gaps / sum_tr
        
        # 6. Normalize and INVERT
        # We divide by 0.50. If gap ratio is 0.25, penalty_score = 0.5. 
        # final_score = 1.0 - 0.5 = 0.5.
        penalty_score = raw_gap_ratio / self.max_gap_ratio
        final_score = 1.0 - penalty_score
        
        # 7. Enforce Contract Bounds
        return self.clamp(final_score)