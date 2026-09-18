import os
import sys
import json
import time
import sqlite3
import gc
import pyotp
import requests
import pandas as pd
from typing import Dict, List
from dotenv import load_dotenv
from SmartApi import SmartConnect
from datetime import datetime

# Force Python to recognize the project root directory (Assumes file is in src/scripts or similar)
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, '../../'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Load environment variables
env_path = os.path.join(project_root, ".env")
load_dotenv(dotenv_path=env_path)

# Import Scoring Modules
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

# Import External Handlers
from src.scripts.ingester import MasterIngester
from src.scripts.telegram_reporter import TelegramReporter 


class MasterScreener:
    def __init__(self, db_path: str):
        self.db_path = db_path
        
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
        
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)
            
        return df

    def evaluate_stock(self, token: str, symbol: str) -> dict:
        df = self.fetch_data(token)
        
        if len(df) < 252:
            return {"symbol": symbol, "token": token, "master_score": 0.0, "status": "Insufficient Data"}

        category_scores = {}
        for category, scorer_list in self.scorers.items():
            scores = [scorer.calculate(df) for scorer in scorer_list]
            category_scores[category] = sum(scores) / len(scores) if scores else 0.0
            
        master_score = sum(category_scores[cat] * weight for cat, weight in self.category_weights.items())
            
        return {
            "symbol": symbol,
            "token": token,
            "master_score": round(master_score, 4),
            "breakdown": category_scores
        }

    def run_screener(self, stock_list: List[Dict[str, str]], batch_size: int = 500) -> pd.DataFrame:
        results = []
        total_stocks = len(stock_list)
        
        print(f"[PHASE 2] Initializing Ensemble Scan for {total_stocks} equities...")
        
        for i in range(0, total_stocks, batch_size):
            batch = stock_list[i:i + batch_size]
            print(f"--> Processing batch {i // batch_size + 1} ({len(batch)} stocks)...")
            
            for stock in batch:
                try:
                    score_data = self.evaluate_stock(stock['token'], stock['symbol'])
                    if score_data['master_score'] > 0:
                        results.append(score_data)
                except Exception as e:
                    print(f"Mathematical Fault on {stock['symbol']}: {e}")
                    continue
            
            gc.collect()
            time.sleep(0.5) 
                
        results_df = pd.DataFrame(results)
        if not results_df.empty:
            results_df = results_df.sort_values(by="master_score", ascending=False).reset_index(drop=True)
            
        return results_df


class PreMarketOrchestrator:
    def __init__(self, top_n=50):
        self.top_n = top_n
        self.db_path = os.path.join(project_root, 'master_history.db')
        self.watchlist_path = os.path.join(project_root, 'target_ticks.json')
        self.csv_path = os.path.join(project_root, 'live_screener_rankings.csv')
        self.surviving_equities = []
        
        self.api = self._authenticate()

    def _authenticate(self):
        """Silently authenticates with Angel One using PyOTP."""
        api_key = os.getenv("ANGEL_API_KEY")
        client_id = os.getenv("ANGEL_CLIENT_ID")
        pin = os.getenv("ANGEL_PIN")
        totp_secret = os.getenv("ANGEL_TOTP_SECRET")

        if not all([api_key, client_id, pin, totp_secret]):
            raise ValueError("[FATAL] Missing Angel One credentials in .env file.")

        try:
            print(f"[SYSTEM] Authenticating Angel One API for Client: {client_id}...")
            api = SmartConnect(api_key=api_key)
            totp = pyotp.TOTP(totp_secret).now()
            session = api.generateSession(client_id, pin, totp)
            
            if session.get('status') is False:
                raise PermissionError(f"Authentication Rejected: {session.get('message')}")
            
            print("[SUCCESS] Angel One Session Established.")
            return api

        except Exception as e:
            print(f"[ERROR] API Authentication Failed: {e}")
            exit(1)

    def fetch_master_contract_list(self):
        """Downloads and filters the daily instrument master list."""
        print("[SYSTEM] Fetching master contract list from Angel One...")
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
            
            self.surviving_equities = [
                item for item in data 
                if item.get('exch_seg') == 'NSE' 
                and item.get('symbol').endswith('-EQ')
            ]
            
            print(f"[SUCCESS] Downloaded {len(data)} total instruments.")
            print(f"[SYSTEM] Liquidity Purge Complete. Surviving NSE Equities: {len(self.surviving_equities)}")
            
        except Exception as e:
            print(f"[ERROR] Failed to fetch or parse master contract list: {e}")
            exit(1)

    def execute_daily_pipeline(self):
        # 1. Fetch live universe
        self.fetch_master_contract_list()
        
        # 2. Sync SQLite Database
        print("\n[PHASE 1] Initializing Master Database Ingestion...")
        ingester = MasterIngester(api_client=self.api, db_path=self.db_path)
        ingester.sync_database(self.surviving_equities)
        
        # 3. Run Ensemble Scoring
        screener = MasterScreener(db_path=self.db_path)
        results_df = screener.run_screener(self.surviving_equities, batch_size=500)
        
        if results_df.empty:
            print("[FATAL] Database returned no valid data or guardrails blocked all stocks.")
            return
            
        # 4. Generate CSV
        print(f"\n[PHASE 3] Generating Output Artifacts...")
        results_df.to_csv(self.csv_path, index=False)
        print(f"--> [SUCCESS] Full universe rankings saved to CSV: {self.csv_path}")
        
        # 5. Generate JSON Handoff (Overwrites daily for execution engine)
        top_stocks = results_df.head(self.top_n)
        final_watchlist = []
        for _, row in top_stocks.iterrows():
            final_watchlist.append({
                "symbol": row['symbol'],
                "token": row['token'],
                "master_score": row['master_score']
            })
            
        with open(self.watchlist_path, 'w') as f:
            json.dump(final_watchlist, f, indent=4)
        print(f"--> [SUCCESS] Top {self.top_n} targets written to JSON: {self.watchlist_path}")
        
        # 6. Telegram Handoff
        try:
            print("--> [SYSTEM] Executing Telegram pre-market dispatch...")
            reporter = TelegramReporter()
            reporter.send_premarket_watchlist(self.watchlist_path)
            print("--> [SUCCESS] Telegram dispatch complete.")
        except Exception as e:
            print(f"--> [ERROR] Telegram handoff crashed: {e}")


if __name__ == "__main__":
    print("\n================================================")
    print("   BOOTING PRE-MARKET ENSEMBLE ORCHESTRATOR")
    print("================================================\n")
    
    orchestrator = PreMarketOrchestrator(top_n=50)
    orchestrator.execute_daily_pipeline()