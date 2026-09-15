class BrokerageCalculator:
    @staticmethod
    def calculate_round_trip_cost(qty: int, buy_price: float, sell_price: float) -> float:
        """Calculates exact Angel One intraday round-trip charges."""
        buy_val = qty * buy_price
        sell_val = qty * sell_price
        total_val = buy_val + sell_val

        # 1. Brokerage: Lower of ₹20 or 0.1%, with a minimum of ₹5 per order
        buy_brok = min(20.0, max(5.0, buy_val * 0.001))
        sell_brok = min(20.0, max(5.0, sell_val * 0.001))
        total_brok = buy_brok + sell_brok

        # 2. STT (Securities Transaction Tax): ~0.025% on the SELL side for intraday
        stt = sell_val * 0.00025

        # 3. Exchange Transaction Charges: ~0.00318% on total turnover
        txn_charge = total_val * 0.0000318

        # 4. SEBI Fees: ₹10 per crore (0.0001% on total turnover)
        sebi = total_val * 0.000001

        # 5. Stamp Duty: ~0.00882% on the BUY side only
        stamp = buy_val * 0.0000882

        # 6. GST: 18% on (Brokerage + Transaction Charges + SEBI Fees)
        gst = (total_brok + txn_charge + sebi) * 0.18

        total_charges = total_brok + stt + txn_charge + sebi + stamp + gst
        return total_charges