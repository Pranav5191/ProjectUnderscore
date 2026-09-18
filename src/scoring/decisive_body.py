import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class DecisiveBodyScorer(BaseScorer):
    def __init__(self):
        # Microstructure looks purely at today's isolated candle. 
        # No rolling period is required.
        pass

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail
        if len(df) < 1:
            return 0.0
            
        # 2. Vectorized Candle Math
        # Real body = absolute difference between Open and Close
        body = (df['close'] - df['open']).abs()
        
        # Total range = High to Low
        total_range = df['high'] - df['low']
        
        # 3. Extract Today's Values
        current_body = body.iloc[-1]
        current_range = total_range.iloc[-1]
        
        # 4. Zero Division Protection
        if current_range == 0:
            return 0.5 # A zero-tick day (circuit limit) is mathematically neutral here
            
        # 5. Calculate Raw Ratio
        # E.g., Body is ₹8, Total Range is ₹10. Score = 0.80.
        raw_score = current_body / current_range
        
        # 6. Enforce Contract Bounds
        return self.clamp(raw_score)