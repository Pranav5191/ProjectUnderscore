import os
import json
import time
import asyncio
import requests
from datetime import datetime
from dotenv import load_dotenv

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

QUOTA_FILE = os.path.join(PROJECT_ROOT, "data", "api_quota_ledger.json")
os.makedirs(os.path.dirname(QUOTA_FILE), exist_ok=True)

MONTHLY_LIMITS = {
    "ANGEL_ONE": int(os.getenv("ANGEL_MONTHLY_LIMIT", 150000)),
    "DHAN": int(os.getenv("DHAN_MONTHLY_LIMIT", 250000)),
    "GEMINI": int(os.getenv("GEMINI_MONTHLY_LIMIT", 45000)),
    "GROQ": int(os.getenv("GROQ_MONTHLY_LIMIT", 45000)),
}

DAILY_LIMITS = {
    "ANGEL_ONE": int(os.getenv("ANGEL_DAILY_LIMIT", 8000)),
    "DHAN": int(os.getenv("DHAN_DAILY_LIMIT", 15000)),
    "GEMINI": int(os.getenv("GEMINI_DAILY_LIMIT", 1500)),
    "GROQ": int(os.getenv("GROQ_DAILY_LIMIT", 14400)),
}

_CACHED_DHAN_TOKEN = None


def get_dhan_token() -> str:
    """
    Resolves Dhan token using DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET, DHAN_API_KEY.
    Attempts TOTP session via DhanAuthenticator first, falling back to DHAN_API_KEY.
    """
    global _CACHED_DHAN_TOKEN
    if _CACHED_DHAN_TOKEN:
        return _CACHED_DHAN_TOKEN

    client_id = os.getenv("DHAN_CLIENT_ID")
    pin = os.getenv("DHAN_PIN")
    totp_secret = os.getenv("DHAN_TOTP_SECRET")
    api_key = os.getenv("DHAN_API_KEY")

    if client_id and pin and totp_secret:
        try:
            from src.auth.dhan_auth import DhanAuthenticator
            auth = DhanAuthenticator()
            token = asyncio.run(auth.get_session(max_retries=1))
            if token and isinstance(token, str):
                _CACHED_DHAN_TOKEN = token
                return _CACHED_DHAN_TOKEN
        except Exception:
            pass

    if api_key:
        _CACHED_DHAN_TOKEN = api_key
        return _CACHED_DHAN_TOKEN
    return ""


class APITracker:
    @staticmethod
    def _load() -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        month = datetime.now().strftime("%Y-%m")
        default = {"day": today, "month": month, "providers": {}}
        if os.path.exists(QUOTA_FILE):
            try:
                with open(QUOTA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("month") != month:
                    data = {"day": today, "month": month, "providers": {}}
                elif data.get("day") != today:
                    data["day"] = today
                    for p in data.get("providers", {}).values():
                        p["daily_calls"] = 0
                        p["daily_errors"] = 0
                return data
            except Exception:
                pass
        return default

    @staticmethod
    def _save(data: dict):
        try:
            with open(QUOTA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception:
            pass

    @classmethod
    def record(cls, provider: str, success: bool = True, rate_limited: bool = False):
        data = cls._load()
        p = data["providers"].setdefault(provider, {
            "daily_calls": 0,
            "monthly_calls": 0,
            "daily_errors": 0,
            "rate_limit_hits": 0,
            "last_call": ""
        })
        p["daily_calls"] += 1
        p["monthly_calls"] += 1
        if not success:
            p["daily_errors"] += 1
        if rate_limited:
            p["rate_limit_hits"] += 1
        p["last_call"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cls._save(data)

    @classmethod
    def is_angel_exhausted(cls) -> bool:
        data = cls._load()
        p = data.get("providers", {}).get("ANGEL_ONE", {})
        if p.get("daily_calls", 0) >= DAILY_LIMITS["ANGEL_ONE"]:
            return True
        if p.get("monthly_calls", 0) >= MONTHLY_LIMITS["ANGEL_ONE"]:
            return True
        return False

    @classmethod
    def get_summary_text(cls) -> str:
        data = cls._load()
        lines = [f"📊 <b>API Quota Ledger ({data['day']})</b>"]
        for prov in ["ANGEL_ONE", "DHAN", "GEMINI", "GROQ"]:
            p = data.get("providers", {}).get(prov, {})
            d_used = p.get("daily_calls", 0)
            m_used = p.get("monthly_calls", 0)
            rl = p.get("rate_limit_hits", 0)
            d_lim = DAILY_LIMITS.get(prov, 10000)
            m_lim = MONTHLY_LIMITS.get(prov, 100000)
            lines.append(
                f"• <b>{prov}</b>: Day <code>{d_used}/{d_lim}</code> | "
                f"Month <code>{m_used}/{m_lim}</code> | 429s: <code>{rl}</code>"
            )
        return "\n".join(lines)


def fetch_dhan_historical(token: str, interval: str, from_date: str, to_date: str) -> list:
    """
    Fallback fetcher using Dhan v2 Historical / Intraday Charts API.
    Uses DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET, DHAN_API_KEY from .env.
    """
    access_token = get_dhan_token()
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    if not access_token:
        return []

    headers = {
        "access-token": access_token,
        "client-id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    is_intraday = interval != "ONE_DAY"
    url = "https://api.dhan.co/v2/charts/intraday" if is_intraday else "https://api.dhan.co/v2/charts/historical"
    payload = {
        "securityId": str(token),
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "fromDate": from_date[:10],
        "toDate": to_date[:10]
    }
    if is_intraday:
        payload["interval"] = "1"

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        APITracker.record("DHAN", success=(resp.status_code == 200), rate_limited=(resp.status_code == 429))
        if resp.status_code == 200:
            d = resp.json()
            opens = d.get("open", [])
            highs = d.get("high", [])
            lows = d.get("low", [])
            closes = d.get("close", [])
            vols = d.get("volume", [])
            ts_list = d.get("timestamp", [])
            rows = []
            for i in range(min(len(opens), len(ts_list))):
                dt_iso = datetime.fromtimestamp(int(ts_list[i])).strftime("%Y-%m-%dT%H:%M:%S")
                rows.append([dt_iso, float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i]), int(vols[i])])
            return rows
    except Exception:
        APITracker.record("DHAN", success=False)
    return []


def fetch_candles_with_fallback(angel_client, token: str, interval: str, from_str: str, to_str: str) -> list:
    """
    Primary: Angel One SmartAPI.
    Automatic Failover: Switches to Dhan API if Angel One quota is exhausted, throws b'', or hits rate limits.
    """
    if not APITracker.is_angel_exhausted() and angel_client is not None:
        try:
            params = {
                "exchange": "NSE",
                "symboltoken": str(token),
                "interval": interval,
                "fromdate": from_str,
                "todate": to_str
            }
            resp = angel_client.getCandleData(params)
            time.sleep(0.38)
            if isinstance(resp, dict) and resp.get("status") and resp.get("data"):
                APITracker.record("ANGEL_ONE", success=True)
                return resp["data"]
            msg = str(resp.get("message", "") if isinstance(resp, dict) else "").lower()
            is_rl = ("limit" in msg) or ("throttle" in msg) or ("exceed" in msg)
            APITracker.record("ANGEL_ONE", success=False, rate_limited=is_rl)
        except Exception:
            APITracker.record("ANGEL_ONE", success=False, rate_limited=True)

    return fetch_dhan_historical(str(token), interval, from_str, to_str)
