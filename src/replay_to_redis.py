import asyncio
import json
import os
import datetime
import decimal
import asyncpg
import redis
from dotenv import load_dotenv

load_dotenv()

async def replay_ticks():
    print("Connecting to Redis...")
    redis_client = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True
    )
    
    print("Connecting to PostgreSQL...")
    try:
        conn = await asyncpg.connect(
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASS", "postgres"),
            database=os.getenv("DB_NAME", "postgres"),
            host=os.getenv("DB_HOST", "localhost")
        )
    except Exception as e:
        print(f"Database connection failed: {e}")
        return

    table_name = "market_ticks"
    
    print(f"Fetching historical ticks from '{table_name}'...")
    records = await conn.fetch(f"SELECT * FROM {table_name} ORDER BY timestamp ASC LIMIT 50000")
    
    if not records:
        print("No ticks found in database table 'market_ticks'. Insert some sample data first!")
        await conn.close()
        redis_client.close()
        return

    print(f"Loaded {len(records)} ticks. Starting replay to Strategy Engine...")
    
    for record in records:
        tick_dict = dict(record)
        
        # Convert datetime and Decimal objects for JSON serialization
        for key, value in tick_dict.items():
            if isinstance(value, datetime.datetime):
                tick_dict[key] = value.isoformat()
            elif isinstance(value, decimal.Decimal):
                tick_dict[key] = float(value)
                
        redis_client.publish("live_ticks", json.dumps(tick_dict))
        await asyncio.sleep(0.0001)
        
    print("Replay finished.")
    await conn.close()
    redis_client.close()

if __name__ == "__main__":
    asyncio.run(replay_ticks())
