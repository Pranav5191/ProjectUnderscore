import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class ChaikinMoneyFlowScorer(BaseScorer):
    def __init__(self, period: int = 21):
        # 21 days is the institutional standard for CMF (roughly one trading month).
        self.period = period

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail: Ensure enough data
        if len(df) < self.period:
            return 0.5 # Return neutral 0.5 (which equals 0.0 raw CMF) if data is missing
            
        # 2. Prevent Division by Zero (Circuit Limits / Zero-Tick Days)
        # If high == low, the divisor becomes 0. We inject a micro-decimal (1e-6) to prevent a crash.
        high_low_spread = df['high'] - df['low']
        divisor = np.where(high_low_spread == 0, 1e-6, high_low_spread)
        
        # 3. Calculate Money Flow Multiplier (MFM)
        # Value between -1.0 (closed at absolute low) and 1.0 (closed at absolute high)
        mfm = ((df['close'] - df['low']) - (df['high'] - df['close'])) / divisor
        
        # 4. Calculate Money Flow Volume (MFV)
        mfv = mfm * df['volume']
        
        # 5. Calculate 21-Day Sums
        sum_mfv = mfv.rolling(window=self.period).sum().iloc[-1]
        sum_vol = df['volume'].rolling(window=self.period).sum().iloc[-1]
        
        # 6. Final Raw CMF Calculation
        if sum_vol == 0:
            return 0.5 # Failsafe for entirely dead stocks
            
        raw_cmf = sum_mfv / sum_vol # Bounded strictly between -1.0 and 1.0
        
        # 7. Normalize to 0.0 - 1.0 range
        # Shift the -1.0 to 1.0 scale into a 0.0 to 2.0 scale, then halve it.
        normalized_score = (raw_cmf + 1.0) / 2.0
        
        # 8. Enforce Contract Bounds
        return self.clamp(normalized_score)