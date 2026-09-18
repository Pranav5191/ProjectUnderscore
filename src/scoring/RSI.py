import pandas as pd
from src.scoring.base import BaseScorer
#Measures the speed and magnitude of recent price changes t
class RSIScorer(BaseScorer):
    def __init__(self, period: int = 14):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period + 1:
            return 0.0
            
        delta = df['close'].diff()
        gain = delta.clip(lower=0.0)
        loss = -1.0 * delta.clip(upper=0.0)
        
        avg_gain = gain.ewm(alpha=1.0/self.period, min_periods=self.period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1.0/self.period, min_periods=self.period, adjust=False).mean()
        
        last_gain = float(avg_gain.iloc[-1])
        last_loss = float(avg_loss.iloc[-1])
        
        if last_loss == 0.0:
            raw_rsi = 100.0 
        else:
            rs = last_gain / last_loss
            raw_rsi = 100.0 - (100.0 / (1.0 + rs))
            
        raw_score = raw_rsi / 100.0
        
        return self.clamp(raw_score)