import os
import json
import time
import threading
import requests
import redis
from dotenv import load_dotenv

load_dotenv()

r = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    decode_responses=True
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TIERS = {
    1: os.getenv("TIER1_CHAT_ID") or DEFAULT_CHAT_ID,
    2: os.getenv("TIER2_CHAT_ID") or DEFAULT_CHAT_ID,
    3: os.getenv("TIER3_CHAT_ID") or DEFAULT_CHAT_ID,
}

def _direct_tg_send(tier: int, source: str, message: str):
    if not BOT_TOKEN:
        return
    chat_id = TIERS.get(int(tier)) or DEFAULT_CHAT_ID
    if not chat_id:
        return
    icons = {1: "🚨", 2: "⚡", 3: "⚙️"}
    text = f"{icons.get(int(tier), '🔔')} <b>[{source}]</b>\n{message}"
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=6
        )
    except Exception:
        pass

def push_alert(tier: int, source: str, message: str, force_direct: bool = False):
    """
    Routes alerts to Telegram.
    If telegram_listener.py is not draining Redis (e.g. local Windows replay),
    automatically dispatches via background HTTP thread so no execution log is ever missed.
    """
    payload = {
        "tier": tier,
        "source": source,
        "message": message,
        "timestamp": int(time.time() * 1000)
    }
    try:
        q_len = r.rpush("sys_alerts_queue", json.dumps(payload))
        # If queue is backing up (>2 items), listener daemon isn't running -> send directly
        if force_direct or q_len > 2:
            r.ltrim("sys_alerts_queue", -1, 0)
            threading.Thread(target=_direct_tg_send, args=(tier, source, message), daemon=True).start()
    except Exception:
        threading.Thread(target=_direct_tg_send, args=(tier, source, message), daemon=True).start()
