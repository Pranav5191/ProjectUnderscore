class PortfolioManager:
    def __init__(self, starting_balance: float, max_allocation_pct: float, max_loss_pct: float):
        self.initial_balance = starting_balance
        self.current_balance = starting_balance
        self.max_allocation_pct = max_allocation_pct
        self.max_loss_pct = max_loss_pct
        self.positions = {}  # {sec_id: {'side': 'BUY'/'SELL', 'qty': int, 'avg_price': float}}
        self.daily_pnl = 0.0

    def can_take_trade(self, sec_id: int, action: str, requested_margin: float) -> bool:
        if self.daily_pnl <= -(self.initial_balance * self.max_loss_pct):
            print("[RISK LOCK] Max daily drawdown breached. Trading blocked.")
            return False
            
        # Prevent pyramiding into the same direction continuously
        current_pos = self.positions.get(sec_id)
        if current_pos and current_pos['side'] == action:
            return False

        if requested_margin > (self.current_balance * self.max_allocation_pct):
            return False
            
        return True

    def update_position(self, sec_id: int, action: str, qty: int, fill_price: float) -> float:
        """Updates portfolio state and returns booked (realized) PnL."""
        pos = self.positions.get(sec_id)
        realized_pnl = 0.0

        if not pos:
            # Open brand-new position
            self.positions[sec_id] = {'side': action, 'qty': qty, 'avg_price': fill_price}
        elif pos['side'] == action:
            # Add to position (Weighted average entry price)
            total_qty = pos['qty'] + qty
            pos['avg_price'] = ((pos['avg_price'] * pos['qty']) + (fill_price * qty)) / total_qty
            pos['qty'] = total_qty
        else:
            # Closing or reversing position: Book PnL
            close_qty = min(pos['qty'], qty)
            if pos['side'] == 'BUY':  # Closing a Long
                realized_pnl = (fill_price - pos['avg_price']) * close_qty
            else:  # Closing a Short
                realized_pnl = (pos['avg_price'] - fill_price) * close_qty

            self.daily_pnl += realized_pnl
            self.current_balance += realized_pnl

            if pos['qty'] == close_qty:
                del self.positions[sec_id]
            else:
                pos['qty'] -= close_qty

        return realized_pnl