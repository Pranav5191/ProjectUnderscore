import sqlite3
import pandas as pd
import numpy as np

def generate_stock_data(token: str, symbol: str, start_price: float, drift: float, vol: float, days: int = 260):
    # Generate business days only
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    
    # Random walk with drift for closing prices
    returns = np.random.normal(loc=drift, scale=vol, size=days)
    price_path = start_price * np.exp(np.cumsum(returns))
    
    df = pd.DataFrame({'timestamp': dates, 'close': price_path})
    
    # Generate realistic Open, High, Low around the Close
    df['open'] = df['close'].shift(1).fillna(start_price) * (1 + np.random.normal(0, vol/2, days))
    df['high'] = df[['open', 'close']].max(axis=1) * (1 + abs(np.random.normal(0, vol, days)))
    df['low'] = df[['open', 'close']].min(axis=1) * (1 - abs(np.random.normal(0, vol, days)))
    
    # Generate Volume (Institutional spikes vs retail flatline)
    df['volume'] = np.random.randint(10000, 5000000, size=days)
    
    df['token'] = token
    df['symbol'] = symbol
    
    # Reorder columns to match production schema
    return df[['token', 'symbol', 'timestamp', 'open', 'high', 'low', 'close', 'volume']]

def build_mock_db():
    db_path = "test_history.db"
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Enforce strict production schema
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS historical_data (
            token TEXT,
            symbol TEXT,
            timestamp DATE,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            PRIMARY KEY (token, timestamp)
        )
    ''')
    
    # 1. BULL-EQ: Strong 0.2% daily upward drift, low volatility
    bull_df = generate_stock_data("1001", "BULL-EQ", 100.0, 0.002, 0.015)
    
    # 2. BEAR-EQ: Violent -0.3% daily downward drift, high volatility
    bear_df = generate_stock_data("1002", "BEAR-EQ", 500.0, -0.003, 0.030)
    
    # 3. CHOP-EQ: Zero drift, massive 5% daily volatility (indecision)
    chop_df = generate_stock_data("1003", "CHOP-EQ", 50.0, 0.000, 0.050)
    
    # Combine and inject into SQLite
    master_df = pd.concat([bull_df, bear_df, chop_df])
    master_df['timestamp'] = master_df['timestamp'].dt.strftime('%Y-%m-%d')
    
    # Replace existing data to ensure a clean test state
    master_df.to_sql('historical_data', conn, if_exists='replace', index=False)
    
    # Re-apply the primary key since pandas to_sql drops constraints
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_token_time ON historical_data(token, timestamp)')
    
    conn.commit()
    conn.close()
    print("Mock database 'test_history.db' created successfully with 3 synthetic equities.")

if __name__ == "__main__":
    build_mock_db()