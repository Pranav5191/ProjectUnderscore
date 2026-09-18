# src/scoring/volatility.py
import pandas as pd
from src.scoring.base import BaseScorer

class NormalizedATRScorer(BaseScorer):
    def __init__(self, period: int = 14, max_expected_pct: float = 0.05):
        # We expect a maximum daily move of 5% (0.05). 
        # Anything moving 5%+ gets a perfect 1.0 score.
        self.period = period
        self.max_expected_pct = max_expected_pct 

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail: Ensure we have enough data to calculate a 14-day window
        if len(df) < self.period + 1:
            return 0.0 
            
        # 2. Calculate the True Range for every single day
        df['prev_close'] = df['close'].shift(1)
        df['tr1'] = df['high'] - df['low']
        df['tr2'] = (df['high'] - df['prev_close']).abs()
        df['tr3'] = (df['low'] - df['prev_close']).abs()
        
        # True Range is the maximum of the above three values
        df['true_range'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        
        # 3. Calculate 14-Day ATR (The Rolling Mean)
        atr = df['true_range'].rolling(window=self.period).mean().iloc[-1]
        latest_close = df['close'].iloc[-1]
        
        # 4. Normalize
        atr_pct = atr / latest_close
        raw_score = atr_pct / self.max_expected_pct
        
        # 5. Enforce Contract Bounds
        return self.clamp(raw_score)