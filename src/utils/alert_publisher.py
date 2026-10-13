import os
import json
import time
import redis
from dotenv import load_dotenv

load_dotenv()

# Use a single connection pool
r = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    decode_responses=True
)

def push_alert(tier: int, source: str, message: str):
    """
    Pushes an alert to the Redis queue instantly.
    Tier 1: Critical (Exceptions, Crashes, Margin Limits)
    Tier 2: Executions (Trade Entries, Exits, SL/TP)
    Tier 3: Audit (Cron jobs, S3 Syncs, File cleanups)
    """
    payload = {
        "tier": tier,
        "source": source,
        "message": message,
        "timestamp": int(time.time() * 1000)
    }
    r.rpush("sys_alerts_queue", json.dumps(payload))