import os
import uuid
import time
import requests
from dotenv import load_dotenv
from src.utils.api_tracker import APITracker, get_dhan_token

load_dotenv()

class DhanSandboxClient:
    """
    Dhan Sandbox / Live Order Router using DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET, DHAN_API_KEY.
    Controlled via EXECUTION_MODE in .env:
      - PAPER        : Local paper simulation only (default)
      - DHAN_SANDBOX : Fires orders to Dhan Sandbox endpoint + paper ledger
      - DUAL         : Fires both Dhan Sandbox & Paper ledger simultaneously
    """
    def __init__(self):
        self.mode = os.getenv("EXECUTION_MODE", "PAPER").strip().upper()
        self.client_id = os.getenv("DHAN_CLIENT_ID", "")
        self.pin = os.getenv("DHAN_PIN", "")
        self.totp_secret = os.getenv("DHAN_TOTP_SECRET", "")
        self.api_key = os.getenv("DHAN_API_KEY", "")
        self.base_url = os.getenv("DHAN_SANDBOX_URL", "https://sandbox.dhan.co/v2")

    def is_enabled(self) -> bool:
        return self.mode in ("DHAN_SANDBOX", "DUAL", "SANDBOX")

    def place_intraday_order(self, sec_id: str, symbol: str, action: str, qty: int, price: float, trace_id: str) -> dict:
        t0 = time.time()
        payload = {
            "dhanClientId": self.client_id or "SANDBOX",
            "correlationId": trace_id[:24],
            "transactionType": "BUY" if action == "BUY" else "SELL",
            "exchangeSegment": "NSE_EQ",
            "productType": "INTRADAY",
            "orderType": "LIMIT",
            "validity": "DAY",
            "tradingSymbol": symbol.replace("-EQ", ""),
            "securityId": str(sec_id),
            "quantity": int(qty),
            "price": round(float(price), 2)
        }

        token = get_dhan_token()
        if not token:
            return {
                "status": "SANDBOX_SIMULATED",
                "order_id": f"DHAN-SBX-{uuid.uuid4().hex[:8].upper()}",
                "latency_ms": round((time.time() - t0) * 1000, 2),
                "payload": payload
            }

        try:
            headers = {
                "access-token": token,
                "client-id": self.client_id,
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            resp = requests.post(f"{self.base_url}/orders", json=payload, headers=headers, timeout=5)
            APITracker.record("DHAN", success=(resp.status_code in (200, 201)), rate_limited=(resp.status_code == 429))
            latency_ms = round((time.time() - t0) * 1000, 2)
            if resp.status_code in (200, 201):
                data = resp.json()
                return {
                    "status": "SANDBOX_FILLED",
                    "order_id": data.get("orderId", f"DHAN-{uuid.uuid4().hex[:6]}"),
                    "latency_ms": latency_ms
                }
            return {
                "status": f"SANDBOX_HTTP_{resp.status_code}",
                "order_id": f"DHAN-SBX-{uuid.uuid4().hex[:6]}",
                "latency_ms": latency_ms
            }
        except Exception as e:
            APITracker.record("DHAN", success=False)
            return {
                "status": f"SANDBOX_ERR:{str(e)[:40]}",
                "order_id": f"DHAN-LOCAL-{uuid.uuid4().hex[:6]}",
                "latency_ms": round((time.time() - t0) * 1000, 2)
            }
