import time
from src.trading.portfolio import PortfolioManager
from src.trading.audit_logger import AuditLogger

class ExecutionEngine:
    def __init__(self, portfolio: PortfolioManager, logger: AuditLogger):
        self.portfolio = portfolio
        self.logger = logger
        self.simulated_latency = 0.0

    def _calculate_indian_taxes(self, action: str, qty: int, price: float) -> float:
        """Calculates exact Indian Equity Intraday taxes and dynamic brokerage."""
        turnover = float(qty * price)
        
        # 1. Dynamic Brokerage: Min ₹5, Max ₹20, or 0.1% of turnover
        calculated_brokerage = turnover * 0.001
        brokerage = max(5.0, min(20.0, calculated_brokerage))
        
        # 2. Exchange Transaction Charge (NSE: ~0.0030699%)
        exchange_txn_charge = turnover * 0.000030699
        
        # 3. SEBI Turnover Fee (₹10 per crore = 0.0001%)
        sebi_fee = turnover * 0.000001
        
        # 4. GST (18% on Brokerage + Exchange Charge + SEBI Fee)
        gst = (brokerage + exchange_txn_charge + sebi_fee) * 0.18
        
        # 5. STT (0.025% on the SELL side only for intraday equity)
        stt = (turnover * 0.00025) if action == 'SELL' else 0.0
        
        # 6. Stamp Duty (0.003% on the BUY side only)
        stamp_duty = (turnover * 0.00003) if action == 'BUY' else 0.0
        
        total_taxes = brokerage + exchange_txn_charge + sebi_fee + gst + stt + stamp_duty
        return round(total_taxes, 2)

    def execute_paper_trade(self, tick: dict, action: str, qty: int, trace_id: str = "NO_TRACE") -> str:
        sec_id = tick.get('security_id')
        tick_timestamp = tick.get('timestamp')
        
        # ==========================================
        # PRICING LOGIC: LIQUIDITY MAKER (LIMIT ORDER)
        # ==========================================
        # Instead of crossing the spread (Taker: Buying at Ask, Selling at Bid),
        # we provide liquidity (Maker: Buying at Bid, Selling at Ask).
        maker_price = (
            tick.get('bid') if action == 'BUY' else tick.get('ask')
        ) or tick.get('ltp') or tick.get('price') or 0.0

        if not maker_price or maker_price <= 0:
            print(f"[EXECUTION WARNING] Invalid maker_price ({maker_price}) for {trace_id}. Skipping trade.")
            return "REJECTED_INVALID_PRICE"

        margin_required = float(maker_price) * qty

        # ==========================================
        # GATE 1: Pyramiding / Duplicate Order Check
        # ==========================================
        current_pos = self.portfolio.positions.get(sec_id)
        if current_pos and current_pos['side'] == action:
            return "REJECTED_POSITION_EXISTS"

        # Determine if this is a closing trade (opposite side of existing position)
        is_closing_trade = current_pos is not None and current_pos['side'] != action

        if not is_closing_trade:
            # ==========================================
            # GATE 2: Margin Check (only for new entries)
            # ==========================================
            margin_used = sum(pos['qty'] * pos['avg_price'] for pos in self.portfolio.positions.values())
            available_cash = self.portfolio.current_balance - margin_used
            if available_cash < margin_required:
                return "REJECTED_INSUFFICIENT_FUNDS"

            # ==========================================
            # GATE 3: Secondary Portfolio Limits (only for new entries)
            # ==========================================
            if not self.portfolio.can_take_trade(sec_id, action, margin_required):
                return "REJECTED_PORTFOLIO_LIMITS"

        # ==========================================
        # EXECUTION (MAKER)
        # ==========================================
        
        # As a Liquidity Maker, we dictate the price via Limit Orders. 
        # Slippage penalty is reduced to exactly zero. We capture the spread.
        slippage = 0.0
        fill_price = maker_price

        # Calculate exact Indian market taxes for this leg
        transaction_taxes = self._calculate_indian_taxes(action, qty, fill_price)

        # Update portfolio & calculate gross realized PnL
        gross_booked_pnl = self.portfolio.update_position(sec_id, action, qty, fill_price)
        
        # Physically deduct taxes from the portfolio balance instantly
        self.portfolio.current_balance -= transaction_taxes
        
        # Net PnL reflects the true cost of doing business
        net_booked_pnl = gross_booked_pnl - transaction_taxes
        total_balance = self.portfolio.current_balance

        # Log to your CSV Vault
        self.logger.log_trade(
            trace_id, tick_timestamp, sec_id, action, fill_price, qty, 
            self.simulated_latency * 1000, slippage, 
            gross_booked_pnl, transaction_taxes, net_booked_pnl, total_balance
        )

        return "EXECUTED"