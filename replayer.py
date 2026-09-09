import os
import time
import json
import redis
import psycopg2
from dotenv import load_dotenv

# Load credentials from .env file
load_dotenv()

def main():
    # Connect to local Redis
    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True # Automatically decodes byte responses to strings
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
        # IMPORTANT: Change 'ticks' to your actual table name if Pranav named it differently
        # Select only what you need. Assuming Pranav stores best_bid_vol and best_ask_vol.
        cursor.execute("""
            SELECT security_id, ltp, bid, ask, volume, timestamp 
            FROM market_ticks 
            ORDER BY timestamp ASC;
        """)
        
        count = 0
        for row in cursor:
            # Map exactly to what the database returns
            tick_data = {
                "security_id": row[0],
                "ltp": float(row[1]) if row[1] else 0.0,
                "bid": float(row[2]) if row[2] else 0.0,
                "ask": float(row[3]) if row[3] else 0.0,
                "volume": int(row[4]) if row[4] else 0,
                "timestamp": int(row[5]) if row[5] else 0
            }
            
            # Push to Redis channel (Change "live_ticks" to whatever Pranav uses)
            r.publish("live_ticks", json.dumps(tick_data))
            
            count += 1
            if count % 1000 == 0:
                print(f"Replayed {count} ticks...")
                
            # Artificial latency: 1 millisecond sleep to mimic live tick spacing
            time.sleep(0.001) 

    print("Replay complete.")
    pg_conn.close()

if __name__ == "__main__":
    main()