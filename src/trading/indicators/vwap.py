from .base import BaseIndicator

class TickVWAP(BaseIndicator):
    def __init__(self):
        super().__init__("TickVWAP")
        # Isolate memory for cumulative Price*Volume and Volume per security
        self.cumulative_pv = {}
        self.cumulative_vol = {}

    def update(self, tick: dict):
        sec_id = str(tick.get('security_id'))
        self._initialize_state(sec_id)

        if sec_id not in self.cumulative_pv:
            self.cumulative_pv[sec_id] = 0.0
            self.cumulative_vol[sec_id] = 0

        ltp = float(tick.get('ltp', 0.0))
        # Indian broker tick data usually passes volume as 'last_traded_quantity' or 'ltq'
        vol = float(tick.get('last_traded_quantity', tick.get('ltq', tick.get('v', 1.0))))

        if ltp == 0.0 or vol == 0.0:
            return

        # VWAP Math: Sum(Price * Volume) / Sum(Volume)
        self.cumulative_pv[sec_id] += (ltp * vol)
        self.cumulative_vol[sec_id] += vol

        if self.cumulative_vol[sec_id] > 0:
            self.values[sec_id] = self.cumulative_pv[sec_id] / self.cumulative_vol[sec_id]
        else:
            self.values[sec_id] = ltp