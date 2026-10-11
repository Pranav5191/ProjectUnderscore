import os
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
from SmartApi import SmartConnect
import pyotp

# Load credentials
current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, "../../.env")
load_dotenv(dotenv_path=env_path)

API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_CODE = os.getenv("ANGEL_CLIENT_ID")
PASSWORD = os.getenv("ANGEL_PIN")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

# Match the tokens exactly as defined in your macro_matrix.py
ANCHOR_TOKENS = {
    "NIFTY_50": {"token": "99926000", "exchange": "NSE"}, # Verify index tokens, sometimes Angel uses "99926000" for Nifty
    "BANK_NIFTY": {"token": "99926009", "exchange": "NSE"},
    "NIFTY_IT": {"token": "99926002", "exchange": "NSE"},
    "RELIANCE": {"token": "2885", "exchange": "NSE"},
    "HDFCBANK": {"token": "1333", "exchange": "NSE"}
}

def authenticate():
    print("[SYSTEM] Authenticating with Angel One SmartAPI...")
    smartApi = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    data = smartApi.generateSession(CLIENT_CODE, PASSWORD, totp)
    
    if data['status']:
        print("[SUCCESS] Session Generated.")
        return smartApi
    else:
        raise Exception(f"Login failed: {data.get('message')}")

def fetch_chunked_data(smartApi, symbol_name, token_info, start_date, end_date):
    """Fetches 1-minute data in 30-day chunks to respect API limits."""
    print(f"\n[FETCH] Downloading {symbol_name}...")
    
    all_candles = []
    current_start = start_date
    
    while current_start < end_date:
        current_end = min(current_start + timedelta(days=30), end_date)
        
        request_params = {
            "exchange": token_info["exchange"],
            "symboltoken": token_info["token"],
            "interval": "ONE_MINUTE",
            "fromdate": current_start.strftime("%Y-%m-%d %H:%M"),
            "todate": current_end.strftime("%Y-%m-%d %H:%M")
        }
        
        try:
            response = smartApi.getCandleData(request_params)
            if response.get('status') and response.get('data'):
                all_candles.extend(response['data'])
            else:
                print(f"  [WARN] No data for {current_start.date()} to {current_end.date()}")
        except Exception as e:
            print(f"  [ERROR] API Call failed: {e}")
            
        current_start = current_end
        time.sleep(0.5) # Prevent rate-limiting bans
        
    if not all_candles:
        return pd.DataFrame()
        
    df = pd.DataFrame(all_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'vol'])
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df.set_index('timestamp', inplace=True)
    
    # Rename columns to match the ML Matrix requirement
    df.columns = [f"{symbol_name}_{col}" for col in df.columns]
    return df

def build_macro_matrix():
    smartApi = authenticate()
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365) # 1 Year History
    
    master_df = None
    
    # 1. Download and merge 1-minute data for all anchors
    for symbol, info in ANCHOR_TOKENS.items():
        df = fetch_chunked_data(smartApi, symbol, info, start_date, end_date)
        
        if master_df is None:
            master_df = df
        else:
            # Outer join aligns timestamps perfectly across all 5 assets
            master_df = master_df.join(df, how='outer')

    print("\n[SYSTEM] Resampling 1-minute data to 2-minute bars...")
    
    # 2. Resample to 2-Minute Bars
    resample_logic = {}
    for symbol in ANCHOR_TOKENS.keys():
        resample_logic[f"{symbol}_open"] = 'first'
        resample_logic[f"{symbol}_high"] = 'max'
        resample_logic[f"{symbol}_low"] = 'min'
        resample_logic[f"{symbol}_close"] = 'last'
        resample_logic[f"{symbol}_vol"] = 'sum'
        
    matrix_2m = master_df.resample('2min').agg(resample_logic)
    matrix_2m.dropna(how='all', inplace=True) # Drop totally empty overnight/weekend rows
    matrix_2m.ffill(inplace=True) # Forward fill isolated missing ticks
    
    print("[SYSTEM] Calculating ML Features (ROC & Correlation)...")
    
    # 3. Apply Feature Engineering (Same logic as macro_matrix.py)
    for anchor in ["NIFTY_50", "BANK_NIFTY", "NIFTY_IT"]:
        col = f"{anchor}_close"
        matrix_2m[f"{anchor}_ROC_2m"] = (matrix_2m[col] / matrix_2m[col].shift(1)) - 1.0

    ROLLING_WINDOW = 15
    matrix_2m["HDFC_REL_CORR"] = matrix_2m["HDFCBANK_close"].rolling(window=ROLLING_WINDOW).corr(matrix_2m["RELIANCE_close"])
    
    matrix_2m.dropna(inplace=True) # Drop initial rows that lack enough data for the rolling window
    
    # 4. Save to root directory
    output_path = os.path.join(current_dir, "../../historical_macro_matrix.csv")
    matrix_2m.to_csv(output_path)
    
    print(f"\n[SUCCESS] Historical Macro Matrix generated successfully: {output_path}")
    print(f"Total 2-minute rows extracted: {len(matrix_2m):,}")

if __name__ == "__main__":
    build_macro_matrix()