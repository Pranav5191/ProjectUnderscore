from __future__ import annotations
import asyncio
import json
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.auth.dhan_auth import DhanAuthenticator
from src.pipeline.db import DatabaseManager

logger = logging.getLogger(__name__)


class DhanWebSocketIngester:
    """High-performance WebSocket ingester for Dhan live market feed.

    Features:
    - Automatic authentication and token management
    - Buffered batch insertion (time + count thresholds)
    - Exponential backoff reconnection
    - Structured logging with correlation IDs
    """

    _WS_URL = "wss://api-feed.dhan.co"
    _BATCH_SIZE_THRESHOLD = 500
    _BATCH_TIME_THRESHOLD = 1.0  # seconds

    def __init__(
        self,
        db_manager: DatabaseManager,
        authenticator: DhanAuthenticator,
        reconnect_base_delay: float = 1.0,
        max_reconnect_delay: float = 60.0,
    ) -> None:
        """Initialize the WebSocket ingester.

        Args:
            db_manager: Initialized DatabaseManager instance.
            authenticator: Initialized DhanAuthenticator instance.
            reconnect_base_delay: Base delay for exponential backoff (seconds).
            max_reconnect_delay: Maximum delay between reconnection attempts.
        """
        self._db = db_manager
        self._auth = authenticator
        self._reconnect_base_delay = reconnect_base_delay
        self._max_reconnect_delay = max_reconnect_delay

        self._buffer: list[dict[str, Any]] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._running = False
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._access_token: str | None = None

    async  def _get_auth_headers(self) -> dict[str, str]:
        """Retrieve valid access token, refreshing if necessary."""
        if self._access_token is None or isinstance(self._access_token, dict):
            self._access_token = await self._auth.get_session()
            logger.info("Obtained new access token")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    async def _flush_buffer(self) -> None:
        """Periodically flush the tick buffer to database."""
        while self._running:
            await asyncio.sleep(self._BATCH_TIME_THRESHOLD)
            async with self._buffer_lock:
                if self._buffer:
                    await self._insert_buffer()

    async def _insert_buffer(self) -> None:
        """Insert buffered ticks and clear buffer."""
        if not self._buffer:
            return

        ticks_to_insert = self._buffer.copy()
        self._buffer.clear()

        try:
            inserted = await self._db.insert_ticks_batch(ticks_to_insert)
            logger.info(
                "Batch inserted",
                extra={"count": inserted, "buffer_remaining": len(self._buffer)},
            )
        except Exception as exc:
            logger.error(
                "Batch insert failed, ticks lost",
                extra={"error": str(exc), "count": len(ticks_to_insert)},
            )
            # Re-buffer on failure to avoid data loss
            self._buffer.extend(ticks_to_insert)

    async def _handle_message(self, message: str) -> None:
        """Parse and buffer incoming WebSocket message."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse WebSocket message", extra={"error": str(exc)})
            return

        # Dhan sends array of tick objects
        ticks = data if isinstance(data, list) else [data]

        for tick in ticks:
            try:
                parsed = self._parse_tick(tick)
                if parsed:
                    async with self._buffer_lock:
                        self._buffer.append(parsed)

                        if len(self._buffer) >= self._BATCH_SIZE_THRESHOLD:
                            await self._insert_buffer()

            except (KeyError, ValueError, TypeError) as exc:
                logger.warning("Malformed tick data", extra={"error": str(exc), "tick": tick})

    def _parse_tick(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        """Parse raw Dhan tick into database schema format.

        Expected Dhan tick format:
        {
            "security_id": 1333,
            "ltp": 1234.56,
            "volume": 1000,
            "bid": 1234.50,
            "ask": 1234.60,
            "oi": 50000,
            "timestamp": 1699999999  # Unix timestamp
        }
        """
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
    def _build_subscription(self, security_ids: list[int]) -> dict:
        """Build the Dhan v2 JSON subscription payload."""
        return {
            "RequestCode": 15,
            "InstrumentCount": len(security_ids),
            "InstrumentList": [
                {"ExchangeSegment": "NSE_EQ", "SecurityId": str(sid)} for sid in security_ids
            ]
        }

    async def _subscribe(self, security_ids: list[int]) -> None:
        """Send JSON subscription request after connection."""
        import json
        
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")

        # Get the dictionary payload
        payload = self._build_subscription(security_ids)
        
        # Send as a JSON string
        await self._ws.send(json.dumps(payload))
        logger.info("Subscribed to market feed", extra={"count": len(security_ids)})
    async def _connect_websocket(self) -> None:
        """Establish WebSocket connection with authentication."""
        import urllib.parse
        
        # Trigger token refresh if needed
        await self._get_auth_headers()
        
        # 1. Safely extract the raw token string (handling camelCase and nested data)
        token = self._access_token
        if isinstance(token, dict):
            extracted = (
                token.get("accessToken") or 
                token.get("access_token") or 
                token.get("token") or 
                (token.get("data") or {}).get("accessToken") or 
                (token.get("data") or {}).get("access_token")
            )
            if extracted:
                token = extracted
            else:
                # If no known keys match, log the raw dictionary to expose hidden API errors
                logger.error(f"Unrecognized token format. Raw payload: {token}")
                token = str(token)
                
        # 2. Safely extract client_id directly from the authenticator's internal state
        client_id = getattr(self._auth, '_client_id', os.getenv("DHAN_CLIENT_ID", ""))
        
        # 3. Log a safe masked version so we know exactly what is being sent
        token_str = str(token)
        safe_token = token_str[:15] + "..." if len(token_str) > 15 else token_str
        logger.info(f"Connecting - ClientID: {client_id}, Token: {safe_token}")
        
        # 4. URL-encode parameters to prevent HTTP 400 errors from special characters
        params = {
            "version": "2",
            "token": token_str,
            "clientId": client_id,
            "authType": "2"
        }
        auth_url = f"{self._WS_URL}?{urllib.parse.urlencode(params)}"
        
        # 5. Connect
        self._ws = await websockets.connect(
            auth_url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        )
        logger.info("WebSocket connected successfully!")

    async def _subscribe(self, security_ids: list[int]) -> None:
        """Send subscription request after connection."""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")

        payload = self._build_subscription(security_ids)
        await self._ws.send(json.dumps(payload))
        logger.info("Subscribed to securities", extra={"count": len(security_ids), "ids": security_ids})

    async def _receive_loop(self) -> None:
        """Main message receive loop."""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")

        async for message in self._ws:
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            await self._handle_message(message)

    async def start_streaming(self, security_ids: list[int]) -> None:
        """Start streaming market data for given security IDs.

        Handles connection, subscription, buffering, and automatic reconnection.

        Args:
            security_ids: List of Dhan security IDs to subscribe to.

        Raises:
            RuntimeError: If already running or initialization fails.
        """
        if self._running:
            raise RuntimeError("Ingester already running")

        if not security_ids:
            raise ValueError("At least one security_id required")

        # Ensure DB pool is connected
        if self._db._pool is None:
            await self._db.connect()

        self._running = True
        reconnect_delay = self._reconnect_base_delay
        attempt = 0

        while self._running:
            try:
                attempt += 1
                logger.info(
                    "Starting WebSocket connection",
                    extra={"attempt": attempt, "security_count": len(security_ids)},
                )

                await self._connect_websocket()
                await self._subscribe(security_ids)

                # Start background flush task
                self._flush_task = asyncio.create_task(self._flush_buffer())

                # Reset reconnection delay on successful connection
                reconnect_delay = self._reconnect_base_delay

                # Run receive loop (blocks until disconnect)
                await self._receive_loop()

            except ConnectionClosed as exc:
                logger.warning(
                    "WebSocket connection closed",
                    extra={"code": exc.code, "reason": exc.reason, "attempt": attempt},
                )
            except WebSocketException as exc:
                logger.exception(
                    "WebSocket error",
                    extra={"error": str(exc), "error_type": type(exc).__name__, "attempt": attempt},
                )
            except asyncio.CancelledError:
                logger.info("Streaming cancelled")
                break
            except Exception as exc:
                logger.exception(
                    "Unexpected error in streaming loop",
                    extra={"error": str(exc), "error_type": type(exc).__name__, "attempt": attempt},
                )
            finally:
                await self._cleanup_connection()

            if self._running:
                delay = min(reconnect_delay, self._max_reconnect_delay)
                logger.info("Reconnecting", extra={"delay_seconds": delay, "attempt": attempt + 1})
                await asyncio.sleep(delay)
                reconnect_delay *= 2  # Exponential backoff

    async def _cleanup_connection(self) -> None:
        """Clean up WebSocket connection and flush buffer."""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        # Final buffer flush
        async with self._buffer_lock:
            if self._buffer:
                await self._insert_buffer()

        if self._ws:
            try:
                await self._ws.close()
            except Exception as exc:
                logger.debug("Error closing WebSocket", extra={"error": str(exc)})
            self._ws = None

    async def stop(self) -> None:
        """Gracefully stop the ingester."""
        logger.info("Stopping WebSocket ingester")
        self._running = False
        await self._cleanup_connection()
        await self._db.close()
        logger.info("WebSocket ingester stopped")
