from .base import BaseIndicator

class CumulativeVolumeDelta(BaseIndicator):
    def __init__(self):
        super().__init__("CVD")
        # Track state per security
        self.deltas = {}

    def update(self, tick: dict):
        sec_id = tick.get('security_id')
        ltp = tick.get('ltp', 0.0)
        ltq = tick.get('ltq', 0)
        bid = tick.get('bid', 0.0)
        ask = tick.get('ask', 0.0)

        # Protect against dead ticks missing core data
        if not sec_id or bid == 0.0 or ask == 0.0 or ltp == 0.0:
            return

        mid = (bid + ask) / 2.0
        
        if sec_id not in self.deltas:
            self.deltas[sec_id] = 0

        # Determine aggression based on where the trade executed relative to the mid-price
        if ltp > mid:
            self.deltas[sec_id] += ltq  # Buyer crossed the spread
        elif ltp < mid:
            self.deltas[sec_id] -= ltq  # Seller crossed the spread
        
        # self.value holds the CVD for the most recently processed tick
        self.value = self.deltas[sec_id]