import os
import json
import redis
import pandas as pd

# Load the historical matrix you generated earlier
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../"))
csv_path = os.path.join(project_root, "historical_macro_matrix.csv")

print(f"[SYSTEM] Loading {csv_path} into memory...")
df = pd.read_csv(csv_path)
df['timestamp'] = pd.to_datetime(df['timestamp'])
df.set_index('timestamp', inplace=True)
df.sort_index(inplace=True)

r = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    decode_responses=True
)

print("[SYSTEM] Mock Oracle Sync initialized. Waiting for replay_stream...")
last_stream_id = "0-0"
current_2m_boundary = None

while True:
    # 1. Read from the exact same stream the strategy engine uses
    results = r.xread({"replay_stream": last_stream_id}, count=100, block=2000)
    
    if not results:
        continue
        
    _, entries = results[0]
    
    for entry_id, entry_data in entries:
        last_stream_id = entry_id
        tick = json.loads(entry_data["data"])
        
        tick_ts = tick.get('timestamp', 0)
        if not tick_ts: 
            continue
        
        # 2. Convert tick epoch to IST naive datetime (matches Angel One CSV format)
        tick_dt = pd.to_datetime(tick_ts, unit='ms', utc=True).tz_convert('Asia/Kolkata').tz_localize(None)
        
        # 3. Floor to nearest 2-minute boundary
        minute = tick_dt.minute
        floored_minute = (minute // 2) * 2
        boundary = tick_dt.replace(minute=floored_minute, second=0, microsecond=0)
        
        # 4. Only hit Redis when the 2-minute boundary rolls over
        if boundary != current_2m_boundary:
            current_2m_boundary = boundary
            
            # Fetch the latest available macro row up to this exact boundary using O(1) padding
            try:
                idx = df.index.get_indexer([boundary], method='pad')[0]
                if idx != -1:
                    row_dict = df.iloc[idx].to_dict()
                    r.set("macro_features_latest", json.dumps(row_dict))
                    print(f"[SYNC] Pushed {boundary.time()} Macro Data to Oracle.")
            except Exception as e:
                pass