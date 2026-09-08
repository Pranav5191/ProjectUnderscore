"""Main entry point for the HFT pipeline: ingestion + execution + strategy."""

import asyncio
import logging
import signal
import sys
import os
import threading
from typing import Any, NoReturn

# New Angel One Integrations
from src.auth.angel_auth import AngelAuthenticator
from src.pipeline.websocket_client import AngelDataPipeline
from src.pipeline.safety_timer import intraday_square_off_guard
from src.trading.execution_client import AngelExecutionClient, OrderType

# Core Architecture 
from src.pipeline.db import DatabaseManager
from src.trading.strategy_engine import BaseStrategy, RiskLimits, Signal, SignalType, StrategyEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

class MovingAverageCrossover(BaseStrategy):
    """Simple MA crossover on tick data."""
    def __init__(self, security_ids: list[int], fast_window: int = 5, slow_window: int = 20) -> None:
        super().__init__("MA_Crossover", security_ids)
        self._fast_window = fast_window
        self._slow_window = slow_window
        self._prices: dict[int, list[float]] = {}

    async def on_start(self) -> None:
        logger.info("MA Crossover strategy started")

    async def on_stop(self) -> None:
        logger.info("MA Crossover strategy stopped")

    async def on_tick(self, tick: dict[str, Any]) -> list[Signal]:
        token_str = tick.get("token")
        if token_str is None:
            return []
            
        security_id = int(token_str)

        # Parse Angel One LTP (paise to rupees conversion)
        ltp_raw = tick.get("last_traded_price")
        if ltp_raw is None:
            return []
            
        ltp = float(ltp_raw) / 100.0

        prices = self._prices.setdefault(security_id, [])
        prices.append(ltp)

        if len(prices) > self._slow_window:
            prices.pop(0)
        if len(prices) < self._slow_window:
            return []

        fast_ma = sum(prices[-self._fast_window:]) / self._fast_window
        slow_ma = sum(prices[-self._slow_window:]) / self._slow_window

        signals = []
        if fast_ma > slow_ma:
            signals.append(
                Signal(
                    security_id=security_id,
                    signal_type=SignalType.BUY,
                    quantity=1,
                    order_type=OrderType.MARKET,
                )
            )
        elif fast_ma < slow_ma:
            signals.append(
                Signal(
                    security_id=security_id,
                    signal_type=SignalType.SELL,
                    quantity=1,
                    order_type=OrderType.MARKET,
                )
            )
        return signals

class PipelineOrchestrator:
    """Orchestrates ingestion, execution, and strategy components."""
    def __init__(self) -> None:
        self._authenticator = AngelAuthenticator()
        self._db_manager = DatabaseManager()
        self._ingester: AngelDataPipeline | None = None
        self._execution_client: AngelExecutionClient | None = None
        self._strategy_engine: StrategyEngine | None = None
        self._shutdown_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()

    async def initialize(self) -> None:
        logger.info("Initializing pipeline components")
        await self._db_manager.connect()

        client_id = os.getenv("ANGEL_CLIENT_ID", "")

        # Real Live Market Execution Client
        self._execution_client = AngelExecutionClient(
            authenticator=self._authenticator,
            client_id=client_id,
        )

        risk_limits = RiskLimits(
            max_open_positions=5, max_position_size=100, max_order_size=50, max_daily_loss=25000, hard_stop_loss_pct=0.015,
        )
        self._strategy_engine = StrategyEngine(
            execution_client=self._execution_client, risk_limits=risk_limits,
        )

        test_securities = [2885]  # Reliance NSE Token mapped to Angel One
        self._strategy_engine.register_strategy(MovingAverageCrossover(test_securities))

        self._ingester = AngelDataPipeline()

        # Bridge: Patch _on_data directly on the ingester instance so it feeds the strategy engine
        original_on_data = self._ingester._on_data

        def bridged_on_data(wsapp: Any, message: dict[str, Any]) -> None:
            if original_on_data:
                original_on_data(wsapp, message)
            
            try:
                asyncio.run_coroutine_threadsafe(
                    self._strategy_engine.enqueue_tick(message),
                    self._loop
                )
            except Exception:
                pass

        self._ingester._on_data = bridged_on_data

    async def run(self, security_ids: list[int]) -> None:
        await self._strategy_engine.start()
        
        # Start Angel One WebSocket natively in a daemon thread so it doesn't block asyncio
        self._ws_thread = threading.Thread(target=self._ingester.start, daemon=True)
        self._ws_thread.start()

        # Keep the orchestrator alive until a shutdown signal is sent
        await self._shutdown_event.wait()

    async def shutdown(self) -> None:
        if self._strategy_engine: await self._strategy_engine.stop()
        if self._ingester: 
            try:
                self._ingester.sws.close_connection()
            except Exception:
                pass
        if self._execution_client: await self._execution_client.close()
        await self._db_manager.close()

    def signal_handler(self, signum: int, frame: Any) -> None:
        self._shutdown_event.set()

async def main() -> NoReturn:
    orchestrator = PipelineOrchestrator()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, orchestrator.signal_handler, sig, None)

    try:
        await orchestrator.initialize()
        
        run_task = asyncio.create_task(orchestrator.run([2885])) 
        
        # Inject the live safety guard with the REAL execution client
        safety_task = asyncio.create_task(intraday_square_off_guard(orchestrator._execution_client))

        # Wait for either the pipeline to shut down (run_task) or a fatal timer crash (safety_task)
        done, pending = await asyncio.wait(
            {run_task, safety_task}, 
            return_when=asyncio.FIRST_COMPLETED
        )

        for task in done:
            if task.exception():
                logger.error(f"Pipeline Crashed: {task.exception()}")

    except Exception as e:
        logger.error(f"Initialization error: {e}")
    finally:
        await orchestrator.shutdown()
        sys.exit(0)

if __name__ == "__main__":
    asyncio.run(main())
