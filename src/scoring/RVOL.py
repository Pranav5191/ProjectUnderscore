#relative Volume Scorer
import pandas as pd
from src.scoring.base import BaseScorer

class RelativeVolumeScorer(BaseScorer):
    def __init__(self, period: int = 20):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period + 1:
            return 0.0
            
        volume = df['volume']
        sma_volume = volume.rolling(window=self.period).mean()
        
        last_volume = float(volume.iloc[-1])
        last_sma = float(sma_volume.iloc[-1])
        
        if last_sma == 0.0:
            return 0.0
            
        raw_rvol = last_volume / last_sma
        raw_score = raw_rvol / 3.0
        
        return self.clamp(raw_score)