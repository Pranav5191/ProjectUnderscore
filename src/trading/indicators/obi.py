from .base import BaseIndicator

class OrderBookImbalance(BaseIndicator):
    def __init__(self):
        super().__init__("OBI")

    def update(self, tick: dict):
        sec_id = str(tick.get('security_id'))
        self._initialize_state(sec_id)

        bid_vol = tick.get('best_bid_vol', 0)
        ask_vol = tick.get('best_ask_vol', 0)
        
        total_vol = bid_vol + ask_vol
        
        if total_vol == 0:
            self.values[sec_id] = 0.0 
        else:
            self.values[sec_id] = (bid_vol - ask_vol) / total_vol