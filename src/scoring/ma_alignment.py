import pandas as pd
from src.scoring.base import BaseScorer

class MovingAverageAlignmentScorer(BaseScorer):
    def __init__(self):
        # The weights for our 4 trend conditions. They sum perfectly to 1.0.
        self.w_price_action = 0.20  # Price > 10 EMA
        self.w_short_trend = 0.30   # 10 EMA > 20 EMA
        self.w_macro_trend = 0.30   # 20 EMA > 50 SMA
        self.w_slope = 0.20         # 50 SMA is pointing UP

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail: We need 50 days to calculate a 50 SMA, 
        # plus 5 extra days to calculate the SMA slope.
        if len(df) < 55:
            return 0.0
            
        # 2. Vectorized Moving Averages
        # adjust=False mimics the exact math used by trading platforms like TradingView
        ema10 = df['close'].ewm(span=10, adjust=False).mean()
        ema20 = df['close'].ewm(span=20, adjust=False).mean()
        sma50 = df['close'].rolling(window=50).mean()
        
        # 3. Extract the current day's values
        current_close = df['close'].iloc[-1]
        current_ema10 = ema10.iloc[-1]
        current_ema20 = ema20.iloc[-1]
        current_sma50 = sma50.iloc[-1]
        
        # Look back 5 days to see if the 50 SMA is rising or falling
        past_sma50 = sma50.iloc[-5] 
        
        # 4. The Boolean Matrix Score Calculation
        raw_score = 0.0
        
        if current_close > current_ema10:
            raw_score += self.w_price_action
            
        if current_ema10 > current_ema20:
            raw_score += self.w_short_trend
            
        if current_ema20 > current_sma50:
            raw_score += self.w_macro_trend
            
        if current_sma50 > past_sma50:
            raw_score += self.w_slope
            
        # 5. Enforce Contract Bounds
        return self.clamp(raw_score)