from .base import BaseIndicator

class OrderBookImbalance(BaseIndicator):
    def __init__(self, alpha: float = 0.1):
        super().__init__("OBI")
        self.alpha = alpha  # EMA smoothing factor (lower = smoother)
        self._raw_obi = {}  # Store raw OBI for smoothing

    def update(self, tick: dict):
        sec_id = str(tick.get('security_id'))
        self._initialize_state(sec_id)

        bid_vol = tick.get('best_bid_vol', 0)
        ask_vol = tick.get('best_ask_vol', 0)

        total_vol = bid_vol + ask_vol

        if total_vol == 0:
            raw_obi = 0.0
        else:
            raw_obi = (bid_vol - ask_vol) / total_vol

        # Apply EMA smoothing to reduce L1 noise and spoofing susceptibility
        if sec_id not in self._raw_obi:
            self._raw_obi[sec_id] = raw_obi
        else:
            self._raw_obi[sec_id] = (self.alpha * raw_obi) + ((1.0 - self.alpha) * self._raw_obi[sec_id])

        self.values[sec_id] = self._raw_obi[sec_id]