import sqlite3
import gc
import time
import pandas as pd
from typing import Dict, List

# Import your engineered modules
from src.scoring.historical_volatility import HistoricalVolatilityScorer
from src.scoring.gap_risk import GapRiskScorer
from src.scoring.cmf import ChaikinMoneyFlowScorer
from src.scoring.vpt import VolumePriceTrendScorer
from src.scoring.ma_alignment import MovingAverageAlignmentScorer
from src.scoring.roc import RateOfChangeScorer
from src.scoring.distance_52w_high import DistanceFromHighScorer
from src.scoring.bb_pct_b import BollingerPctBScorer
from src.scoring.decisive_body import DecisiveBodyScorer
from src.scoring.streak import ConsecutiveDaysStreakScorer
from src.scoring.volatility import NormalizedATRScorer
from src.scoring.BBW import BollingerBandWidthScorer
from src.scoring.Intraday_amplitude import IntradayAmplitudeScorer
from src.scoring.RVOL import RelativeVolumeScorer
from src.scoring.ADT import AverageDailyTurnoverScorer
from src.scoring.RSI import RSIScorer
from src.scoring.Average_Directional_index import AverageDirectionalIndexScorer
from src.scoring.percentage_deviation_from_EMA import PriceToEMAStretchScorer
from src.scoring.closing_tick import ClosingRangeScorer
# (You will also import Pranav's 8 modules here once he pushes them)

class MasterScreener:
    def __init__(self, db_path: str = "master_history.db"):
        self.db_path = db_path
        
        # Initialize the Scorers
        # We group them by category so we can assign macro-weights to the categories later
        self.scorers = {
            "volatility": [
                HistoricalVolatilityScorer(),
                GapRiskScorer(),
                NormalizedATRScorer(),
                BollingerBandWidthScorer(),
                IntradayAmplitudeScorer()
            ],
            "liquidity": [
                ChaikinMoneyFlowScorer(),
                VolumePriceTrendScorer(),
                RelativeVolumeScorer(),
                AverageDailyTurnoverScorer(),

            ],
            "momentum": [
                MovingAverageAlignmentScorer(),
                RateOfChangeScorer(),
                DistanceFromHighScorer(),
                RSIScorer(),
                AverageDirectionalIndexScorer()
            ],
            "exhaustion": [
                BollingerPctBScorer(),
                PriceToEMAStretchScorer()
            ],
            "microstructure": [
                DecisiveBodyScorer(),
                ConsecutiveDaysStreakScorer(),
                ClosingRangeScorer()
            ]
        }
        
        # Ensemble Weights (Must sum to 1.0)
        # Momentum and Liquidity drive the market. Microstructure is just for timing.
        self.category_weights = {
            "volatility": 0.20,
            "liquidity": 0.25,
            "momentum": 0.35,
            "exhaustion": 0.10,
            "microstructure": 0.10
        }

    def fetch_data(self, token: str) -> pd.DataFrame:
        """Fetches strictly 252 days of data to satisfy the 52-Week High module."""
        conn = sqlite3.connect(self.db_path)
        query = f"""
            SELECT timestamp, open, high, low, close, volume 
            FROM historical_data 
            WHERE token = '{token}' 
            ORDER BY timestamp ASC 
            LIMIT 252
        """
        df = pd.read_sql_query(query, conn)
        conn.close()
        
        # Convert types for Pandas C-engine
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
            
        return df

    def evaluate_stock(self, token: str, symbol: str) -> dict:
        df = self.fetch_data(token)
        
        # Minimum data guardrail for the orchestrator
        if len(df) < 252:
            return {"symbol": symbol, "master_score": 0.0, "status": "Insufficient Data"}

        category_scores = {}
        
        # 1. Calculate average score for each category
        for category, scorer_list in self.scorers.items():
            scores = [scorer.calculate(df) for scorer in scorer_list]
            category_scores[category] = sum(scores) / len(scores) if scores else 0.0
            
        # 2. Apply Ensemble Weighting
        master_score = 0.0
        for category, weight in self.category_weights.items():
            master_score += category_scores[category] * weight
            
        return {
            "symbol": symbol,
            "master_score": round(master_score, 4),
            "breakdown": category_scores
        }

    def run_screener(self, stock_list: List[Dict[str, str]], batch_size: int = 500) -> pd.DataFrame:
        """
        Processes the universe in chunks, forcing Python Garbage Collection 
        after every batch to maintain a flat memory profile on the server.
        """
        results = []
        total_stocks = len(stock_list)
        
        print(f"Initializing scan for {total_stocks} equities...")
        
        # Slicing the list into batches
        for i in range(0, total_stocks, batch_size):
            batch = stock_list[i:i + batch_size]
            print(f"Processing batch {i // batch_size + 1} ({len(batch)} stocks)...")
            
            for stock in batch:
                try:
                    score_data = self.evaluate_stock(stock['token'], stock['symbol'])
                    if score_data['master_score'] > 0:
                        results.append(score_data)
                except Exception as e:
                    print(f"Mathematical Fault on {stock['symbol']}: {e}")
                    continue
            
            # System Design Guardrail: Force memory deallocation of stale DataFrames
            gc.collect()
            
            # Prevent database I/O throttling
            time.sleep(0.5) 
                
        # Convert, sort, and return
        results_df = pd.DataFrame(results)
        if not results_df.empty:
            results_df = results_df.sort_values(by="master_score", ascending=False).reset_index(drop=True)
            
        return results_df
# import os
# import sys

# # Force Python to recognize the project root directory
# current_dir = os.path.dirname(os.path.abspath(__file__))
# project_root = os.path.abspath(os.path.join(current_dir, '../../'))
# if project_root not in sys.path:
#     sys.path.insert(0, project_root)

# # ... rest of your imports (json, time, etc.)
# import pyotp
# import requests
# import json
# import time
# from dotenv import load_dotenv
# from SmartApi import SmartConnect
# from datetime import datetime, timedelta
# from src.scripts.telegram_reporter import TelegramReporter 

# # Load environment variables
# current_dir = os.path.dirname(os.path.abspath(__file__))
# env_path = os.path.join(current_dir, "../../.env")
# load_dotenv(dotenv_path=env_path)

# #Database creation
# from src.scripts.ingester import MasterIngester

# class PreMarketScreener:
#     def __init__(self, top_n=50):
#         self.top_n = top_n
#         # Change this line inside __init__:
#         self.watchlist_path = os.path.join(current_dir, '../../target_ticks.json')
#         self.api = self._authenticate()

#     def _authenticate(self):
#         """Silently authenticates with Angel One using PyOTP."""
#         api_key = os.getenv("ANGEL_API_KEY")
#         client_id = os.getenv("ANGEL_CLIENT_ID")
#         pin = os.getenv("ANGEL_PIN")
#         totp_secret = os.getenv("ANGEL_TOTP_SECRET")

#         if not all([api_key, client_id, pin, totp_secret]):
#             raise ValueError("[FATAL] Missing Angel One credentials in .env file.")

#         try:
#             print(f"[SYSTEM] Authenticating Angel One API for Client: {client_id}...")
#             api = SmartConnect(api_key=api_key)
            
#             # Generate live TOTP token mathematically
#             totp = pyotp.TOTP(totp_secret).now()
            
#             # Establish Session
#             session = api.generateSession(client_id, pin, totp)
            
#             if session.get('status') is False:
#                 raise PermissionError(f"Authentication Rejected: {session.get('message')}")
            
#             print("[SUCCESS] Angel One Session Established.")
#             return api

#         except Exception as e:
#             print(f"[ERROR] API Authentication Failed: {e}")
#             exit(1)

#     def fetch_master_contract_list(self):
#         """Downloads and filters the daily instrument master list."""
#         print("[SYSTEM] Fetching master contract list from Angel One...")
#         url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        
#         try:
#             response = requests.get(url, timeout=10)
#             response.raise_for_status()
#             data = response.json()
            
#             # The Purge: Keep ONLY National Stock Exchange (NSE) Equities. 
#             # Reject derivatives, mutual funds, indices, and BSE garbage.
#             nse_equities = [
#                 item for item in data 
#                 if item.get('exch_seg') == 'NSE' 
#                 and item.get('symbol').endswith('-EQ')
#             ]
            
#             print(f"[SUCCESS] Downloaded {len(data)} total instruments.")
#             print(f"[SYSTEM] Liquidity Purge Complete. Surviving NSE Equities: {len(nse_equities)}")
#             self.surviving_equities = nse_equities
            
#         except Exception as e:
#             print(f"[ERROR] Failed to fetch or parse master contract list: {e}")
#             exit(1)

#     def generate_watchlist(self):
#         """Ranks symbols by normalized volatility and generates the JSON handoff file."""
#         print(f"\n[SYSTEM] Calculating Volatility for {len(self.surviving_equities)} Equities...")
        
#         scored_symbols = []
#         count = 0
#         # target_universe = self.surviving_equities[:10]
#         print(f"\n[TEST MODE] Running sanity check on {len(self.surviving_equities)} symbols...")
        
#         for equity in self.surviving_equities:
#             count += 1
#             token = equity['token']
#             symbol = equity['symbol']
            
#             # Print progress every 100 symbols so you know the engine hasn't frozen
#             if count % 100 == 0:
#                 print(f"[{count}/{len(self.surviving_equities)}] Scoring in progress...")
                
#             volatility_pct = self.get_normalized_atr(token, symbol)
#         # for count, equity in enumerate(target_universe, start=1):
#         #     token = equity['token']
#         #     symbol = equity['symbol']
            
#         #     print(f"[{count}/{len(target_universe)}] Scoring {symbol}...")
            
#         #     volatility_pct = self.get_normalized_atr(token, symbol)
#             if volatility_pct > 0:
#                 scored_symbols.append({
#                     "symbol": symbol,
#                     "token": token,
#                     "volatility_pct": round(volatility_pct, 2)
#                 })
                
#         # The Final Sort: Descending order by volatility percentage
#         scored_symbols.sort(key=lambda x: x['volatility_pct'], reverse=True)
        
#         # Slice the Top N
#         final_watchlist = scored_symbols[:self.top_n]
        
#         # The JSON Handoff
#         with open(self.watchlist_path, 'w') as f:
#             json.dump(final_watchlist, f, indent=4)
            
#         print(f"\n[SUCCESS] Screener Complete. Top {self.top_n} hyper-active assets written to watchlist.json.")

#         #Telegram sender
#         try:
#             print("[SYSTEM] Executing Telegram pre-market dispatch...")
#             # Import dynamically to avoid circular dependencies
#             reporter = TelegramReporter()
#             reporter.send_premarket_watchlist(self.watchlist_path)
#         except Exception as e:
#             print(f"[ERROR] Telegram handoff crashed: {e}")
#     def get_normalized_atr(self, token: str, symbol: str) -> float:
#         """
#         Fetches daily candles, calculates 14-day ATR, and normalizes it as a percentage of LTP.
#         Includes a fortified 0.45s sleep (2.2 req/sec limit) and an exponential backoff for HTTP 429s.
#         """
#         to_date = datetime.now().strftime('%Y-%m-%d %H:%M')
#         from_date = (datetime.now() - timedelta(days=25)).strftime('%Y-%m-%d %H:%M')
        
#         payload = {
#             "exchange": "NSE",
#             "symboltoken": token,
#             "interval": "ONE_DAY",
#             "fromdate": from_date, 
#             "todate": to_date
#         }

#         max_retries = 2
#         for attempt in range(max_retries):
#             try:
#                 response = self.api.getCandleData(payload)
                
#                 # THE FORTRESS PROTOCOL: 450ms hard stop guarantees we never breach 2.2 req/sec
#                 time.sleep(0.45)  
                
#                 # If we get throttled or rate-limited by the broker's WAF
#                 if response and not response.get('status'):
#                     error_msg = response.get('message', '').lower()
#                     if 'limit' in error_msg or 'throttle' in error_msg:
#                         print(f"[WARNING] API Rate Limit hit on {symbol}. Backing off for 5 seconds (Attempt {attempt+1}/{max_retries})...")
#                         time.sleep(5.0)
#                         continue # Retry
                
#                 data = response.get('data') if response else None
                
#                 # If valid data is returned, process the math and break the retry loop
#                 if data and len(data) >= 15:
#                     true_ranges = []
#                     for i in range(1, len(data)):
#                         high = data[i][2]
#                         low = data[i][3]
#                         prev_close = data[i-1][4]
                        
#                         tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
#                         true_ranges.append(tr)
                        
#                     atr = sum(true_ranges[-14:]) / 14
#                     latest_close = data[-1][4]
                    
#                     return (atr / latest_close) * 100 if latest_close > 0 else 0.0
                
#                 return 0.0 # Return 0 if not enough historical data exists
                
#             except Exception as e:
#                 print(f"[API ERROR] Failed to fetch data for {symbol}: {e}")
#                 time.sleep(1.0) # Penalty sleep on hard errors before moving to next symbol
#                 return 0.0
                
#         return 0.0

    
# # if __name__ == "__main__":
# #     print("\n[SYSTEM] Booting Pre-Market Volatility Screener...")
    
# #     # 1. Initialize the Screener (Automatically authenticates via TOTP)
# #     screener = PreMarketScreener(top_n=50)
    
# #     # 2. Download the universe and purge illiquid garbage
# #     # (This must save the 2,678 surviving assets to self.surviving_equities)
# #     screener.fetch_master_contract_list()
    
# #     # 3. Calculate 14-day ATR, rank by volatility, and export to JSON
# #     screener.generate_watchlist()

# if __name__ == "__main__":
#     print("\n[SYSTEM] Booting Pre-Market Engine...")
    
#     # 1. Initialize the Screener (Authenticates via TOTP)
#     screener = PreMarketScreener(top_n=50)
    
#     # 2. Download the universe and purge illiquid garbage
#     screener.fetch_master_contract_list()
    
#     # =================================================================
#     # PHASE 1: THE MASTER INGESTION
#     # =================================================================
    
#     # Force the database into the project root, not deep inside the scripts folder
#     db_path = os.path.join(project_root, 'master_history.db')
    
#     # Pass the authenticated Angel One API client and fire the sync
#     ingester = MasterIngester(api_client=screener.api, db_path=db_path)
#     ingester.sync_database(screener.surviving_equities)
#     #test run
#     test_universe = screener.surviving_equities[:10]
#     ingester.sync_database(test_universe)
#     # =================================================================
#     # PHASE 2 & 3: ENSEMBLE SCORING (Temporarily Disabled)
#     # =================================================================
#     # We are bypassing the math until the SQLite vault successfully builds.
#     # screener.generate_watchlist()