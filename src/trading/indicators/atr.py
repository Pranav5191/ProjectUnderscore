from .base import BaseIndicator

class TickATR(BaseIndicator):
    def __init__(self, period: int = 14, tick_chunk: int = 50):
        super().__init__("TickATR")
        self.period = period
        self.tick_chunk = tick_chunk  # How many ticks make up 1 "candle"
        self.ticks_seen = 0
        
        self.current_high = -float('inf')
        self.current_low = float('inf')
        self.prev_close = None
        self.tr_history = []

    def update(self, tick: dict):
        ltp = tick.get('ltp', 0.0)
        if ltp == 0.0:
            return

        # Initialize bounds on first tick
        if self.current_high == -float('inf'):
            self.current_high = ltp
            self.current_low = ltp

        # Track High/Low dynamically for the current chunk
        self.current_high = max(self.current_high, ltp)
        self.current_low = min(self.current_low, ltp)
        self.ticks_seen += 1

        # Once chunk is full, calculate True Range and average it
        if self.ticks_seen >= self.tick_chunk:
            if self.prev_close is None:
                tr = self.current_high - self.current_low
            else:
                tr = max(
                    self.current_high - self.current_low,
                    abs(self.current_high - self.prev_close),
                    abs(self.current_low - self.prev_close)
                )
            
            self.tr_history.append(tr)
            if len(self.tr_history) > self.period:
                self.tr_history.pop(0)

            # Simple Moving Average of True Range = ATR
            self.value = sum(self.tr_history) / len(self.tr_history)

            # Reset state for the next tick chunk
            self.prev_close = ltp
            self.current_high = ltp
            self.current_low = ltp
            self.ticks_seen = 0