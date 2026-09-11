from .base import BaseIndicator

class BidAskSpread(BaseIndicator):
    def __init__(self):
        super().__init__("Normalized_Spread")

    def update(self, tick: dict):
        bid = tick.get('bid', 0.0)
        ask = tick.get('ask', 0.0)
        
        # Protect against dead ticks and division by zero
        if ask == 0.0 or bid == 0.0:
            self.value = 0.0
        else:
            # Normalized spread ratio
            self.value = (ask - bid) / ask