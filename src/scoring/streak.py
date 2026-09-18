import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class ConsecutiveDaysStreakScorer(BaseScorer):
    def __init__(self, max_streak: int = 5):
        # A 5-day continuous streak in one direction is our institutional ceiling.
        # Anything beyond 5 days gets clamped because the move is likely exhausted.
        self.max_streak = max_streak

    def calculate(self, df: pd.DataFrame) -> float:
        # 1. Guardrail
        if len(df) < 2:
            return 0.5 # Neutral if missing data
            
        # 2. Vectorized Directionality
        # +1 for up days, -1 for down days, 0 for flat days
        delta = df['close'] - df['close'].shift(1)
        direction = np.sign(delta)
        
        # 3. Vectorized Block Identification (The cumsum trick)
        # .diff().ne(0) returns True only on the exact day the trend flips direction.
        direction_changes = direction.diff().ne(0)
        
        # .cumsum() increments the ID every time it sees a True. 
        # This groups all consecutive identical days into a single block ID.
        streak_blocks = direction_changes.cumsum()
        
        # 4. Count the consecutive days inside each block
        streak_length = df.groupby(streak_blocks).cumcount() + 1
        
        # 5. Apply the direction (+ length for green streaks, - length for red streaks)
        actual_streak = streak_length * direction
        
        # 6. Extract Today's Exact Streak
        current_streak = actual_streak.iloc[-1]
        
        if pd.isna(current_streak):
            return 0.5
            
        # 7. Normalize (Shift and Scale)
        # First, hard-cap the raw streak between -5 and +5
        capped_streak = max(-self.max_streak, min(self.max_streak, current_streak))
        
        # Shift the scale: a -5 penalty becomes 0, flat 0 becomes 5, +5 streak becomes 10
        shifted_streak = capped_streak + self.max_streak
        
        # Divide by the total mathematical range (10) to map to 0.0 - 1.0
        raw_score = shifted_streak / (2.0 * self.max_streak)
        
        # 8. Enforce Contract Bounds
        return self.clamp(raw_score)