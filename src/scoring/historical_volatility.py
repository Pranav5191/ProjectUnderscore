import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class HistoricalVolatilityScorer(BaseScorer):
    def __init__(self, period: int = 20, max_expected_hv: float = 0.60):
        # max_expected_hv = 0.60 means an annualized volatility of 60%.
        # Highly stable stocks hover around 0.15 - 0.25. 
        self.period = period
        self.max_expected_hv = max_expected_hv

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail: Need enough data for the shift(1) and the rolling window
        if len(df) < self.period + 1:
            return 0.0
            
        # 2. Vectorized Log Returns
        # np.log(today / yesterday)
        df['log_ret'] = np.log(df['close'] / df['close'].shift(1))
        
        # 3. Calculate Standard Deviation and Annualize it
        # There are exactly 252 trading days in the Indian market year
        daily_std = df['log_ret'].rolling(window=self.period).std().iloc[-1]
        annualized_hv = daily_std * np.sqrt(252)
        
        # 4. Normalize to 0.0 - 1.0 range
        raw_score = annualized_hv / self.max_expected_hv
        
        # 5. Enforce Contract Bounds
        return self.clamp(raw_score)