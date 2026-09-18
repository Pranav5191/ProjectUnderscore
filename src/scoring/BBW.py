#Bollinger Band Width
import pandas as pd
from src.scoring.base import BaseScorer
class BollingerBandWidthScorer(BaseScorer):
    def __init__(self, period: int = 20, std_dev: float = 2.0):
        self.period = period
        self.std_dev = std_dev

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period + 1:
            return 0.0
            
        close_prices = df['close']
        
        sma = close_prices.rolling(window=self.period).mean()
        std = close_prices.rolling(window=self.period).std()
        
        upper_band = sma + (self.std_dev * std)
        lower_band = sma - (self.std_dev * std)
        
        last_upper = float(upper_band.iloc[-1])
        last_lower = float(lower_band.iloc[-1])
        last_sma = float(sma.iloc[-1])
        
        if last_sma == 0.0:
            return 0.0
            
        raw_bbw = (last_upper - last_lower) / last_sma
        raw_score = raw_bbw / 0.15
        
        return self.clamp(raw_score)