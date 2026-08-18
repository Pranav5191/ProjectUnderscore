"""Dhan WebSocket client for real-time market data ingestion.

Architecture:
- Hot Path: Parse tick -> XADD to Redis Stream 'market:ticks' (sub-millisecond latency)
- Cold Path: Background consumer reads from Redis Stream -> bulk INSERT to PostgreSQL
"""

import asyncio
import json
import logging
import os
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import redis.asyncio as redis
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.auth.dhan_auth import DhanAuthenticator
from src.pipeline.db import DatabaseManager

logger = logging.getLogger(__name__)


class DhanWebSocketIngester:
    """High-performance WebSocket ingester with hot/cold path separation."""

    _WS_URL = "wss://api-feed.dhan.co"
    _REDIS_STREAM = "market:ticks"
    _CONSUMER_GROUP = "pg_consumer_group"
    _CONSUMER_NAME = "pg_writer_1"

    # Cold path thresholds
    _BATCH_SIZE_THRESHOLD = 500
    _BATCH_TIME_THRESHOLD = 1.0  # seconds

    def __init__(
        self,
        db_manager: DatabaseManager,
        authenticator: DhanAuthenticator,
        redis_url: str | None = None,
        reconnect_base_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
    ) -> None:
        self._db = db_manager
        self._auth = authenticator
        self._reconnect_base_delay = reconnect_base_delay
        self._max_reconnect_delay = max_reconnect_delay

        self._redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self._redis: redis.Redis | None = None

        self._cold_buffer: list[dict[str, Any]] = []
        self._cold_buffer_lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._consumer_task: asyncio.Task | None = None

        self._running = False
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._access_token: str | None = None

    async def _get_redis(self) -> redis.Redis:
        """Get or create Redis connection."""
        if self._redis is None:
            self._redis = redis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
                max_connections=20,
            )
            try:
                await self._redis.xgroup_create(
                    self._REDIS_STREAM,
                    self._CONSUMER_GROUP,
                    id="0",
                    mkstream=True,
                )
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
        return self._redis

    # ==================== HOT PATH ====================

    async def _handle_message(self, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse WebSocket message", extra={"error": str(exc)})
            return

        ticks = data if isinstance(data, list) else [data]
        r = await self._get_redis()

        for tick in ticks:
            try:
                parsed = self._parse_tick(tick)
                if not parsed:
                    continue

                stream_entry = {
                    "timestamp": parsed["timestamp"].isoformat(),
                    "security_id": str(parsed["security_id"]),
                    "ltp": str(parsed["ltp"]),
                    "volume": str(parsed["volume"]),
                    "bid": str(parsed["bid"]) if parsed["bid"] is not None else "",
                    "ask": str(parsed["ask"]) if parsed["ask"] is not None else "",
                    "open_interest": str(parsed["open_interest"]) if parsed["open_interest"] is not None else "",
                }

                await r.xadd(self._REDIS_STREAM, stream_entry, maxlen=100000, approximate=True)

            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Malformed tick data", extra={"error": str(exc), "tick": tick})
            except redis.RedisError as exc:
                logger.error("Redis publish failed (hot path)", extra={"error": str(exc)})

    # ==================== COLD PATH ====================

    async def _consumer_loop(self) -> None:
        r = await self._get_redis()

        while self._running:
            try:
                entries = await r.xreadgroup(
                    groupname=self._CONSUMER_GROUP,
                    consumername=self._CONSUMER_NAME,
                    streams={self._REDIS_STREAM: ">"},
                    count=self._BATCH_SIZE_THRESHOLD,
                    block=int(self._BATCH_TIME_THRESHOLD * 1000),
                )

                if not entries:
                    async with self._cold_buffer_lock:
                        if self._cold_buffer:
                            await self._flush_cold_buffer()
                    continue

                batch = []
                entry_ids = []

                for stream_name, messages in entries:
                    for entry_id, fields in messages:
                        entry_ids.append(entry_id)
                        batch.append(self._deserialize_tick(fields))

                if batch:
                    async with self._cold_buffer_lock:
                        self._cold_buffer.extend(batch)
                        if len(self._cold_buffer) >= self._BATCH_SIZE_THRESHOLD:
                            await self._flush_cold_buffer()

                if entry_ids:
                    await r.xack(self._REDIS_STREAM, self._CONSUMER_GROUP, *entry_ids)

            except redis.RedisError as exc:
                logger.error("Redis consumer error", extra={"error": str(exc)})
                await asyncio.sleep(1)
            except Exception as exc:
                logger.error("Cold path error", extra={"error": str(exc), "error_type": type(exc).__name__})

    def _deserialize_tick(self, fields: dict[str, str]) -> dict[str, Any]:
        return {
            "timestamp": datetime.fromisoformat(fields["timestamp"]),
            "security_id": int(fields["security_id"]),
            "ltp": Decimal(fields["ltp"]),
            "volume": int(fields["volume"]),
            "bid": Decimal(fields["bid"]) if fields["bid"] else None,
            "ask": Decimal(fields["ask"]) if fields["ask"] else None,
            "open_interest": int(fields["open_interest"]) if fields["open_interest"] else None,
        }

    async def _flush_cold_buffer(self) -> None:
        if not self._cold_buffer:
            return

        ticks_to_insert = self._cold_buffer.copy()
        self._cold_buffer.clear()

        try:
            inserted = await self._db.insert_ticks_batch(ticks_to_insert)
            logger.info(
                "Cold path batch inserted",
                extra={"count": inserted, "buffer_remaining": len(self._cold_buffer)},
            )
        except Exception as exc:
            logger.error(
                "Cold path batch insert failed",
                extra={"error": str(exc), "count": len(ticks_to_insert)},
            )
            self._cold_buffer.extend(ticks_to_insert)

    # ==================== SHARED ====================

    def _parse_tick(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        required = ("security_id", "ltp", "volume", "timestamp")
        if not all(k in raw for k in required):
            return None

        ts = raw["timestamp"]
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = datetime.now(timezone.utc)

        return {
            "timestamp": dt,
            "security_id": int(raw["security_id"]),
            "ltp": Decimal(str(raw["ltp"])),
            "volume": int(raw["volume"]),
            "bid": Decimal(str(raw["bid"])) if raw.get("bid") is not None else None,
            "ask": Decimal(str(raw["ask"])) if raw.get("ask") is not None else None,
            "open_interest": int(raw["oi"]) if raw.get("oi") is not None else None,
        }

    def _build_subscription(self, security_ids: list[int]) -> dict[str, Any]:
        return {
            "RequestCode": 15,
            "InstrumentCount": len(security_ids),
            "InstrumentList": [
                {"ExchangeSegment": "NSE_EQ", "SecurityId": str(sid)} for sid in security_ids
            ],
        }

    async def _connect_websocket(self) -> None:
        """Establish WebSocket connection with V2 Query Parameter authentication."""
        if self._access_token is None:
            self._access_token = await self._auth.get_session()
            logger.info("Obtained new access token")

        # CRITICAL FIX: Strip all hidden newlines and URL-encode the token!
        clean_token = str(self._access_token).strip().replace('\n', '').replace('\r', '')
        client_id = getattr(self._auth, '_client_id', "")
        clean_client_id = str(client_id).strip().replace('\n', '').replace('\r', '')
        
        safe_token = urllib.parse.quote(clean_token)
        
        # Dhan V2 requires credentials in the URL
        auth_url = f"{self._WS_URL}?version=2&token={safe_token}&clientId={clean_client_id}&authType=2"

        self._ws = await websockets.connect(
            auth_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        logger.info("WebSocket connected successfully!")

    async def _subscribe(self, security_ids: list[int]) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")

        payload = self._build_subscription(security_ids)
        await self._ws.send(json.dumps(payload))
        logger.info("Subscribed to securities", extra={"count": len(security_ids), "ids": security_ids})

    async def _receive_loop(self) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")

        async for message in self._ws:
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            await self._handle_message(message)

    async def start_streaming(self, security_ids: list[int]) -> None:
        if self._running:
            raise RuntimeError("Ingester already running")
        if not security_ids:
            raise ValueError("At least one security_id required")

        if self._db._pool is None:
            await self._db.connect()

        await self._get_redis()

        self._running = True
        reconnect_delay = self._reconnect_base_delay
        attempt = 0

        while self._running:
            try:
                attempt += 1
                logger.info("Starting WebSocket connection", extra={"attempt": attempt})

                await self._connect_websocket()
                await self._subscribe(security_ids)

                self._consumer_task = asyncio.create_task(self._consumer_loop())
                self._flush_task = asyncio.create_task(self._periodic_flush())

                reconnect_delay = self._reconnect_base_delay
                await self._receive_loop()

            except ConnectionClosed as exc:
                logger.warning(f"WebSocket closed | Code: {exc.code} | Reason: {exc.reason}")
            except WebSocketException as exc:
                logger.error("WebSocket error", extra={"error": str(exc)})
            except asyncio.CancelledError:
                logger.info("Streaming cancelled")
                break
            except Exception as exc:
                logger.error("Unexpected error in streaming loop", extra={"error": repr(exc)})
            finally:
                await self._cleanup_connection()

            if self._running:
                delay = min(reconnect_delay, self._max_reconnect_delay)
                logger.info("Reconnecting", extra={"delay_seconds": delay})
                await asyncio.sleep(delay)
                reconnect_delay *= 2

    async def _periodic_flush(self) -> None:
        while self._running:
            await asyncio.sleep(self._BATCH_TIME_THRESHOLD)
            async with self._cold_buffer_lock:
                if self._cold_buffer:
                    await self._flush_cold_buffer()

    async def _cleanup_connection(self) -> None:
        for task in (self._flush_task, self._consumer_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        async with self._cold_buffer_lock:
            if self._cold_buffer:
                await self._flush_cold_buffer()

        if self._ws:
            try:
                await self._ws.close()
            except Exception as exc:
                logger.debug("Error closing WebSocket", extra={"error": str(exc)})
            self._ws = None

    async def stop(self) -> None:
        logger.info("Stopping WebSocket ingester")
        self._running = False
        await self._cleanup_connection()

        if self._redis:
            await self._redis.aclose()
            self._redis = None

        await self._db.close()
        logger.info("WebSocket ingester stopped")
