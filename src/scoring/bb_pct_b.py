import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class BollingerPctBScorer(BaseScorer):
    def __init__(self, period: int = 20, std_dev: float = 2.0):
        self.period = period
        self.std_dev = std_dev

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period:
            return 0.5

        sma = df['close'].rolling(window=self.period).mean()
        std = df['close'].rolling(window=self.period).std()

        upper_band = sma + (self.std_dev * std)
        lower_band = sma - (self.std_dev * std)

        current_close = float(df['close'].iloc[-1])
        current_upper = float(upper_band.iloc[-1])
        current_lower = float(lower_band.iloc[-1])

        band_width = current_upper - current_lower
        if band_width == 0 or pd.isna(band_width):
            return 0.5

        raw_pct_b = (current_close - current_lower) / band_width
        # Ideal non-exhausted momentum zone is %b = 0.65; penalize >1.05 blowoffs
        raw_score = 1.0 - (abs(raw_pct_b - 0.65) / 0.65)
        return self.clamp(raw_score)
