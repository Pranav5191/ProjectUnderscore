import os
import json
import redis
import pandas as pd

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../"))
CSV_PATH = os.path.join(project_root, "historical_macro_matrix.csv")

ANCHOR_TOKENS = {
    "NIFTY_50": "99926000",
    "BANK_NIFTY": "99926009",
    "NIFTY_IT": "99926002",
    "RELIANCE": "2885",
    "HDFCBANK": "1333"
}
REVERSE_ANCHORS = {v: k for k, v in ANCHOR_TOKENS.items()}

def main():
    r = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)), decode_responses=True)
    
    print("[SYSTEM] Fetching all ticks from Redis replay_stream...")
    messages = r.xrange("replay_stream", "-", "+")
    
    if not messages:
        print("[ERROR] Stream is empty. Run replay_today.py first!")
        return

    records = []
    for msg_id, msg_data in messages:
        tick = json.loads(msg_data['data'])
        sec_id = str(tick.get('security_id'))
        if sec_id in REVERSE_ANCHORS:
            records.append({
                'timestamp': pd.to_datetime(tick['timestamp'], unit='ms', utc=True).tz_convert('Asia/Kolkata').tz_localize(None),
                'asset': REVERSE_ANCHORS[sec_id],
                'price': tick.get('ltp', 0.0),
                'vol': max(tick.get('best_bid_vol', 0), tick.get('best_ask_vol', 0))
            })

    if not records:
        print("[ERROR] No macro index ticks found in stream.")
        return

    df_raw = pd.DataFrame(records)
    df_raw.set_index('timestamp', inplace=True)

    print("[SYSTEM] Building 2-minute Macro Bars...")
    bars = []
    for asset in ANCHOR_TOKENS.keys():
        asset_df = df_raw[df_raw['asset'] == asset]
        # Align to 2-minute boundaries perfectly mirroring live execution
        resampled = asset_df.resample('2min', closed='left', label='left').agg({
            'price': ['first', 'max', 'min', 'last'],
            'vol': 'sum'
        })
        resampled.columns = [f"{asset}_open", f"{asset}_high", f"{asset}_low", f"{asset}_close", f"{asset}_vol"]
        bars.append(resampled)

    master_df = pd.concat(bars, axis=1)
    master_df.ffill(inplace=True) 
    master_df.fillna(0, inplace=True) 

    print("[SYSTEM] Calculating AI Features (ROC & Correlation)...")
    for anchor in ["NIFTY_50", "BANK_NIFTY", "NIFTY_IT"]:
        master_df[f"{anchor}_ROC_2m"] = (master_df[f"{anchor}_close"] / master_df[f"{anchor}_close"].shift(1)) - 1.0

    master_df["HDFC_REL_CORR"] = master_df["HDFCBANK_close"].rolling(window=15).corr(master_df["RELIANCE_close"])
    master_df.dropna(inplace=True)

    master_df.reset_index(inplace=True)
    master_df.rename(columns={'timestamp': 'timestamp'}, inplace=True)

    print(f"[SYSTEM] Appending {len(master_df)} compiled rows to {CSV_PATH}...")
    if os.path.exists(CSV_PATH):
        existing_df = pd.read_csv(CSV_PATH)
        
        # 1. Strip trailing timezones and force mixed format parsing
        raw_ts = existing_df['timestamp'].astype(str).str.split('+').str[0]
        existing_df['timestamp'] = pd.to_datetime(raw_ts, format='mixed')
        
        combined = pd.concat([existing_df, master_df]).drop_duplicates(subset=['timestamp'], keep='last')
        combined.to_csv(CSV_PATH, index=False)
    else:
        master_df.to_csv(CSV_PATH, index=False)

    print("[SUCCESS] Matrix fully saturated! You can now run the strategy engine.")

if __name__ == "__main__":
    main()