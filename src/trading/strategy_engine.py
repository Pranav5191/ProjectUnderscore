"""Strategy engine with risk management for live trading."""
import sys
import os
import json
import redis
import uuid
from dotenv import load_dotenv
from datetime import datetime, time

# Indicators
from src.trading.indicators.spread import BidAskSpread
from src.trading.indicators.obi import OrderBookImbalance
from src.trading.indicators.cvd import CumulativeVolumeDelta
from src.trading.indicators.wmp import WeightedMidPrice
from src.trading.indicators.atr import TickATR

# Sandbox Components
from src.trading.portfolio import PortfolioManager
from src.trading.audit_logger import AuditLogger
from src.trading.execution_client import ExecutionEngine
from src.trading.execution_client import ExecutionEngine
from src.scripts.aws_sync import AWSSync

#risk components
from src.trading.risk_manager import RiskManager

#Log components
from src.trading.audit_logger import setup_signal_logger
from src.scripts.telegram_reporter import TelegramReporter
from src.scripts.aws_sync import AWSSync


# # Indicators
# from indicators.spread import BidAskSpread
# from indicators.obi import OrderBookImbalance
# from indicators.cvd import CumulativeVolumeDelta
# from indicators.wmp import WeightedMidPrice
# from indicators.atr import TickATR

# # Sandbox Components
# from portfolio import PortfolioManager
# from audit_logger import AuditLogger
# from execution_client import ExecutionEngine
# from scripts.aws_sync import AWSSync

# #risk components
# from risk_manager import RiskManager

# #Log components
# from audit_logger import setup_signal_logger

current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, "../../.env")
load_dotenv(dotenv_path=env_path)

def main():
    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True
    )
    
    # Initialize Math Engine
    spread = BidAskSpread()
    obi = OrderBookImbalance()
    cvd = CumulativeVolumeDelta()
    wmp = WeightedMidPrice()
    atr = TickATR(period=14, tick_chunk=50) # Added ATR
    indicators = [spread, obi, cvd, wmp, atr] # Added to list
    
    # Initialize Sandbox (Starting with ₹1,00,000 | 10% max allocation | 2% max daily loss)
    portfolio = PortfolioManager(starting_balance=100000.0, max_allocation_pct=0.10, max_loss_pct=0.02)
    logger = AuditLogger()
    engine = ExecutionEngine(portfolio, logger)
    
    pubsub = r.pubsub()
    pubsub.subscribe("live_ticks")
    risk_manager = RiskManager(base_risk_pct = 0.10)
    # Initialize the Signal Logger
    signal_logger = setup_signal_logger()
    print("Signal Logger initialized. Silently logging to /logs directory...")
    print("Execution System Live. Listening for ticks and searching for setups...")
    print("Execution System Live. Listening for ticks and searching for setups...")
    
    # NEW ARCHITECTURE: Non-blocking event loop
    while True:
        # =================================================================
        # 1. THE 3:14 PM HARD KILL-SWITCH (EVALUATED EVERY SECOND)
        # =================================================================
        current_time = datetime.now().time()
        cutoff_time = time(15, 14, 0) # 15:14:00 (3:14 PM)
        
        if current_time >= cutoff_time:
            open_positions = list(portfolio.positions.items())
            
            if open_positions:
                print("\n[SYSTEM ALERT] 3:14 PM Cutoff Reached. Initiating forced liquidation.")
                for open_sec_id, pos_data in open_positions:
                    pos_qty = pos_data['qty']
                    side = pos_data['side']
                    exit_action = 'SELL' if side == 'BUY' else 'BUY'
                    trace_id = f"{open_sec_id}-SQUAREOFF-{uuid.uuid4().hex[:6]}"
                    print(f"[LIQUIDATION] Force closing {side} position on #{open_sec_id}. Trace: {trace_id}")
                    # Fire a dummy tick to force the execution client to process it
                    dummy_tick = {'security_id': open_sec_id, 'ltp': pos_data['avg_price'], 'timestamp': datetime.now().isoformat()}
                    engine.execute_paper_trade(dummy_tick, action=exit_action, qty=pos_qty, trace_id=trace_id)
            
            portfolio.save_state()
            print(f"[STATE SAVED] Final Portfolio Balance: ₹{portfolio.current_balance:.2f} written to disk.")
            
            # THE CLOUD & TELEGRAM HANDOFF
            print("\n[SYSTEM] Commencing Cloud Handoff to AWS S3...")
            from src.scripts.aws_sync import AWSSync
            cloud_sync = AWSSync()
            cloud_sync.upload_daily_logs()

            print("\n[SYSTEM] Generating End-of-Day Telegram Report and DB Backup...")
            from src.scripts.telegram_reporter import TelegramReporter
            reporter = TelegramReporter()
            reporter.send_report()
            reporter.send_file(reporter.csv_path)
            log_path = os.path.join(reporter.base_dir, "logs", f"signals_{reporter.today_str}.log")
            reporter.send_file(log_path)
            
            db_dump = reporter.backup_postgres()
            if db_dump:
                reporter.send_file(db_dump)

            print("\n[SYSTEM TERMINATED] All intraday positions flat and data secured. Shutting down.")
            break  # Break the while loop
        
        # =================================================================
        # 2. NON-BLOCKING REDIS FETCH (1 Second Timeout)
        # =================================================================
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        
        if message and message['type'] == 'message':
            tick = json.loads(message['data'])
            sec_id = tick.get('security_id')
            tick_ltp = tick.get('ltp', 0.0)

            # =================================================================
            # 3. UPDATE INDICATOR MATH
            # =================================================================
            for ind in indicators:
                ind.update(tick)
            
            obi_score = obi.get_score()
            cvd_score = cvd.get_score()
            
            # =================================================================
            # 4. POSITION MANAGEMENT (DYNAMIC ATR SL/TP BANDS)
            # =================================================================
            current_pos = portfolio.positions.get(sec_id)
            if current_pos:
                entry_price = current_pos['avg_price']
                pos_qty = current_pos['qty']
                side = current_pos['side']
                
                atr_val = atr.get_score() if atr.get_score() > 0 else 1.0
                
                if side == 'BUY':
                    tp_price = entry_price + (atr_val * 3.0)
                    sl_price = entry_price - (atr_val * 1.5)
                    
                    if tick_ltp >= tp_price or tick_ltp <= sl_price:
                        trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                        reason = "TAKE PROFIT" if tick_ltp >= tp_price else "STOP LOSS"
                        print(f"\n[{reason}] Long on #{sec_id}. Exiting. Trace: {trace_id}")
                        engine.execute_paper_trade(tick, action='SELL', qty=pos_qty, trace_id=trace_id)
                        continue
                        
                elif side == 'SELL':
                    tp_price = entry_price - (atr_val * 3.0)
                    sl_price = entry_price + (atr_val * 1.5)
                    
                    if tick_ltp <= tp_price or tick_ltp >= sl_price:
                        trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                        reason = "TAKE PROFIT" if tick_ltp <= tp_price else "STOP LOSS"
                        print(f"\n[{reason}] Short on #{sec_id}. Exiting. Trace: {trace_id}")
                        engine.execute_paper_trade(tick, action='BUY', qty=pos_qty, trace_id=trace_id)
                        continue

            # =================================================================
            # 5. ENTRY STRATEGY LOGIC 
            # =================================================================
            if obi_score < -0.4 and cvd_score > 1000:
                confidence = risk_manager.calculate_confidence(obi_score, cvd_score, spread.get_score())
                margin_used = sum(pos['qty'] * pos['avg_price'] for pos in portfolio.positions.values())
                free_cash = portfolio.current_balance - margin_used
                qty = risk_manager.calculate_position_size(confidence, free_cash, tick_ltp, tick.get('best_bid_vol', 0))
                
                if qty > 0:
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    status = engine.execute_paper_trade(tick, action='SELL', qty=qty, trace_id=trace_id)
                    signal_logger.info(f"Trace: {trace_id} | Sec: {sec_id} | Action: SELL | Conf: {confidence:.2f} | Qty: {qty} | Status: {status}")
                
            elif obi_score > 0.4 and cvd_score < -1000:
                confidence = risk_manager.calculate_confidence(obi_score, cvd_score, spread.get_score())
                margin_used = sum(pos['qty'] * pos['avg_price'] for pos in portfolio.positions.values())
                free_cash = portfolio.current_balance - margin_used
                qty = risk_manager.calculate_position_size(confidence, free_cash, tick_ltp, tick.get('best_ask_vol', 0))
                
                if qty > 0:
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    status = engine.execute_paper_trade(tick, action='BUY', qty=qty, trace_id=trace_id)
                    signal_logger.info(f"Trace: {trace_id} | Sec: {sec_id} | Action: BUY | Conf: {confidence:.2f} | Qty: {qty} | Status: {status}")
if __name__ == "__main__":
    main()

# import asyncio
# import logging
# from abc import ABC, abstractmethod
# from collections import defaultdict
# from dataclasses import dataclass, field
# from datetime import datetime, timezone
# from decimal import Decimal
# from enum import Enum
# from typing import Any

# from src.trading.execution_client import (
#     AngelExecutionClient,
#     OrderType,
# )

# logger = logging.getLogger(__name__)


# class SignalType(Enum):
#     BUY = "BUY"
#     SELL = "SELL"
#     CLOSE = "CLOSE"
#     HOLD = "HOLD"


# class OrderValidity(str, Enum):
#     DAY = "DAY"
#     IOC = "IOC"


# @dataclass(slots=True)
# class OrderResponse:
#     order_id: str
#     order_status: str = "SUBMITTED"
#     error_message: str | None = None


# @dataclass(frozen=True, slots=True)
# class Signal:
#     """Trading signal generated by a strategy."""
#     security_id: int
#     signal_type: SignalType
#     quantity: int
#     price: Decimal | None = None
#     trigger_price: Decimal | None = None
#     order_type: OrderType = OrderType.MARKET
#     validity: OrderValidity = OrderValidity.DAY
#     metadata: dict[str, Any] = field(default_factory=dict)
#     timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# @dataclass(frozen=True, slots=True)
# class Position:
#     """Current position state."""
#     security_id: int
#     quantity: int
#     avg_price: Decimal
#     unrealized_pnl: Decimal = Decimal("0")
#     realized_pnl: Decimal = Decimal("0")
#     last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# @dataclass(slots=True)
# class RiskLimits:
#     """Risk management configuration."""
#     max_open_positions: int = 10
#     max_position_size: int = 1000
#     max_order_size: int = 500
#     max_daily_loss: Decimal = Decimal("50000")
#     max_drawdown_pct: float = 0.05
#     hard_stop_loss_pct: float = 0.02
#     max_open_orders: int = 20


# @dataclass(slots=True)
# class RiskState:
#     """Current risk state tracking."""
#     open_positions: dict[int, Position] = field(default_factory=dict)
#     open_orders: dict[str, OrderResponse] = field(default_factory=dict)
#     daily_pnl: Decimal = Decimal("0")
#     peak_equity: Decimal = Decimal("0")
#     current_equity: Decimal = Decimal("0")
#     blocked: bool = False
#     block_reason: str = ""


# class BaseStrategy(ABC):
#     """Abstract base class for trading strategies."""

#     def __init__(self, name: str, security_ids: list[int]) -> None:
#         self.name = name
#         self.security_ids = security_ids
#         self._running = False

#     @abstractmethod
#     async def on_tick(self, tick: dict[str, Any]) -> list[Signal]:
#         """Process incoming tick and generate signals.

#         Args:
#             tick: Raw tick data from WebSocket.

#         Returns:
#             List of Signal objects (empty if no action).
#         """
#         pass

#     @abstractmethod
#     async def on_start(self) -> None:
#         """Called when strategy starts."""
#         pass

#     @abstractmethod
#     async def on_stop(self) -> None:
#         """Called when strategy stops."""
#         pass

#     async def start(self) -> None:
#         self._running = True
#         await self.on_start()

#     async def stop(self) -> None:
#         self._running = False
#         await self.on_stop()


# class RiskManager:
#     """Pre-trade risk validation layer."""

#     def __init__(self, limits: RiskLimits) -> None:
#         self._limits = limits
#         self._state = RiskState()
#         self._lock = asyncio.Lock()

#     async def validate_order(
#         self,
#         signal: Signal,
#         current_price: Decimal | None = None,
#     ) -> tuple[bool, str]:
#         """Validate order against risk limits."""
#         async with self._lock:
#             if self._state.blocked:
#                 return False, f"Trading blocked: {self._state.block_reason}"

#             # Max open positions
#             if len(self._state.open_positions) >= self._limits.max_open_positions:
#                 if signal.security_id not in self._state.open_positions:
#                     return False, f"Max open positions ({self._limits.max_open_positions}) reached"

#             # Max position size
#             pos = self._state.open_positions.get(signal.security_id)
#             current_qty = pos.quantity if pos else 0

#             if signal.signal_type == SignalType.BUY:
#                 new_qty = current_qty + signal.quantity
#             elif signal.signal_type == SignalType.SELL:
#                 new_qty = current_qty - signal.quantity
#             else:
#                 new_qty = abs(current_qty)

#             if abs(new_qty) > self._limits.max_position_size:
#                 return False, f"Position size {abs(new_qty)} exceeds limit {self._limits.max_position_size}"

#             # Max order size
#             if signal.quantity > self._limits.max_order_size:
#                 return False, f"Order size {signal.quantity} exceeds limit {self._limits.max_order_size}"

#             # Max open orders
#             if len(self._state.open_orders) >= self._limits.max_open_orders:
#                 return False, f"Max open orders ({self._limits.max_open_orders}) reached"

#             # Daily loss limit
#             if self._state.daily_pnl <= -self._limits.max_daily_loss:
#                 self._state.blocked = True
#                 self._state.block_reason = f"Daily loss limit hit: {self._state.daily_pnl}"
#                 return False, self._state.block_reason

#             # Drawdown limit
#             if self._state.peak_equity > 0:
#                 drawdown = (self._state.peak_equity - self._state.current_equity) / self._state.peak_equity
#                 if drawdown >= self._limits.max_drawdown_pct:
#                     self._state.blocked = True
#                     self._state.block_reason = f"Max drawdown {drawdown:.2%} exceeded"
#                     return False, self._state.block_reason

#             return True, "OK"

#     async def apply_stop_loss(self, signal: Signal, entry_price: Decimal) -> Signal:
#         """Apply hard stop loss to signal if needed."""
#         if signal.signal_type in (SignalType.BUY, SignalType.SELL):
#             stop_pct = self._limits.hard_stop_loss_pct
#             if signal.signal_type == SignalType.BUY:
#                 trigger = entry_price * (1 - Decimal(str(stop_pct)))
#             else:
#                 trigger = entry_price * (1 + Decimal(str(stop_pct)))

#             if signal.order_type == OrderType.MARKET:
#                 stop_order_type = getattr(OrderType, "STOP_LOSS_MARKET", getattr(OrderType, "STOPLOSS", OrderType.MARKET))
#                 return Signal(
#                     security_id=signal.security_id,
#                     signal_type=signal.signal_type,
#                     quantity=signal.quantity,
#                     price=signal.price,
#                     trigger_price=trigger,
#                     order_type=stop_order_type,
#                     validity=signal.validity,
#                     metadata={**signal.metadata, "stop_loss": "auto"},
#                     timestamp=signal.timestamp,
#                 )
#         return signal

#     async def update_position(self, order: OrderResponse, fill_price: Decimal, fill_qty: int) -> None:
#         """Update position state after fill."""
#         async with self._lock:
#             if order.order_id in self._state.open_orders:
#                 del self._state.open_orders[order.order_id]

#     async def update_equity(self, equity: Decimal) -> None:
#         """Update equity for drawdown tracking."""
#         async with self._lock:
#             self._state.current_equity = equity
#             if equity > self._state.peak_equity:
#                 self._state.peak_equity = equity

#     async def update_daily_pnl(self, pnl: Decimal) -> None:
#         """Update daily PnL."""
#         async with self._lock:
#             self._state.daily_pnl += pnl

#     def get_state(self) -> RiskState:
#         """Get current risk state snapshot."""
#         return self._state


# class StrategyEngine:
#     """Orchestrates strategies, risk management, and order execution."""

#     def __init__(
#         self,
#         execution_client: AngelExecutionClient,
#         risk_limits: RiskLimits | None = None,
#         tick_buffer_size: int = 1000,
#     ) -> None:
#         self._execution = execution_client
#         self._risk = RiskManager(risk_limits or RiskLimits())
#         self._strategies: dict[str, BaseStrategy] = {}
#         self._ingester: Any | None = None
#         self._tick_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=tick_buffer_size)
#         self._running = False
#         self._processor_task: asyncio.Task | None = None
#         self._order_tracker: dict[str, asyncio.Task] = {}

#     def register_strategy(self, strategy: BaseStrategy) -> None:
#         """Register a strategy with the engine."""
#         if strategy.name in self._strategies:
#             raise ValueError(f"Strategy '{strategy.name}' already registered")
#         self._strategies[strategy.name] = strategy
#         logger.info("Strategy registered", extra={"strategy_name": strategy.name, "securities": strategy.security_ids})

#     def set_ingester(self, ingester: Any) -> None:
#         """Link WebSocket ingester for tick consumption."""
#         self._ingester = ingester

#     async def _process_ticks(self) -> None:
#         """Main tick processing loop."""
#         while self._running:
#             try:
#                 tick = await asyncio.wait_for(self._tick_queue.get(), timeout=1.0)
#                 await self._dispatch_tick(tick)
#             except asyncio.TimeoutError:
#                 continue
#             except Exception as exc:
#                 logger.error("Tick processing error", extra={"error": str(exc), "error_type": type(exc).__name__})

#     async def _dispatch_tick(self, tick: dict[str, Any]) -> None:
#         """Dispatch tick to relevant strategies."""
#         # Cross-broker symbol resolution (handles both Dhan 'security_id' and Angel 'token')
#         raw_id = tick.get("security_id") or tick.get("token")
#         if raw_id is None:
#             return

#         security_id = int(raw_id)

#         for strategy in self._strategies.values():
#             if security_id in strategy.security_ids:
#                 try:
#                     signals = await strategy.on_tick(tick)
#                     for signal in signals:
#                         await self._handle_signal(signal)
#                 except Exception as exc:
#                     logger.error(
#                         "Strategy error",
#                         extra={"strategy": strategy.name, "security_id": security_id, "error": str(exc)},
#                         exc_info=True
#                     )

#     async def _handle_signal(self, signal: Signal) -> None:
#         """Process signal through risk and execute."""
#         current_price = signal.price or Decimal(str(signal.trigger_price or 0))

#         # Validate against risk limits
#         allowed, reason = await self._risk.validate_order(signal, current_price)
#         if not allowed:
#             logger.warning("Signal rejected by risk", extra={"signal": str(signal), "reason": reason})
#             return

#         # Apply hard stop loss
#         if signal.order_type == OrderType.MARKET and current_price > 0:
#             signal = await self._risk.apply_stop_loss(signal, current_price)

#         # Execute order directly via AngelExecutionClient
#         try:
#             logger.info("Sending order", extra={"signal": str(signal)})
#             order_res = await self._execution.place_order(signal)
#             if not order_res:
#                 return

#             order_id = getattr(order_res, "order_id", str(order_res))
#             response = OrderResponse(order_id=order_id)

#             # Track open order in risk state
#             async with self._risk._lock:
#                 self._risk._state.open_orders[order_id] = response

#             # Track order lifecycle if execution client supports get_order_status
#             if hasattr(self._execution, "get_order_status"):
#                 task = asyncio.create_task(self._track_order(order_id, signal))
#                 self._order_tracker[order_id] = task

#         except Exception as exc:
#             logger.error("Order execution failed", extra={"signal": str(signal), "error": str(exc)})

#     async def _track_order(self, order_id: str, signal: Signal) -> None:
#         """Monitor order until completion."""
#         try:
#             while True:
#                 await asyncio.sleep(2)
#                 status = await self._execution.get_order_status(order_id)

#                 if status.order_status in ("COMPLETE", "CANCELLED", "REJECTED"):
#                     async with self._risk._lock:
#                         self._risk._state.open_orders.pop(order_id, None)

#                     if status.order_status == "COMPLETE":
#                         logger.info("Order filled", extra={"order_id": order_id, "signal": str(signal)})
#                     elif status.order_status == "REJECTED":
#                         logger.warning("Order rejected", extra={"order_id": order_id, "error": status.error_message})
#                     break

#         except asyncio.CancelledError:
#             pass
#         except Exception as exc:
#             logger.error("Order tracking error", extra={"order_id": order_id, "error": str(exc)})
#         finally:
#             self._order_tracker.pop(order_id, None)

#     async def start(self) -> None:
#         """Start the strategy engine."""
#         if self._running:
#             return

#         self._running = True

#         for strategy in self._strategies.values():
#             await strategy.start()

#         self._processor_task = asyncio.create_task(self._process_ticks())
#         logger.info("Strategy engine started", extra={"strategies": list(self._strategies.keys())})

#     async def stop(self) -> None:
#         """Stop the strategy engine gracefully."""
#         self._running = False

#         if self._processor_task:
#             self._processor_task.cancel()
#             try:
#                 await self._processor_task
#             except asyncio.CancelledError:
#                 pass

#         for task in self._order_tracker.values():
#             task.cancel()
#         await asyncio.gather(*self._order_tracker.values(), return_exceptions=True)

#         for strategy in self._strategies.values():
#             await strategy.stop()

#         logger.info("Strategy engine stopped")

#     async def enqueue_tick(self, tick: dict[str, Any]) -> bool:
#         """Add tick to processing queue."""
#         try:
#             self._tick_queue.put_nowait(tick)
#             return True
#         except asyncio.QueueFull:
#             sec_id = tick.get("security_id") or tick.get("token")
#             logger.warning("Tick queue full, dropping tick", extra={"security_id": sec_id})
#             return False
