"""Authentication module for Dhan API integration."""

import asyncio
import logging
import os
from typing import Optional

import pyotp
from dhanhq import DhanLogin

logger = logging.getLogger(__name__)


class DhanAuthenticator:
    """Asynchronous authenticator for Dhan trading API.

    Handles TOTP generation, session creation, and robust retry logic
    for authentication against the Dhan API.
    """

    def __init__(self) -> None:
        """Initialize the authenticator with credentials from environment variables.

        Raises:
            ValueError: If any required environment variable is missing.
        """
        self._client_id = os.getenv("DHAN_CLIENT_ID")
        self._pin = os.getenv("DHAN_PIN")
        self._totp_secret = os.getenv("DHAN_TOTP_SECRET")
        self._api_key = os.getenv("DHAN_API_KEY")

        missing = [
            name
            for name, value in [
                ("DHAN_CLIENT_ID", self._client_id),
                ("DHAN_PIN", self._pin),
                ("DHAN_TOTP_SECRET", self._totp_secret),
                ("DHAN_API_KEY", self._api_key),
            ]
            if not value
        ]

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        self._totp = pyotp.TOTP(self._totp_secret)
        self._login_client = DhanLogin(self._client_id)

    def _generate_totp(self) -> str:
        """Generate current 6-digit TOTP code.

        Returns:
            Current TOTP as a 6-digit string.
        """
        return self._totp.now()

    async def get_session(self, max_retries: int = 3, base_delay: float = 1.0) -> str:
        """Authenticate and retrieve access token from Dhan API.

        Implements exponential backoff retry logic for handling rate limits
        and transient network errors.

        Args:
            max_retries: Maximum number of retry attempts (default: 3).
            base_delay: Base delay in seconds for exponential backoff (default: 1.0).

        Returns:
            Access token string on successful authentication.

        Raises:
            RuntimeError: If authentication fails after all retries.
            ValueError: If credentials are invalid.
        """
        totp = self._generate_totp()
        logger.info("Initiating Dhan authentication", extra={"client_id": self._client_id})

        last_exception: Optional[Exception] = None

        for attempt in range(1, max_retries + 1):
            try:
                logger.debug(
                    "Authentication attempt",
                    extra={"attempt": attempt, "max_retries": max_retries},
                )

                token = await asyncio.to_thread(
                    self._login_client.generate_token, self._pin, totp
                )

                if not token:
                    raise ValueError("Empty token received from Dhan API")

                logger.info(
                    "Authentication successful",
                    extra={"client_id": self._client_id, "attempt": attempt},
                )
                return token

            except Exception as exc:
                last_exception = exc
                logger.warning(
                    "Authentication attempt failed",
                    extra={
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )

                if attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.info(
                        "Retrying after delay",
                        extra={"delay_seconds": delay, "next_attempt": attempt + 1},
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error(
                        "All authentication attempts exhausted",
                        extra={"max_retries": max_retries, "final_error": str(exc)},
                    )

        raise RuntimeError(
            f"Authentication failed after {max_retries} attempts"
        ) from last_exception
