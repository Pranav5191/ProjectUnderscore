import pandas as pd
from src.scoring.base import BaseScorer
#Measures the absolute strength of a trend, regardless of whether the price is rising or falling, by comparing the magnitude of price expansion over a set period.
class AverageDirectionalIndexScorer(BaseScorer):
    def __init__(self, period: int = 14):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < (self.period * 2):
            return 0.0
            
        prev_close = df['close'].shift(1)
        
        # True Range components
        tr1 = df['high'] - df['low']
        tr2 = (df['high'] - prev_close).abs()
        tr3 = (df['low'] - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # Directional Movement components
        up_move = df['high'] - df['high'].shift(1)
        down_move = df['low'].shift(1) - df['low']
        
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0)
        
        # Wilder's Smoothing via Exponential Moving Average
        atr = tr.ewm(alpha=1.0/self.period, min_periods=self.period, adjust=False).mean()
        plus_dm_smoothed = plus_dm.ewm(alpha=1.0/self.period, min_periods=self.period, adjust=False).mean()
        minus_dm_smoothed = minus_dm.ewm(alpha=1.0/self.period, min_periods=self.period, adjust=False).mean()
        
        # Directional Indicators (+DI and -DI)
        plus_di = 100.0 * (plus_dm_smoothed / atr)
        minus_di = 100.0 * (minus_dm_smoothed / atr)
        
        # Directional Index (DX)
        dx_denominator = (plus_di + minus_di).replace(0.0, 1e-10)
        dx = 100.0 * (plus_di - minus_di).abs() / dx_denominator
        
        # Average Directional Index (ADX)
        adx = dx.ewm(alpha=1.0/self.period, min_periods=self.period, adjust=False).mean()
        
        if pd.isna(adx.iloc[-1]):
            return 0.0
            
        last_adx = float(adx.iloc[-1])
        raw_score = last_adx / 50.0
        
        return self.clamp(raw_score)