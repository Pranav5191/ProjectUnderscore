from src.trading.portfolio import PortfolioManager
from src.trading.audit_logger import AuditLogger
from src.trading.dhan_sandbox import DhanSandboxClient

class ExecutionEngine:
    def __init__(self, portfolio: PortfolioManager, logger: AuditLogger):
        self.portfolio = portfolio
        self.logger = logger
        self.simulated_latency = 0.0
        self.dhan_sandbox = DhanSandboxClient()

    def _calculate_indian_taxes(self, action: str, qty: int, price: float) -> float:
        turnover = float(qty * price)
        calculated_brokerage = turnover * 0.001
        brokerage = max(5.0, min(20.0, calculated_brokerage))
        exchange_txn_charge = turnover * 0.000030699
        sebi_fee = turnover * 0.000001
        gst = (brokerage + exchange_txn_charge + sebi_fee) * 0.18
        stt = (turnover * 0.00025) if action == 'SELL' else 0.0
        stamp_duty = (turnover * 0.00003) if action == 'BUY' else 0.0
        return round(brokerage + exchange_txn_charge + sebi_fee + gst + stt + stamp_duty, 2)

    def execute_paper_trade(
        self, tick: dict, action: str, qty: int, trace_id: str = "NO_TRACE",
        bypass_margin: bool = False, symbol: str = "", trigger_source: str = "", trigger_reason: str = ""
    ) -> str:
        sec_id = str(tick.get('security_id'))
        tick_timestamp = tick.get('timestamp')

        maker_price = (
            tick.get('bid') if action == 'BUY' else tick.get('ask')
        ) or tick.get('ltp') or tick.get('price') or 0.0

        if not maker_price or maker_price <= 0:
            return "REJECTED_INVALID_PRICE"

        notional_required = float(maker_price) * qty
        current_pos = self.portfolio.positions.get(sec_id)
        is_closing_trade = current_pos is not None and current_pos['side'] != action

        if not bypass_margin and not is_closing_trade:
            allowed, reject_reason = self.portfolio.can_take_trade(sec_id, action, notional_required)
            if not allowed:
                return reject_reason

        fill_price = float(maker_price)
        latency_ms = self.simulated_latency * 1000.0

        if self.dhan_sandbox.is_enabled():
            sbx_res = self.dhan_sandbox.place_intraday_order(
                sec_id=sec_id, symbol=symbol or f"TOK_{sec_id}",
                action=action, qty=qty, price=fill_price, trace_id=trace_id
            )
            latency_ms = sbx_res.get("latency_ms", latency_ms)
            trigger_reason = f"{trigger_reason} | {sbx_res.get('order_id', '')}"

        transaction_taxes = self._calculate_indian_taxes(action, qty, fill_price)
        gross_booked_pnl = self.portfolio.update_position(
            sec_id, action, qty, fill_price, symbol=symbol, trigger_source=trigger_source
        )
        self.portfolio.current_balance -= transaction_taxes
        net_booked_pnl = gross_booked_pnl - transaction_taxes

        self.logger.log_trade(
            trace_id, tick_timestamp, sec_id, action, fill_price, qty,
            latency_ms, 0.0,
            gross_booked_pnl, transaction_taxes, net_booked_pnl, self.portfolio.current_balance,
            symbol=symbol, trigger_source=trigger_source, trigger_reason=trigger_reason
        )
        return "EXECUTED"
