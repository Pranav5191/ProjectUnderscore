from .base import BaseIndicator

class TickATR(BaseIndicator):
    def __init__(self, period: int = 14, tick_chunk: int = 50):
        super().__init__("TickATR")
        self.period = period
        self.tick_chunk = tick_chunk  # How many ticks make up 1 "candle"
        
        # Isolated tracking state per security
        self.ticks_seen = {}
        self.current_high = {}
        self.current_low = {}
        self.prev_close = {}
        self.tr_history = {}

    def update(self, tick: dict):
        sec_id = str(tick.get('security_id'))
        self._initialize_state(sec_id)  # From BaseIndicator

        # Initialize ATR-specific memory blocks for new securities
        if sec_id not in self.ticks_seen:
            self.ticks_seen[sec_id] = 0
            self.current_high[sec_id] = -float('inf')
            self.current_low[sec_id] = float('inf')
            self.prev_close[sec_id] = None
            self.tr_history[sec_id] = []

        ltp = tick.get('ltp', 0.0)
        if ltp == 0.0:
            return

        # Initialize bounds on first tick
        if self.current_high[sec_id] == -float('inf'):
            self.current_high[sec_id] = ltp
            self.current_low[sec_id] = ltp

        # Track High/Low dynamically for the current chunk
        self.current_high[sec_id] = max(self.current_high[sec_id], ltp)
        self.current_low[sec_id] = min(self.current_low[sec_id], ltp)
        self.ticks_seen[sec_id] += 1

        # Once chunk is full, calculate True Range and average it
        if self.ticks_seen[sec_id] >= self.tick_chunk:
            if self.prev_close[sec_id] is None:
                tr = self.current_high[sec_id] - self.current_low[sec_id]
            else:
                tr = max(
                    self.current_high[sec_id] - self.current_low[sec_id],
                    abs(self.current_high[sec_id] - self.prev_close[sec_id]),
                    abs(self.current_low[sec_id] - self.prev_close[sec_id])
                )
            
            self.tr_history[sec_id].append(tr)
            if len(self.tr_history[sec_id]) > self.period:
                self.tr_history[sec_id].pop(0)

            # Simple Moving Average of True Range = ATR
            self.values[sec_id] = sum(self.tr_history[sec_id]) / len(self.tr_history[sec_id])

            # Reset state for the next tick chunk
            self.prev_close[sec_id] = ltp
            self.current_high[sec_id] = ltp
            self.current_low[sec_id] = ltp
            self.ticks_seen[sec_id] = 0