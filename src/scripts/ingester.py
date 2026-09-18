import os
import time
import sqlite3
from datetime import datetime, timedelta

class MasterIngester:
    def __init__(self, api_client, db_path="master_history.db"):
        self.api = api_client
        self.db_path = db_path
        self._setup_database()

    def _setup_database(self):
        """Creates the B-Tree optimized SQLite schema if it does not exist."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # FIXED: Renamed to historical_data to match screener.py
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS historical_data (
                    token TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timestamp DATE NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume INTEGER NOT NULL,
                    PRIMARY KEY (token, timestamp)
                )
            """)
            conn.commit()

    def _get_last_sync_date(self, token: str) -> str:
        """Queries the database to determine the delta sync requirement."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(timestamp) FROM historical_data WHERE token = ?", (token,))
            result = cursor.fetchone()[0]
            
            if result:
                last_date = datetime.strptime(result, '%Y-%m-%d')
                return (last_date + timedelta(days=1)).strftime('%Y-%m-%d 09:15')
            
            # FIXED: Pulled 260 days to satisfy the 252-day screener guardrail (padding for holidays)
            return (datetime.now() - timedelta(days=260)).strftime('%Y-%m-%d 09:15')

    def sync_database(self, surviving_equities: list):
        """
        The Master Network Loop. 
        Respects the 450ms API throttle and stages all data in RAM for an atomic commit.
        """
        print(f"\n[SYSTEM] Commencing Master OHLCV Sync for {len(surviving_equities)} equities...")
        
        to_date = datetime.now().strftime('%Y-%m-%d 15:30')
        staged_rows = []
        count = 0

        for equity in surviving_equities:
            count += 1
            token = equity['token']
            symbol = equity['symbol']
            
            from_date = self._get_last_sync_date(token)
            
            if datetime.strptime(from_date.split()[0], '%Y-%m-%d').date() > datetime.now().date():
                continue

            if count % 100 == 0:
                print(f"[{count}/{len(surviving_equities)}] Downloading History (RAM Staging)...")

            payload = {
                "exchange": "NSE",
                "symboltoken": token,
                "interval": "ONE_DAY",
                "fromdate": from_date,
                "todate": to_date
            }

            max_retries = 2
            for attempt in range(max_retries):
                try:
                    response = self.api.getCandleData(payload)
                    time.sleep(0.45) 
                    
                    if response and not response.get('status'):
                        error_msg = response.get('message', '').lower()
                        if 'limit' in error_msg or 'throttle' in error_msg:
                            print(f"[WARNING] API Rate Limit on {symbol}. Backing off 5s...")
                            time.sleep(5.0)
                            continue
                    
                    data = response.get('data') if response else None
                    if data:
                        for row in data:
                            timestamp_iso = row[0][:10]
                            staged_rows.append((
                                token, symbol, timestamp_iso, 
                                row[1], row[2], row[3], row[4], row[5]
                            ))
                    break 

                except Exception as e:
                    print(f"[API ERROR] {symbol} Fetch Failed: {e}")
                    time.sleep(1.0)

        if staged_rows:
            print(f"\n[SYSTEM] Network Loop Complete. Executing Atomic Disk Write for {len(staged_rows)} rows...")
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # FIXED: Insert into historical_data
                cursor.executemany("""
                    INSERT OR REPLACE INTO historical_data 
                    (token, symbol, timestamp, open, high, low, close, volume) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, staged_rows)
                conn.commit()
            print("[SUCCESS] Master Database Synced and Locked.")
        else:
            print("\n[SYSTEM] Database already up to date. No new rows committed.")