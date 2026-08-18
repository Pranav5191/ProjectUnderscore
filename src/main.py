"""Main entry point for the HFT pipeline: ingestion + execution + strategy."""

import asyncio
import logging
import signal
import sys
import os
from typing import Any, NoReturn

from src.auth.dhan_auth import DhanAuthenticator
from src.pipeline.db import DatabaseManager
from src.pipeline.websocket_client import DhanWebSocketIngester
from src.trading.execution_client import DhanExecutionClient, OrderType
from src.trading.strategy_engine import BaseStrategy, RiskLimits, Signal, SignalType, StrategyEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

class MovingAverageCrossover(BaseStrategy):
    """Example strategy: Simple MA crossover on tick data."""
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
        security_id = tick.get("security_id")
        ltp = tick.get("ltp")

        if security_id is None or ltp is None:
            return []

        prices = self._prices.setdefault(security_id, [])
        prices.append(float(ltp))

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
        self._authenticator = DhanAuthenticator()
        self._db_manager = DatabaseManager()
        self._ingester: DhanWebSocketIngester | None = None
        self._execution_client: DhanExecutionClient | None = None
        self._strategy_engine: StrategyEngine | None = None
        self._shutdown_event = asyncio.Event()

    async def initialize(self) -> None:
        logger.info("Initializing pipeline components")
        await self._db_manager.connect()
        
        # Safely get the client ID from environment variables
        client_id = os.getenv("DHAN_CLIENT_ID", "")
        
        self._execution_client = DhanExecutionClient(
            authenticator=self._authenticator,
            client_id=client_id,  
        )
        
        risk_limits = RiskLimits(
            max_open_positions=5, max_position_size=100, max_order_size=50, max_daily_loss=25000, hard_stop_loss_pct=0.015,
        )
        self._strategy_engine = StrategyEngine(
            execution_client=self._execution_client, risk_limits=risk_limits,
        )

        test_securities = [1333]  # Reliance
        self._strategy_engine.register_strategy(MovingAverageCrossover(test_securities))

        self._ingester = DhanWebSocketIngester(
            db_manager=self._db_manager, authenticator=self._authenticator,
        )
        
        original_handle = self._ingester._handle_message
        async def bridged_handle(message: str) -> None:
            await original_handle(message)
            try:
                import json
                data = json.loads(message)
                ticks = data if isinstance(data, list) else [data]
                for tick in ticks:
                    await self._strategy_engine.enqueue_tick(tick)
            except Exception as exc:
                pass

        self._ingester._handle_message = bridged_handle 

    async def run(self, security_ids: list[int]) -> None:
        await self._strategy_engine.start()
        await self._ingester.start_streaming(security_ids)

    async def shutdown(self) -> None:
        if self._strategy_engine: await self._strategy_engine.stop()
        if self._ingester: await self._ingester.stop()
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
        run_task = asyncio.create_task(orchestrator.run([1333]))
        shutdown_task = asyncio.create_task(orchestrator._shutdown_event.wait())
        
        done, pending = await asyncio.wait({run_task, shutdown_task}, return_when=asyncio.FIRST_COMPLETED)
        
        # If it crashes, don't swallow the error! Print it!
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
