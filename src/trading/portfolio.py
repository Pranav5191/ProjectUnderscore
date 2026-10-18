import json
import os

class PortfolioManager:
    def __init__(
        self,
        starting_balance: float = 100000.0,
        max_allocation_pct: float = 0.20,
        max_loss_pct: float = 0.02,
        max_concurrent_positions: int = None,
        state_file: str = "portfolio_state.json"
    ):
        self.state_file = os.path.join(os.path.dirname(__file__), state_file)
        loaded_balance = self._load_state(starting_balance)

        self.initial_balance = loaded_balance
        self.current_balance = loaded_balance
        self.max_allocation_pct = max_allocation_pct
        self.max_loss_pct = max_loss_pct
        self.max_concurrent_positions = int(
            max_concurrent_positions or os.getenv("MAX_CONCURRENT_POSITIONS", 4)
        )

        self.positions = {}
        self.daily_pnl = 0.0
        self.peak_capital_deployed = 0.0
        self.total_turnover_deployed = 0.0

    def _load_state(self, default_balance: float) -> float:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return float(data.get("current_balance", default_balance))
            except Exception:
                pass
        return default_balance

    def save_state(self):
        with open(self.state_file, 'w', encoding='utf-8') as f:
            json.dump({
                "current_balance": round(self.current_balance, 2),
                "initial_balance": round(self.initial_balance, 2),
                "daily_pnl": round(self.daily_pnl, 2),
                "peak_capital_deployed": round(self.peak_capital_deployed, 2),
                "total_turnover_deployed": round(self.total_turnover_deployed, 2),
                "max_concurrent_positions": self.max_concurrent_positions
            }, f, indent=4)

    def get_active_notional(self) -> float:
        return sum(pos['qty'] * pos['avg_price'] for pos in self.positions.values())

    def get_margin_used(self) -> float:
        return self.get_active_notional() / 5.0

    def get_idle_capital(self) -> float:
        return max(0.0, self.current_balance - self.get_margin_used())

    def can_take_trade(self, sec_id, action: str, requested_notional: float) -> tuple[bool, str]:
        sec_id = str(sec_id)
        if self.daily_pnl <= -(self.initial_balance * self.max_loss_pct):
            return False, "REJECTED_MAX_DAILY_DRAWDOWN"

        if sec_id not in self.positions and len(self.positions) >= self.max_concurrent_positions:
            return False, f"REJECTED_MAX_SLOTS_{self.max_concurrent_positions}"

        # Exact live portfolio limit: single position notional <= current_balance * max_allocation_pct (20%)
        if (requested_notional / 5.0) > (self.current_balance * self.max_allocation_pct):
            return False, "REJECTED_PORTFOLIO_LIMITS"

        if (requested_notional / 5.0) > self.get_idle_capital():
            return False, "REJECTED_INSUFFICIENT_FUNDS"

        return True, "OK"

    def update_position(self, sec_id, action: str, qty: int, fill_price: float, symbol: str = "", trigger_source: str = "") -> float:
        sec_id = str(sec_id)
        pos = self.positions.get(sec_id)
        realized_pnl = 0.0
        trade_notional = float(qty * fill_price)

        if not pos:
            self.positions[sec_id] = {
                'side': action,
                'qty': qty,
                'avg_price': fill_price,
                'symbol': symbol or f"TOK_{sec_id}",
                'trigger_source': trigger_source or "BOT_MATH"
            }
            self.total_turnover_deployed += trade_notional
        elif pos['side'] == action:
            total_qty = pos['qty'] + qty
            pos['avg_price'] = ((pos['avg_price'] * pos['qty']) + (fill_price * qty)) / total_qty
            pos['qty'] = total_qty
            self.total_turnover_deployed += trade_notional
        else:
            close_qty = min(pos['qty'], qty)
            if pos['side'] == 'BUY':
                realized_pnl = (fill_price - pos['avg_price']) * close_qty
            else:
                realized_pnl = (pos['avg_price'] - fill_price) * close_qty

            self.daily_pnl += realized_pnl
            self.current_balance += realized_pnl

            if pos['qty'] == close_qty:
                del self.positions[sec_id]
            else:
                pos['qty'] -= close_qty

        current_notional = self.get_active_notional()
        if current_notional > self.peak_capital_deployed:
            self.peak_capital_deployed = current_notional

        return realized_pnl
