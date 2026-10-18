from collections import deque
from .base import BaseIndicator

class CumulativeVolumeDelta(BaseIndicator):
    def __init__(self, window: int = 500):
        super().__init__("CVD")
        self.window = window
        self._deltas = {}
        self._prev_ltp = {}

    def update(self, tick: dict):
        sec_id = str(tick.get('security_id'))
        self._initialize_state(sec_id)

        if sec_id not in self._deltas:
            self._deltas[sec_id] = deque(maxlen=self.window)
            self._prev_ltp[sec_id] = 0.0

        ltp = float(tick.get('ltp', 0.0))
        ltq = int(tick.get('ltq', 0))
        bid = float(tick.get('bid', 0.0))
        ask = float(tick.get('ask', 0.0))

        if not sec_id or ltp <= 0.0 or ltq <= 0:
            return

        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else ltp
        prev_p = self._prev_ltp[sec_id]
        self._prev_ltp[sec_id] = ltp

        if ltp > mid:
            delta = ltq
        elif ltp < mid:
            delta = -ltq
        elif prev_p > 0 and ltp != prev_p:
            delta = ltq if ltp > prev_p else -ltq
        else:
            delta = 0

        self._deltas[sec_id].append(delta)
        self.values[sec_id] = sum(self._deltas[sec_id])