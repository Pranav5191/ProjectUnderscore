import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class BollingerPctBScorer(BaseScorer):
    def __init__(self, period: int = 20, std_dev: float = 2.0):
        # The institutional standard is 20 days and 2 standard deviations.
        # This mathematically contains ~95% of all price action.
        self.period = period
        self.std_dev = std_dev

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail
        if len(df) < self.period:
            return 0.5 # Return neutral 0.5 (sitting exactly on the SMA) if missing data
            
        # 2. Vectorized Band Calculation
        sma = df['close'].rolling(window=self.period).mean()
        std = df['close'].rolling(window=self.period).std()
        
        upper_band = sma + (self.std_dev * std)
        lower_band = sma - (self.std_dev * std)
        
        # 3. Extract Current Day Values
        current_close = df['close'].iloc[-1]
        current_upper = upper_band.iloc[-1]
        current_lower = lower_band.iloc[-1]
        
        # 4. Zero Division Protection
        # If a stock is completely flat-lined (0 volatility), Upper == Lower.
        band_width = current_upper - current_lower
        if band_width == 0 or pd.isna(band_width):
            return 0.5
            
        # 5. Calculate Raw %b
        # Formula: (Price - Lower Band) / (Upper Band - Lower Band)
        raw_pct_b = (current_close - current_lower) / band_width
        
        # 6. Enforce Contract Bounds
        # If a stock closes violently above the upper band, raw_pct_b might be 1.15. 
        # clamp() gracefully forces it to exactly 1.0.
        return self.clamp(raw_pct_b)