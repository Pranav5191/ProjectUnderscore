from .base import BaseIndicator

class WeightedMidPrice(BaseIndicator):
    def __init__(self):
        super().__init__("WMP")

    def update(self, tick: dict):
        sec_id = str(tick.get('security_id'))
        self._initialize_state(sec_id)

        bid = tick.get('bid', 0.0)
        ask = tick.get('ask', 0.0)
        bid_vol = tick.get('best_bid_vol', 0)
        ask_vol = tick.get('best_ask_vol', 0)

        total_vol = bid_vol + ask_vol

        # Fallback to standard mid if volume data drops to prevent zero-division
        if total_vol == 0 or bid == 0.0 or ask == 0.0:
            self.values[sec_id] = (bid + ask) / 2.0 if (bid and ask) else 0.0
        else:
            # Cross-weighted math
            self.values[sec_id] = (bid * ask_vol + ask * bid_vol) / total_vol