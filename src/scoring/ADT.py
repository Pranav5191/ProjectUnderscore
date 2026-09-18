import pandas as pd
from src.scoring.base import BaseScorer
class AverageDailyTurnoverScorer(BaseScorer):
    def __init__(self, period: int = 20):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period + 1:
            return 0.0
            
        daily_turnover = df['close'] * df['volume']
        rolling_adt = daily_turnover.rolling(window=self.period).mean()
        
        last_adt = float(rolling_adt.iloc[-1])
        raw_score = last_adt / 1000000000.0
        
        return self.clamp(raw_score)