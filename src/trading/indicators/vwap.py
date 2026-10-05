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
        # Handle both live (last_traded_quantity) and replay (ltq) tick formats
        raw_vol = tick.get('last_traded_quantity') or tick.get('ltq') or tick.get('v')
        vol = float(raw_vol) if raw_vol is not None and float(raw_vol) > 0 else 0.0

        if ltp == 0.0 or vol <= 0.0:
            return

        # VWAP Math: Sum(Price * Volume) / Sum(Volume)
        self.cumulative_pv[sec_id] += (ltp * vol)
        self.cumulative_vol[sec_id] += vol

        if self.cumulative_vol[sec_id] > 0:
            self.values[sec_id] = self.cumulative_pv[sec_id] / self.cumulative_vol[sec_id]
        else:
            self.values[sec_id] = ltp