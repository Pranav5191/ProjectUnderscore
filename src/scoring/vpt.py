import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class VolumePriceTrendScorer(BaseScorer):
    def __init__(self, period: int = 20):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail
        if len(df) < self.period + 1:
            return 0.5 # Neutral score if missing data
            
        # 2. Vectorized Percentage Change
        df['prev_close'] = df['close'].shift(1)
        
        # We add 1e-6 to prevent division by zero in the freak case a stock trades at ₹0.00
        df['pct_change'] = (df['close'] - df['prev_close']) / (df['prev_close'] + 1e-6)
        
        # 3. Calculate Daily VPT Flow and Cumulative Sum
        # If price goes up 2%, we add (Volume * 0.02) to the running total.
        df['vpt_flow'] = df['volume'] * df['pct_change']
        df['vpt'] = df['vpt_flow'].cumsum()
        
        # 4. Stochastic Normalization (Rolling Min-Max)
        vpt_min = df['vpt'].rolling(window=self.period).min().iloc[-1]
        vpt_max = df['vpt'].rolling(window=self.period).max().iloc[-1]
        current_vpt = df['vpt'].iloc[-1]
        
        # 5. Zero Division / Flatline Protection
        # If the stock has zero volume or zero price movement for 20 days, max == min.
        if vpt_max == vpt_min:
            return 0.5
            
        # 6. Calculate Final Score
        # Maps the current VPT strictly between 0.0 (at the 20-day low) and 1.0 (at the 20-day high)
        raw_score = (current_vpt - vpt_min) / (vpt_max - vpt_min)
        
        # 7. Enforce Contract Bounds
        return self.clamp(raw_score)