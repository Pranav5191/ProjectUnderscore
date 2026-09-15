class RiskManager:
    def __init__(self, base_risk_pct: float = 0.10):
        # The absolute maximum percentage of free cash you will risk on a 1.0 (Perfect) setup
        self.base_risk_pct = base_risk_pct

    def calculate_confidence(self, obi: float, cvd: float, spread: float) -> float:
        """Converts raw indicators into a 0.0 to 1.0 Confidence Score."""
        # 1. Normalize OBI (Max strength is 1.0)
        obi_strength = abs(obi)
        
        # 2. Normalize CVD (Assuming 2000 delta is a 'maximum' benchmark)
        cvd_strength = min(abs(cvd) / 2000.0, 1.0)
        
        # 3. Spread Penalty (If spread is tighter than 0.10 rupees, no penalty. Else, cut confidence in half)
        spread_penalty = 1.0 if spread < 0.10 else 0.5
        
        # Weighting: 60% Order Book, 40% Volume Delta
        confidence = ((obi_strength * 0.6) + (cvd_strength * 0.4)) * spread_penalty
        
        # Cap at 1.0 just to be mathematically safe
        return min(confidence, 1.0)

    def calculate_position_size(self, confidence: float, free_cash: float, ltp: float, available_vol: int) -> int:
        """Calculates dynamic quantity based on confidence, portfolio heat, and order book liquidity."""
        
        # Reject weak setups outright (e.g., Confidence below 30%)
        if confidence < 0.30:
            return 0

        # Calculate how much cash we are allowed to use
        target_margin = free_cash * self.base_risk_pct * confidence
        
        # Calculate theoretical shares
        if ltp == 0:
            return 0
        theoretical_qty = int(target_margin // ltp)
        
        # Liquidity Cap: Never buy more than what is available at the best price level
        final_qty = min(theoretical_qty, available_vol)
        
        return final_qty