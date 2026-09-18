import pandas as pd
from src.scoring.base import BaseScorer
class IntradayAmplitudeScorer(BaseScorer):
    def __init__(self, period: int = 10):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period + 1:
            return 0.0
            
        daily_amplitude = (df['high'] - df['low']) / df['low']
        rolling_mean_amp = daily_amplitude.rolling(window=self.period).mean()
        
        last_mean_amp = float(rolling_mean_amp.iloc[-1])
        raw_score = last_mean_amp / 0.06
        
        return self.clamp(raw_score)