"""Background worker to drain market ticks from Redis to PostgreSQL."""

import asyncio
import json
import logging
import os
import redis.asyncio as redis
from src.pipeline.db import DatabaseManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

async def main() -> None:
    # 1. Connect to PostgreSQL
    db = DatabaseManager()
    await db.connect()

    # 2. Connect to Redis
    redis_client = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True
    )
    
    stream_key = "market:ticks"
    batch_size = 5000

    logger.info("Starting Redis-to-PostgreSQL batch writer...")

    try:
        while True:
            # Fetch up to `batch_size` ticks starting from the oldest available ("0-0")
            messages = await redis_client.xread({stream_key: "0-0"}, count=batch_size)

            if not messages:
                logger.info("Redis stream is empty. Waiting for new ticks...")
                await asyncio.sleep(2)
                continue

            stream_data = messages[0][1]  # Extract the list of (msg_id, data)
            
            ticks = []
            msg_ids = []
            
            for msg_id, data in stream_data:
                try:
                    payload = json.loads(data["payload"])
                    ticks.append(payload)
                    msg_ids.append(msg_id)
                except Exception as e:
                    logger.error(f"Failed to parse tick payload: {e}")
                    msg_ids.append(msg_id)  # Still track the ID so we delete the bad data

            if ticks:
                try:
                    # Insert into PostgreSQL using your db.py DatabaseManager
                    await db.insert_ticks_batch(ticks)
                    
                    # Delete the processed ticks from Redis to free up RAM
                    await redis_client.xdel(stream_key, *msg_ids)
                    logger.info(f"Successfully processed and cleared {len(ticks)} ticks.")
                except Exception as e:
                    logger.error(f"Database insertion failed: {e}")
                    await asyncio.sleep(5)  # Wait before retrying on DB failure
                    
    except asyncio.CancelledError:
        logger.info("Batch writer shutting down...")
    finally:
        await db.close()
        await redis_client.close()

if __name__ == "__main__":
    asyncio.run(main())
