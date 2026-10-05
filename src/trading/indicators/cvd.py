from collections import deque
from .base import BaseIndicator

class CumulativeVolumeDelta(BaseIndicator):
    def __init__(self, window: int = 500):
        super().__init__("CVD")
        self.window = window
        self._deltas = {}  # Per-security rolling delta buffer

    def update(self, tick: dict):
        sec_id = str(tick.get('security_id'))
        self._initialize_state(sec_id)

        if sec_id not in self._deltas:
            self._deltas[sec_id] = deque(maxlen=self.window)

        ltp = tick.get('ltp', 0.0)
        ltq = tick.get('ltq', 0)
        bid = tick.get('bid', 0.0)
        ask = tick.get('ask', 0.0)

        # Protect against dead ticks missing core data
        if not sec_id or bid == 0.0 or ask == 0.0 or ltp == 0.0 or ltq == 0:
            return

        mid = (bid + ask) / 2.0

        # Determine aggression based on where the trade executed relative to the mid-price
        if ltp > mid:
            delta = ltq   # Buyer crossed the spread
        elif ltp < mid:
            delta = -ltq  # Seller crossed the spread
        else:
            delta = 0     # Trade at mid — ambiguous, ignore

        self._deltas[sec_id].append(delta)
        self.values[sec_id] = sum(self._deltas[sec_id])