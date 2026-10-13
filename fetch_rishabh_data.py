import os
import pandas as pd
from datetime import datetime, timedelta
import time
from dotenv import load_dotenv
from src.auth.angel_auth import AngelAuthenticator

# 1. Load environment variables so os.getenv() works
load_dotenv()

# Standard NSE Tokens for the 5 requested instruments
SYMBOLS = {
    "NIFTY_50": {"token": "26000", "exchange": "NSE"},
    "NIFTY_BANK": {"token": "26009", "exchange": "NSE"},
    "NIFTY_IT": {"token": "26014", "exchange": "NSE"},
    "RELIANCE": {"token": "2885", "exchange": "NSE"},
    "HDFCBANK": {"token": "1333", "exchange": "NSE"}
}

def fetch_1_year_data():
    print("Authenticating with Angel One...")
    auth = AngelAuthenticator()
    
    # 2. Execute the login to generate the JWT and Feed tokens
    auth.generate_session()
    sc = auth.smart_connect
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)
    
    for name, info in SYMBOLS.items():
        print(f"\nFetching 12 months of 2-Min data for {name}...")
        all_data = []
        
        current_start = start_date
        while current_start < end_date:
            current_end = min(current_start + timedelta(days=30), end_date)
            
            historicParam = {
                "exchange": info["exchange"],
                "symboltoken": info["token"],
                "interval": "TWO_MINUTE",
                "fromdate": current_start.strftime("%Y-%m-%d %H:%M"),
                "todate": current_end.strftime("%Y-%m-%d %H:%M")
            }
            
            try:
                response = sc.getCandleData(historicParam)
                if response and isinstance(response, dict) and response.get("data"):
                    all_data.extend(response["data"])
            except Exception as e:
                print(f"  [!] Error fetching {current_start.date()}: {e}")
            
            current_start = current_end
            time.sleep(0.4)
            
        if all_data:
            df = pd.DataFrame(all_data, columns=["timestamp", "open", "high", "low", "close", "volume"])
            
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index("timestamp", inplace=True)
            df = df[~df.index.duplicated(keep='first')]
            
            df.rename(columns={
                "open": f"{name}_open",
                "high": f"{name}_high",
                "low": f"{name}_low",
                "close": f"{name}_close",
                "volume": f"{name}_vol"
            }, inplace=True)
            
            filename = f"/home/ubuntu/project_pipeline/{name}_1yr_2min.csv"
            df.to_csv(filename)
            print(f"  --> Saved {filename} ({len(df)} rows)")

if __name__ == "__main__":
    fetch_1_year_data()
