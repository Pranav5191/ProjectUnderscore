import os
import redis
import json
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
from src.auth.angel_auth import AngelAuthenticator
from logzero import logger

class AngelDataPipeline:
    def __init__(self):
        # Initialize Redis connection for the hot-path stream
        self.redis_client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True
        )
        
        # Authenticate and obtain tokens programmatically
        auth = AngelAuthenticator()
        tokens = auth.generate_session()
        
        # Clean JWT token for WebSocket compatibility
        raw_jwt = tokens["jwt_token"]
        self.auth_token = raw_jwt.replace("Bearer ", "") if raw_jwt.startswith("Bearer ") else raw_jwt
        self.feed_token = tokens["feed_token"]
        self.api_key = os.getenv("ANGEL_API_KEY")
        self.client_code = os.getenv("ANGEL_CLIENT_ID")
        
        self.sws = SmartWebSocketV2(
            self.auth_token,
            self.api_key,
            self.client_code,
            self.feed_token
        )
        
        self.correlation_id = "hft_stream_1"

    def _on_data(self, wsapp, message):
        """Callback triggered on every incoming market tick packet."""
        try:
            self.redis_client.xadd(
                "market:ticks",
                {"payload": json.dumps(message)}
            )
        except Exception as e:
            logger.error(f"Error pushing tick to Redis: {e}")

    def _on_open(self, wsapp):
        logger.info("WebSocket connection established. Subscribing to market feed...")
        
        action = 1 
        mode = 1   
        token_list = [
            {
                "exchangeType": 1, 
                "tokens": ["3045", "2885"] 
            }
        ]
        
        self.sws.subscribe(self.correlation_id, mode, token_list)

    def _on_error(self, wsapp, error):
        logger.error(f"WebSocket Error: {error}")

    def _on_close(self, wsapp, *args):
        logger.info("WebSocket gracefully disconnected from Angel One.")

    def start(self):
        # Register callback hooks
        self.sws.on_open = self._on_open
        self.sws.on_data = self._on_data
        self.sws.on_error = self._on_error
        self.sws.on_close = self._on_close
        
        # --- MONKEY PATCH: FIX ANGEL ONE SDK SHUTDOWN BUG ---
        # The SmartAPI SDK strictly defines `_on_close(self, wsapp)` but the underlying
        # websocket-client passes `(wsapp, status_code, close_msg)`. This mismatch
        # causes a crash upon Ctrl+C and triggers an unwanted reconnect loop.
        def patched_sdk_on_close(wsapp, *args):
            if self.sws.on_close:
                self.sws.on_close(wsapp, *args)
                
        # Overwrite the broken SDK method with our safe one before connecting
        self.sws._on_close = patched_sdk_on_close
        # ----------------------------------------------------
        
        logger.info("Starting Angel One WebSocket stream...")
        self.sws.connect()
