import pandas as pd
from src.scoring.base import BaseScorer

class DistanceFromHighScorer(BaseScorer):
    def __init__(self, period: int = 252, max_drawdown: float = 0.30):
        # 252 trading days in the Indian market year.
        # max_drawdown = 0.30: If a stock is 30% or more below its 52-week high, 
        # it is structurally damaged and gets a 0.0 score.
        self.period = period
        self.max_drawdown = max_drawdown

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Macro Guardrail
        # The orchestrator MUST pass at least a year of data.
        if len(df) < self.period:
            return 0.0
            
        # 2. Vectorized 52-Week High
        # We scan the 'high' column, not the 'close', because we care about the absolute peak.
        df['52w_high'] = df['high'].rolling(window=self.period).max()
        
        current_close = df['close'].iloc[-1]
        current_52w_high = df['52w_high'].iloc[-1]
        
        # Zero-division / missing data failsafe
        if current_52w_high == 0 or pd.isna(current_52w_high):
            return 0.0
            
        # 3. Calculate Raw Distance (Negative Percentage)
        # E.g., (100 - 120) / 120 = -0.166 (-16.6% below high)
        distance_pct = (current_close - current_52w_high) / current_52w_high
        
        # 4. Normalize via Penalty Subtraction
        # Take the absolute distance (0.166), divide by max_drawdown (0.30) = 0.55 penalty
        # Final Score = 1.0 - 0.55 = 0.45
        penalty = abs(distance_pct) / self.max_drawdown
        raw_score = 1.0 - penalty
        
        # 5. Enforce Contract Bounds
        return self.clamp(raw_score)