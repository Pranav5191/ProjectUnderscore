from .base import BaseIndicator

class OrderBookImbalance(BaseIndicator):
    def __init__(self):
        super().__init__("OBI")

    def update(self, tick: dict):
        bid_vol = tick.get('best_bid_vol', 0)
        ask_vol = tick.get('best_ask_vol', 0)
        
        total_vol = bid_vol + ask_vol
        
        if total_vol == 0:
            self.value = 0.0 
        else:
            self.value = (bid_vol - ask_vol) / total_vol