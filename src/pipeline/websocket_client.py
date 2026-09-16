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
            # Skip non-tick messages (like heartbeats)
            if "token" not in message:
                return

            # Safely extract the top level of the order book (Index 0)
            best_buy_data = message.get("best_5_buy_data", [])
            best_sell_data = message.get("best_5_sell_data", [])
            
            best_buy = best_buy_data[0] if best_buy_data else {}
            best_sell = best_sell_data[0] if best_sell_data else {}

            # Standardize the payload keys and convert paise to INR
            normalized_tick = {
                "timestamp": message.get("exchange_timestamp", 0),
                "security_id": int(message.get("token", 0)),
                "ltp": message.get("last_traded_price", 0) / 100.0,
                "ltq": message.get("last_traded_quantity", 0),
                "bid": best_buy.get("price", 0) / 100.0,
                "ask": best_sell.get("price", 0) / 100.0,
                "best_bid_vol": best_buy.get("quantity", 0),
                "best_ask_vol": best_sell.get("quantity", 0)
            }

            # 1. Stream to Batch Writer (PostgreSQL persistence)
            self.redis_client.xadd(
                "market:ticks",
                {"payload": json.dumps(normalized_tick)}
            )
            
            # 2. Publish directly to Strategy Engine (Real-time quantitative analysis)
            self.redis_client.publish(
                "live_ticks", 
                json.dumps(normalized_tick)
            )

        except Exception as e:
            logger.error(f"Error parsing/pushing tick: {e}")

    def _on_open(self, wsapp):
        logger.info("WebSocket connection established. Reading dynamic symbols...")

        # 1. Read the JSON config file
        try:
            # Adjust the path if your symbols.json is in a config folder
            with open("symbols.json", "r") as f:
                config = json.load(f)
                active_tokens = config.get("tokens", [])
        except Exception as e:
            logger.error(f"Failed to read symbols.json, using fallbacks. Error: {e}")
            active_tokens = ["3045", "2885"] # Safety fallback

        # 2. Build the payload for Angel One
        mode = 3 # SnapQuote for full order book
        token_list = [
            {
                "exchangeType": 1, # NSE Equity
                "tokens": active_tokens
            }
        ]

        # 3. Send the subscription request
        self.sws.subscribe(self.correlation_id, mode, token_list)
        logger.info(f"Successfully subscribed to {len(active_tokens)} stocks.")
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
