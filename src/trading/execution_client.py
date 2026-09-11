import time
from portfolio import PortfolioManager
from audit_logger import AuditLogger

class ExecutionEngine:
    def __init__(self, portfolio: PortfolioManager, logger: AuditLogger):
        self.portfolio = portfolio
        self.logger = logger
        self.simulated_latency = 0.050 

    def execute_paper_trade(self, tick: dict, action: str, qty: int):
        sec_id = tick.get('security_id')
        tick_timestamp = tick.get('timestamp')
        estimated_price = tick.get('ask') if action == 'BUY' else tick.get('bid')
        margin_required = estimated_price * qty

        if not self.portfolio.can_take_trade(sec_id, action, margin_required):
            return

        time.sleep(self.simulated_latency)

        best_vol = tick.get('best_ask_vol') if action == 'BUY' else tick.get('best_bid_vol')
        slippage = 0.05 if qty > best_vol else 0.0
        fill_price = estimated_price + slippage if action == 'BUY' else estimated_price - slippage

        # Update portfolio & calculate realized PnL
        booked_pnl = self.portfolio.update_position(sec_id, action, qty, fill_price)
        total_balance = self.portfolio.current_balance

        self.logger.log_trade(
            tick_timestamp, sec_id, action, fill_price, qty, 
            self.simulated_latency * 1000, slippage, booked_pnl, total_balance
        )

        print(f"[{tick_timestamp}] {action} {qty}x #{sec_id} @ {fill_price:.2f} | "
              f"Booked PnL: ₹{booked_pnl:.2f} | Total Balance: ₹{total_balance:.2f}")

        
# import asyncio
# from enum import Enum
# from logzero import logger
# from typing import Any

# class OrderType(Enum):
#     MARKET = "MARKET"
#     LIMIT = "LIMIT"
#     STOPLOSS = "STOPLOSS"

# class AngelExecutionClient:
#     """Live execution client connecting directly to Angel One's SmartAPI gateway."""
    
#     def __init__(self, authenticator: Any, client_id: str) -> None:
#         self.authenticator = authenticator
#         self.client_id = client_id
        
#         # Access the authenticated SmartConnect instance from your auth module
#         self.smart_connect = self.authenticator.smart_connect
        
#         # SmartAPI requires the text symbol to place an order, not just the token.
#         # Add your strategy targets here.
#         self.token_to_symbol_map = {
#             "2885": "RELIANCE-EQ",
#             "3045": "SBIN-EQ"
#         }

#     async def place_order(self, signal: Any) -> str | None:
#         """Translates strategy signals into live market orders without blocking the async loop."""
#         try:
#             # Unpack signal constraints
#             transaction_type = "BUY" if signal.signal_type.name == "BUY" else "SELL"
#             symbol_token = str(signal.security_id)
#             trading_symbol = self.token_to_symbol_map.get(symbol_token, f"{symbol_token}-EQ")
            
#             # Format order payload per Angel One documentation
#             orderparams = {
#                 "variety": "NORMAL",
#                 "tradingsymbol": trading_symbol,
#                 "symboltoken": symbol_token,
#                 "transactiontype": transaction_type,
#                 "exchange": "NSE",
#                 "ordertype": signal.order_type.value,
#                 "producttype": "INTRADAY",
#                 "duration": "DAY",
#                 "quantity": str(signal.quantity),
#                 "price": "0",  # 0 for MARKET orders
#             }
            
#             logger.info(f"Routing live {transaction_type} order for {trading_symbol} to Angel One gateway...")
            
#             # SmartConnect is synchronous. Run it in a background thread to prevent loop blocking.
#             order_id = await asyncio.to_thread(self.smart_connect.placeOrder, orderparams)
            
#             logger.info(f"Order executed successfully! Exchange ID: {order_id}")
#             return order_id
            
#         except Exception as e:
#             logger.error(f"Live order placement failed: {e}")
#             return None

#     async def flatten_all_positions(self) -> None:
#         """Safety Timer execution: Pulls active intraday positions and squares them off."""
#         try:
#             logger.info("Fetching open positions from SmartAPI for square-off...")
#             positions = await asyncio.to_thread(self.smart_connect.position)
            
#             if not positions or not positions.get("data"):
#                 logger.info("No open positions found.")
#                 return
                
#             open_positions = [p for p in positions["data"] if int(p.get("netqty", 0)) != 0]
            
#             for pos in open_positions:
#                 qty = abs(int(pos["netqty"]))
#                 transaction_type = "SELL" if int(pos["netqty"]) > 0 else "BUY"
                
#                 orderparams = {
#                     "variety": "NORMAL",
#                     "tradingsymbol": pos["tradingsymbol"],
#                     "symboltoken": pos["symboltoken"],
#                     "transactiontype": transaction_type,
#                     "exchange": pos["exchange"],
#                     "ordertype": "MARKET",
#                     "producttype": pos["producttype"],
#                     "duration": "DAY",
#                     "quantity": str(qty)
#                 }
                
#                 await asyncio.to_thread(self.smart_connect.placeOrder, orderparams)
#                 logger.info(f"Squared off open position: {pos['tradingsymbol']} ({transaction_type} {qty})")
                
#         except Exception as e:
#             logger.error(f"Failed to flatten positions: {e}")

#     async def close(self) -> None:
#         """Graceful shutdown hook."""
#         pass
