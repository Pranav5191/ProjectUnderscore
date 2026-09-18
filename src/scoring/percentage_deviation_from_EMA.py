import pandas as pd
from src.scoring.base import BaseScorer
#Measures the percentage deviation of the current closing price from its Exponential Moving Average (EMA) to identify overextended price conditions and potential mean-reversion zones.
#percentage deviation from EMA = (current close - EMA) / EMA
class PriceToEMAStretchScorer(BaseScorer):
    def __init__(self, period: int = 20):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period + 1:
            return 0.0
            
        close_prices = df['close']
        ema = close_prices.ewm(span=self.period, adjust=False).mean()
        
        last_close = float(close_prices.iloc[-1])
        last_ema = float(ema.iloc[-1])
        
        if last_ema == 0.0:
            return 0.0
            
        raw_stretch = (last_close - last_ema) / last_ema
        raw_score = (raw_stretch + 0.08) / 0.16
        
        return self.clamp(raw_score)