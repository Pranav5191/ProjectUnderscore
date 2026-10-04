"""
═══════════════════════════════════════════════════════════════
  PRE-MARKET ENSEMBLE SCREENER
═══════════════════════════════════════════════════════════════

Daily pipeline:
  1. Authenticate with Angel One API
  2. Download/sync OHLCV history for all NSE equities → SQLite
  3. Pre-filter: remove penny stocks, illiquid stocks
  4. Score each surviving stock with 19-scorer weighted ensemble
  5. Rank → write CSV (full universe) + JSON (top N for trading)
  6. Dispatch to Telegram

Output flow:
  screener.py → target_ticks.json → update_symbols.py → symbols.json → WebSocket/Redis
"""

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
from datetime import datetime, timedelta

# Force Python to recognize the project root directory
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
        """
        Fetches the LATEST 252 trading days for a stock.

        CRITICAL FIX: Previous version used ORDER BY timestamp ASC LIMIT 252,
        which returned the OLDEST 252 days. New data was never seen, which is
        why the screener gave the same list every day.

        Now uses ORDER BY DESC to get the most recent data, then reverses
        to chronological order for the scoring modules.
        """
        conn = sqlite3.connect(self.db_path)
        query = """
            SELECT timestamp, open, high, low, close, volume 
            FROM historical_data 
            WHERE token = ? 
            ORDER BY timestamp DESC 
            LIMIT 252
        """
        df = pd.read_sql_query(query, conn, params=(token,))
        conn.close()

        if df.empty:
            return df

        # Reverse from newest-first → chronological (oldest-first) as scorers expect
        df = df.iloc[::-1].reset_index(drop=True)

        df['timestamp'] = pd.to_datetime(df['timestamp'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)

        return df

    def pre_filter(self, token: str) -> dict:
        """
        Quick pre-filter check BEFORE running the expensive 19-scorer ensemble.
        Returns a dict with filter results, or None if the stock should be skipped.

        Filters:
          - Minimum 100 days of data (relaxed from 252 to include more stocks)
          - Price >= Rs.10 (no penny stocks — untradeable spreads)
          - Average daily turnover >= Rs.50 lakh (minimum liquidity for intraday)
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get the most recent 20 days for quick checks
        cursor.execute("""
            SELECT close, volume FROM historical_data 
            WHERE token = ? ORDER BY timestamp DESC LIMIT 20
        """, (token,))
        rows = cursor.fetchall()
        conn.close()

        if len(rows) < 20:
            return None

        latest_price = float(rows[0][0])
        avg_volume = sum(float(r[1]) for r in rows) / len(rows)
        avg_turnover = latest_price * avg_volume

        # Hard filters for intraday tradability
        if latest_price < 10.0:
            return None   # Penny stocks have spreads wider than their price movement
        if avg_turnover < 5_000_000:
            return None   # Less than Rs.50 lakh/day = can't enter/exit cleanly

        return {
            "price": latest_price,
            "avg_volume": avg_volume,
            "avg_turnover": avg_turnover
        }

    def evaluate_stock(self, token: str, symbol: str) -> dict:
        """Runs the full 19-scorer ensemble on a single stock."""
        df = self.fetch_data(token)

        if len(df) < 100:
            return {"symbol": symbol, "token": token, "master_score": 0.0, "status": "Insufficient Data"}

        output = {
            "symbol": symbol,
            "token": token
        }

        category_scores = {}

        for category, scorer_list in self.scorers.items():
            scores = []
            for scorer in scorer_list:
                try:
                    val = scorer.calculate(df)
                    scores.append(val)
                    output[scorer.__class__.__name__] = round(val, 4)
                except Exception as e:
                    # Don't let one broken scorer kill the entire stock
                    scores.append(0.5)  # Neutral fallback
                    output[scorer.__class__.__name__] = 0.5

            cat_avg = sum(scores) / len(scores) if scores else 0.0
            category_scores[category] = cat_avg
            output[f"CAT_{category.upper()}"] = round(cat_avg, 4)

        # Apply Ensemble Weighting
        master_score = sum(category_scores[cat] * weight for cat, weight in self.category_weights.items())
        output["master_score"] = round(master_score, 4)

        return output

    def run_screener(self, stock_list: List[Dict[str, str]], batch_size: int = 500) -> pd.DataFrame:
        """Scores all stocks and returns a ranked DataFrame."""
        results = []
        total_stocks = len(stock_list)
        filtered_out = 0
        evaluated = 0

        print(f"[PHASE 2] Initializing Ensemble Scan for {total_stocks} equities...")

        for i in range(0, total_stocks, batch_size):
            batch = stock_list[i:i + batch_size]
            print(f"--> Processing batch {i // batch_size + 1} ({len(batch)} stocks)...")

            for stock in batch:
                try:
                    # Pre-filter: skip penny stocks and illiquid names
                    filter_result = self.pre_filter(stock['token'])
                    if filter_result is None:
                        filtered_out += 1
                        continue

                    score_data = self.evaluate_stock(stock['token'], stock['symbol'])
                    if score_data.get('master_score', 0) > 0:
                        # Attach price/turnover for reference
                        score_data['latest_price'] = round(filter_result['price'], 2)
                        score_data['avg_daily_turnover'] = round(filter_result['avg_turnover'], 0)
                        results.append(score_data)
                        evaluated += 1
                except Exception as e:
                    print(f"Mathematical Fault on {stock['symbol']}: {e}")
                    continue

            gc.collect()
            time.sleep(0.5)

        print(f"[PHASE 2] Pre-filter removed {filtered_out} stocks (penny/illiquid)")
        print(f"[PHASE 2] Evaluated {evaluated} stocks with full ensemble")

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
                and item.get('symbol', '').endswith('-EQ')
            ]

            print(f"[SUCCESS] Downloaded {len(data)} total instruments.")
            print(f"[SYSTEM] Liquidity Purge Complete. Surviving NSE Equities: {len(self.surviving_equities)}")

        except Exception as e:
            print(f"[ERROR] Failed to fetch or parse master contract list: {e}")
            exit(1)

    def validate_database(self):
        """Checks the database has recent data before scoring."""
        if not os.path.exists(self.db_path):
            print("[FATAL] master_history.db not found. Run ingester first.")
            return False

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT MAX(timestamp), COUNT(*), COUNT(DISTINCT token) FROM historical_data")
            latest, total_rows, total_tokens = cursor.fetchone()

            if total_rows == 0:
                print("[FATAL] Database is empty. Ingester has not run yet.")
                return False

            print(f"[DB CHECK] {total_rows:,} rows | {total_tokens} tokens | Latest: {latest}")

            # Warn if data is stale (> 3 days old)
            if latest:
                latest_dt = datetime.strptime(str(latest)[:10], '%Y-%m-%d')
                days_old = (datetime.now() - latest_dt).days
                if days_old > 3:
                    print(f"[WARNING] Database is {days_old} days stale! Latest data: {latest}")
                    print(f"[WARNING] Scores may not reflect current market conditions.")
                else:
                    print(f"[DB CHECK] Data is {days_old} day(s) old. OK.")

            return True

        except Exception as e:
            print(f"[ERROR] Database validation failed: {e}")
            return False
        finally:
            conn.close()

    def execute_daily_pipeline(self):
        # 1. Fetch live universe
        self.fetch_master_contract_list()

        # 2. Sync SQLite Database
        print("\n[PHASE 1] Initializing Master Database Ingestion...")
        ingester = MasterIngester(api_client=self.api, db_path=self.db_path)
        ingester.sync_database(self.surviving_equities)

        # 2b. Validate database has usable data
        if not self.validate_database():
            print("[FATAL] Cannot proceed with empty or corrupt database.")
            return

        # 3. Run Ensemble Scoring
        screener = MasterScreener(db_path=self.db_path)
        results_df = screener.run_screener(self.surviving_equities, batch_size=500)

        if results_df.empty:
            print("[FATAL] Screener returned no valid results. Check DB and guardrails.")
            print("[FATAL] NOT overwriting target_ticks.json to preserve yesterday's picks.")
            return

        # 4. Generate CSV
        print(f"\n[PHASE 3] Generating Output Artifacts...")
        results_df.to_csv(self.csv_path, index=False)
        print(f"--> [SUCCESS] Full universe rankings saved: {self.csv_path}")
        print(f"--> Top 5 by master_score:")
        for _, row in results_df.head(5).iterrows():
            print(f"    {row['symbol']:>20} | score: {row['master_score']:.4f} | price: Rs.{row.get('latest_price', '?')}")

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
        print(f"--> [SUCCESS] Top {self.top_n} targets written: {self.watchlist_path}")

        # 6. Telegram Handoff
        try:
            print("--> [SYSTEM] Executing Telegram pre-market dispatch...")
            reporter = TelegramReporter()
            reporter.send_file(self.csv_path)
            reporter.send_premarket_watchlist(self.watchlist_path)
            print("--> [SUCCESS] Telegram dispatch complete.")
        except Exception as e:
            print(f"--> [ERROR] Telegram handoff crashed: {e}")

        print(f"\n{'='*55}")
        print(f"  SCREENER COMPLETE — {len(final_watchlist)} stocks selected")
        print(f"  Next step: update_symbols.py picks top 12 → symbols.json")
        print(f"{'='*55}\n")


if __name__ == "__main__":
    print("\n================================================")
    print("   BOOTING PRE-MARKET ENSEMBLE ORCHESTRATOR")
    print("================================================\n")

    orchestrator = PreMarketOrchestrator(top_n=50)
    orchestrator.execute_daily_pipeline()