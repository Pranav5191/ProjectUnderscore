"""Asynchronous database manager for high-frequency market data ingestion."""
import os
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).resolve().parent.parent.parent / '.env')
import asyncio
import logging
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Async PostgreSQL connection pool manager for bulk tick ingestion.

    Uses asyncpg for maximum throughput with prepared statements
    and connection pooling optimized for write-heavy workloads.
    """

    def __init__(self) -> None:
        """Initialize configuration from environment variables."""
        self._user = os.getenv("DB_USER")
        self._password = os.getenv("DB_PASS")
        self._host = os.getenv("DB_HOST")
        self._port = os.getenv("DB_PORT")
        self._database = os.getenv("DB_NAME")

        missing = [
            name
            for name, value in [
                ("DB_USER", self._user),
                ("DB_PASS", self._password),
                ("DB_HOST", self._host),
                ("DB_PORT", self._port),
                ("DB_NAME", self._database),
            ]
            if not value
        ]

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        self._pool: asyncpg.Pool | None = None
        self._insert_stmt: str = """
            INSERT INTO market_ticks (timestamp, security_id, ltp, volume, bid, ask, open_interest)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """

    async def connect(
        self,
        min_size: int = 10,
        max_size: int = 20,
        command_timeout: float = 60.0,
    ) -> None:
        """Initialize asyncpg connection pool.

        Args:
            min_size: Minimum pool connections (default: 10).
            max_size: Maximum pool connections (default: 20).
            command_timeout: Query timeout in seconds (default: 60.0).

        Raises:
            RuntimeError: If pool creation fails.
            asyncpg.PostgresError: On connection/authentication failure.
        """
        if self._pool is not None:
            logger.warning("Connection pool already initialized")
            return

        try:
            self._pool = await asyncpg.create_pool(
                user=self._user,
                password=self._password,
                host=self._host,
                port=int(self._port),
                database=self._database,
                min_size=min_size,
                max_size=max_size,
                command_timeout=command_timeout,
                statement_cache_size=0,  # Disable for bulk inserts
            )
            logger.info(
                "Database connection pool initialized",
                extra={"min_size": min_size, "max_size": max_size, "host": self._host},
            )
        except asyncpg.PostgresError as exc:
            logger.error("Failed to create connection pool", extra={"error": str(exc)})
            raise RuntimeError("Database connection failed") from exc

    async def close(self) -> None:
        """Gracefully close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("Database connection pool closed")

    async def insert_ticks_batch(self, ticks: list[dict[str, Any]]) -> int:
        """Bulk insert market ticks using executemany for maximum throughput.

        Args:
            ticks: List of tick dictionaries with keys:
                timestamp (datetime), security_id (int), ltp (Decimal/float),
                volume (int), bid (Decimal/float/None), ask (Decimal/float/None),
                open_interest (int/None)

        Returns:
            Number of rows inserted.

        Raises:
            RuntimeError: If pool not initialized.
            asyncpg.PostgresError: On insertion failure.
            ValueError: If tick data is malformed.
        """
        if self._pool is None:
            raise RuntimeError("Database pool not initialized. Call connect() first.")

        if not ticks:
            logger.debug("Empty tick batch received, skipping insert")
            return 0

        records = []
        for i, tick in enumerate(ticks):
            try:
                record = (
                    tick["timestamp"],
                    tick["security_id"],
                    tick["ltp"],
                    tick["volume"],
                    tick.get("bid"),
                    tick.get("ask"),
                    tick.get("open_interest"),
                )
                records.append(record)
            except KeyError as exc:
                logger.error(
                    "Malformed tick data",
                    extra={"index": i, "missing_field": str(exc), "tick": tick},
                )
                raise ValueError(f"Tick at index {i} missing required field: {exc}") from exc

        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(self._insert_stmt, records)

            logger.info("Batch insert completed", extra={"rows_inserted": len(records)})
            return len(records)

        except asyncpg.PostgresError as exc:
            logger.error(
                "Batch insert failed",
                extra={"error": str(exc), "error_type": type(exc).__name__, "batch_size": len(records)},
            )
            raise

    async def health_check(self) -> bool:
        """Verify database connectivity.

        Returns:
            True if connection successful, False otherwise.
        """
        if self._pool is None:
            return False

        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except asyncpg.PostgresError as exc:
            logger.error("Health check failed", extra={"error": str(exc)})
            return False
