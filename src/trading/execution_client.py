"""Asynchronous Dhan order execution client with dynamic token management."""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiohttp
from aiohttp import ClientTimeout, TCPConnector

from src.auth.dhan_auth import DhanAuthenticator

logger = logging.getLogger(__name__)

class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"
    STOP_LOSS_MARKET = "STOP_LOSS_MARKET"

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class OrderValidity(str, Enum):
    DAY = "DAY"
    IOC = "IOC"
    GTT = "GTT"

class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

@dataclass(frozen=True, slots=True)
class OrderRequest:
    security_id: int
    exchange_segment: str
    transaction_type: OrderSide
    order_type: OrderType
    quantity: int
    price: float | None = None
    trigger_price: float | None = None
    validity: OrderValidity = OrderValidity.DAY
    disclosed_quantity: int = 0
    after_market_order: bool = False
    bo_profit_value: float | None = None
    bo_stop_loss_value: float | None = None

@dataclass(frozen=True, slots=True)
class OrderResponse:
    order_id: str
    order_status: OrderStatus
    client_order_id: str | None = None
    error_message: str | None = None

@dataclass(frozen=True, slots=True)
class ModifyOrderRequest:
    order_id: str
    quantity: int | None = None
    price: float | None = None
    trigger_price: float | None = None
    order_type: OrderType | None = None
    validity: OrderValidity | None = None

class RateLimiter:
    """Token bucket rate limiter for Dhan API."""
    def __init__(self, max_requests: int = 100, window_seconds: float = 60.0) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._requests: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            cutoff = now - self._window_seconds
            self._requests = [t for t in self._requests if t > cutoff]

            if len(self._requests) >= self._max_requests:
                oldest = self._requests[0]
                wait_time = oldest + self._window_seconds - now
                if wait_time > 0:
                    logger.warning("Rate limit reached, waiting", extra={"wait_seconds": wait_time})
                    await asyncio.sleep(wait_time)

            self._requests.append(now)

class DhanExecutionClient:
    """Async REST client for Dhan order execution with dynamic token refresh."""
    _BASE_URL = "https://api.dhan.co/v2"
    _ENDPOINTS = {
        "place_order": "/orders",
        "modify_order": "/orders/{order_id}",
        "cancel_order": "/orders/{order_id}",
        "order_status": "/orders/{order_id}",
        "order_list": "/orders",
    }

    def __init__(
        self,
        authenticator: DhanAuthenticator,
        client_id: str,
        max_retries: int = 3,
        base_retry_delay: float = 0.5,
        rate_limit_requests: int = 100,
        rate_limit_window: float = 60.0,
        request_timeout: float = 30.0,
    ) -> None:
        self._auth = authenticator
        self._client_id = client_id
        self._max_retries = max_retries
        self._base_retry_delay = base_retry_delay
        self._rate_limiter = RateLimiter(rate_limit_requests, rate_limit_window)
        self._timeout = ClientTimeout(total=request_timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = TCPConnector(limit=20, limit_per_host=10, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(connector=connector, timeout=self._timeout)
        return self._session

    async def _get_headers(self) -> dict[str, str]:
        token = await self._auth.get_session()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None, path_params: dict[str, str] | None = None) -> dict[str, Any]:
        await self._rate_limiter.acquire()
        url = f"{self._BASE_URL}{endpoint}"
        if path_params:
            url = url.format(**path_params)

        headers = await self._get_headers()
        session = await self._get_session()
        last_exception: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                async with session.request(method, url, json=payload, headers=headers) as resp:
                    response_data = await resp.json()
                    if resp.status == 429:
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info, history=resp.history, status=429, message="Rate limited"
                        )
                    if resp.status >= 400:
                        error_msg = response_data.get("errorMessage", f"HTTP {resp.status}")
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info, history=resp.history, status=resp.status, message=error_msg
                        )
                    return response_data
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exception = exc
                if attempt < self._max_retries:
                    delay = self._base_retry_delay * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)
                else:
                    raise
        raise RuntimeError(f"Request failed after {self._max_retries} attempts") from last_exception

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        if order.quantity <= 0:
            raise ValueError("Quantity must be positive")
        
        payload = {
            "dhanClientId": self._client_id,
            "securityId": str(order.security_id),
            "exchangeSegment": order.exchange_segment,
            "transactionType": order.transaction_type.value,
            "orderType": order.order_type.value,
            "quantity": order.quantity,
            "validity": order.validity.value,
            "disclosedQuantity": order.disclosed_quantity,
            "afterMarketOrder": order.after_market_order,
        }
        
        if order.price is not None: payload["price"] = order.price
        if order.trigger_price is not None: payload["triggerPrice"] = order.trigger_price

        response = await self._request("POST", self._ENDPOINTS["place_order"], payload=payload)
        return OrderResponse(
            order_id=str(response.get("orderId", "")),
            order_status=OrderStatus(response.get("orderStatus", "PENDING")),
            client_order_id=response.get("clientOrderId"),
            error_message=response.get("errorMessage"),
        )

    async def get_order_status(self, order_id: str) -> OrderResponse:
        response = await self._request("GET", self._ENDPOINTS["order_status"], path_params={"order_id": order_id})
        return OrderResponse(
            order_id=order_id,
            order_status=OrderStatus(response.get("orderStatus", "PENDING")),
            client_order_id=response.get("clientOrderId"),
            error_message=response.get("errorMessage"),
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
