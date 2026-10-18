import pandas as pd
from src.scoring.base import BaseScorer

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
        # Sweet spot for non-exhausted trend continuation is +1.5% above 20-EMA.
        # Penalize stocks stretched > +8% (overbought exhaustion) or < -5% (broken trend).
        dist_from_sweet_spot = abs(raw_stretch - 0.015)
        raw_score = 1.0 - (dist_from_sweet_spot / 0.08)

        return self.clamp(raw_score)
