import pandas as pd
from src.scoring.base import BaseScorer
#Measures the exact location of the final closing price relative to the extreme daily boundaries
class ClosingRangeScorer(BaseScorer):
    def __init__(self, period: int = 1):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period:
            return 0.0
            
        range_spread = (df['high'] - df['low']).replace(0.0, 1e-10)
        closing_range = (df['close'] - df['low']) / range_spread
        
        raw_score = float(closing_range.iloc[-1])
        
        return self.clamp(raw_score)