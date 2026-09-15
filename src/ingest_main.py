"""
Standalone Data Ingestion Service.
Connects to Angel One WebSocket and publishes ticks directly to Redis.
"""
import asyncio
import logging
import signal
import sys
import os
import threading
import json
import redis
from typing import Any, NoReturn

from src.auth.angel_auth import AngelAuthenticator
from src.pipeline.websocket_client import AngelDataPipeline
from src.pipeline.db import DatabaseManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("Data_Ingester")

class IngestionService:
    def __init__(self) -> None:
        self._authenticator = AngelAuthenticator()
        self._db_manager = DatabaseManager()
        self._ingester: AngelDataPipeline | None = None
        self._shutdown_event = asyncio.Event()
        
        # Redis connection to broadcast live ticks
        self._redis_client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True
        )

    async def initialize(self) -> None:
        logger.info("Initializing Standalone Data Ingestion Service...")
        
        # Connect to the local database if needed for tick logging
        await self._db_manager.connect()

        # Initialize the WebSocket client
        self._ingester = AngelDataPipeline()
        original_on_data = self._ingester._on_data

        def bridged_on_data(wsapp: Any, message: dict[str, Any]) -> None:
            # 1. Maintain the original ingestion logic (e.g., DB writes)
            if original_on_data:
                original_on_data(wsapp, message)
            
            # 2. Publish to Redis so StrategyEngine can process it asynchronously
            try:
                self._redis_client.publish("live_ticks", json.dumps(message))
            except Exception as e:
                logger.debug(f"Redis publish failed: {e}")

        # Override the callback
        self._ingester._on_data = bridged_on_data

    async def run(self) -> None:
        # Start Angel One WebSocket natively in a daemon thread so it doesn't block asyncio
        self._ws_thread = threading.Thread(target=self._ingester.start, daemon=True)
        self._ws_thread.start()
        
        logger.info("WebSocket connected. Pumping ticks to Redis channel: 'live_ticks'...")
        
        # Keep the service alive until manually interrupted
        await self._shutdown_event.wait()

    async def shutdown(self) -> None:
        logger.info("Shutting down Ingestion Service...")
        if self._ingester:
            try:
                self._ingester.sws.close_connection()
            except Exception:
                pass
        await self._db_manager.close()
        self._redis_client.close()

    def signal_handler(self, signum: int, frame: Any) -> None:
        self._shutdown_event.set()

async def main() -> NoReturn:
    service = IngestionService()
    loop = asyncio.get_running_loop()
    
    # Catch Ctrl+C and kill signals for graceful shutdown
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, service.signal_handler, sig, None)

    try:
        await service.initialize()
        await service.run()
    except Exception as e:
        logger.error(f"Ingestion crashed: {e}")
    finally:
        await service.shutdown()
        sys.exit(0)

if __name__ == "__main__":
    asyncio.run(main())
