from .base import BaseIndicator

class BidAskSpread(BaseIndicator):
    def __init__(self):
        super().__init__("Normalized_Spread")

    def update(self, tick: dict):
        sec_id = str(tick.get('security_id'))
        self._initialize_state(sec_id)

        bid = tick.get('bid', 0.0)
        ask = tick.get('ask', 0.0)
        
        # Protect against dead ticks and division by zero
        if ask == 0.0 or bid == 0.0:
            self.values[sec_id] = 0.0
        else:
            # Normalized spread ratio
            self.values[sec_id] = (ask - bid) / ask