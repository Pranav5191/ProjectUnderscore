import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class HistoricalVolatilityScorer(BaseScorer):
    def __init__(self, period: int = 20, max_expected_hv: float = 0.60):
        self.period = period
        self.max_expected_hv = max_expected_hv

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period + 1:
            return 0.0

        df = df.copy()
        df['log_ret'] = np.log(df['close'] / df['close'].shift(1))

        daily_std = df['log_ret'].rolling(window=self.period).std().iloc[-1]
        annualized_hv = daily_std * np.sqrt(252)
        raw_score = annualized_hv / self.max_expected_hv

        return self.clamp(raw_score)
