import os
import time
import json
import redis
import psycopg2
from datetime import datetime
from dotenv import load_dotenv

# Load credentials from .env file
load_dotenv()

def main():
    # Calculate exactly when "Today" started in epoch milliseconds
    midnight_today_ms = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    print(f"Filtering database for ticks occurring after: {midnight_today_ms} ms")

    # Connect to local Redis
    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True 
    )
    
    # Connect to local Postgres
    pg_conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASS", "password"),
        dbname=os.getenv("DB_NAME", "market_data")
    )
    
    print("Connected to local databases. Starting replay...")

    # Server-side cursor to prevent RAM crashing on massive tick tables
    with pg_conn.cursor(name='tick_cursor') as cursor:
        # INJECTED: The WHERE clause strictly isolating today's data
        cursor.execute(f"""
            SELECT security_id, ltp, ltq, bid, ask, best_bid_vol, best_ask_vol, timestamp 
            FROM market_ticks 
            WHERE timestamp >= {midnight_today_ms}
            ORDER BY timestamp ASC;
        """)
        
        count = 0
        for row in cursor:
            # Map exactly to the new SELECT order
            tick_data = {
                "security_id": row[0],
                "ltp": float(row[1]) if row[1] else 0.0,
                "ltq": int(row[2]) if row[2] else 0,
                "bid": float(row[3]) if row[3] else 0.0,
                "ask": float(row[4]) if row[4] else 0.0,
                "best_bid_vol": int(row[5]) if row[5] else 0,
                "best_ask_vol": int(row[6]) if row[6] else 0,
                "timestamp": int(row[7]) if row[7] else 0
            }
            
            # Push to Redis channel 
            r.publish("live_ticks", json.dumps(tick_data))
            
            count += 1
            if count % 1000 == 0:
                print(f"Replayed {count} ticks...")
                
            # Artificial latency
            time.sleep(0.0001)

    print("Replay complete.")
    pg_conn.close()

if __name__ == "__main__":
    main()
    #