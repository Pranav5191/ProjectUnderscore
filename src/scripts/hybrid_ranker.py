"""
src/scripts/hybrid_ranker.py
Mathematical + AI Blending (Top 15) & Pure AI Discovery Universe (Top 20).
Includes:
  - Intensity Deadband Gate (< 0.30 -> Neutral 0.50)
  - Bearish Distribution Veto (Protects Long setups from severe negative news)
  - SQLite Penny Stock (< Rs. 10), Turnover (< Rs. 50L), and Circuit-Lock Filter
"""

import os
import json
import sqlite3
import logging
import requests
import pandas as pd
from typing import Dict, List, Optional

logger = logging.getLogger("HybridRanker")


class HybridRanker:
    def __init__(
        self,
        db_path: str,
        csv_rankings_path: str,
        w_math: float = 0.70,
        w_ai: float = 0.30,
        min_price: float = 10.0,
        min_avg_turnover: float = 5_000_000.0
    ):
        if round(w_math + w_ai, 4) != 1.0:
            raise ValueError(f"Weights must sum to 1.0 (got w_math={w_math}, w_ai={w_ai})")
        self.db_path = db_path
        self.csv_rankings_path = csv_rankings_path
        self.w_math = w_math
        self.w_ai = w_ai
        self.min_price = min_price
        self.min_avg_turnover = min_avg_turnover

        self.symbol_to_token: Dict[str, str] = {}
        self.symbol_to_math_score: Dict[str, float] = {}
        self.symbol_to_context: Dict[str, str] = {}
        self._load_symbol_metadata()

    def _load_symbol_metadata(self):
        """Loads symbol->token, master_score, and price/turnover context from live_screener_rankings.csv."""
        if os.path.exists(self.csv_rankings_path) and os.path.getsize(self.csv_rankings_path) > 0:
            try:
                df = pd.read_csv(self.csv_rankings_path)
                for _, row in df.iterrows():
                    sym = str(row["symbol"]).strip().upper()
                    tok = str(row["token"]).strip()
                    score = float(row.get("master_score", 0.0))
                    self.symbol_to_token[sym] = tok
                    self.symbol_to_math_score[sym] = round(score, 4)

                    price = row.get("latest_price")
                    turnover = row.get("avg_daily_turnover")
                    if pd.notna(price) and pd.notna(turnover):
                        t_cr = round(float(turnover) / 1e7, 2)
                        self.symbol_to_context[sym] = f"Price: Rs.{price}, Avg Daily Turnover: Rs.{t_cr} Cr"
            except Exception as e:
                logger.warning(f"Could not load {self.csv_rankings_path}: {e}")

    def resolve_token_from_angel_master(self, missing_symbols: List[str]) -> None:
        if not missing_symbols:
            return
        try:
            url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                for item in resp.json():
                    if item.get("exch_seg") == "NSE" and item.get("symbol", "").endswith("-EQ"):
                        sym = item["symbol"].strip().upper()
                        if sym not in self.symbol_to_token:
                            self.symbol_to_token[sym] = str(item["token"]).strip()
        except Exception as e:
            logger.warning(f"Angel ScripMaster fallback token lookup failed: {e}")

    def check_sqlite_tradability(self, token: str) -> Optional[Dict[str, float]]:
        """
        Verifies via master_history.db:
          1. Minimum 20 trading days of history
          2. Latest closing price >= Rs. 10.0 (no penny stocks)
          3. 20-day Average Daily Turnover >= Rs. 50 Lakh
          4. Not circuit-locked (rejects stocks where >=3 of last 5 days had High == Low)
        """
        if not token or not os.path.exists(self.db_path):
            return None

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT high, low, close, volume FROM historical_data
                WHERE token = ?
                ORDER BY timestamp DESC
                LIMIT 20
                """,
                (str(token),)
            )
            rows = cursor.fetchall()
            if len(rows) < 20:
                return None

            latest_price = float(rows[0][2])
            avg_volume = sum(float(r[3]) for r in rows) / len(rows)
            avg_turnover = latest_price * avg_volume

            if latest_price < self.min_price or avg_turnover < self.min_avg_turnover:
                return None

            # Check last 5 candles for circuit lock (High == Low means locked in upper/lower circuit all day)
            circuit_locked_days = sum(1 for r in rows[:5] if float(r[0]) == float(r[1]))
            if circuit_locked_days >= 3:
                logger.info(f"Rejected token {token}: circuit-locked on {circuit_locked_days}/5 recent sessions.")
                return None

            return {
                "latest_price": round(latest_price, 2),
                "avg_turnover": round(avg_turnover, 0)
            }
        except Exception as e:
            logger.warning(f"SQLite tradability check error for token {token}: {e}")
            return None
        finally:
            conn.close()

    def rank_hybrid_top_15(
        self, math_top_50: List[Dict], ai_evaluations: Dict[str, Dict]
    ) -> List[Dict]:
        """
        Merges master_score and ai_score with Intensity Deadband Gate (<0.30)
        and Bearish Veto protection.
        """
        blended_list = []
        for item in math_top_50:
            sym = str(item["symbol"]).strip().upper()
            token = str(item["token"]).strip()
            master_score = float(item.get("master_score", 0.0))

            ai_eval = ai_evaluations.get(sym, {})
            raw_ai_score = float(ai_eval.get("ai_score", 0.50))
            intensity = float(ai_eval.get("catalyst_intensity", 0.0))
            bias = str(ai_eval.get("sentiment_bias", "NEUTRAL")).upper()

            # 1. Intensity Deadband Gate: Prevent low-intensity noise (<0.30) from altering math rank
            if intensity < 0.30:
                effective_ai_score = 0.50
            else:
                effective_ai_score = raw_ai_score

            # 2. Bearish Distribution Veto: Demote stocks with high-intensity negative catalysts
            if bias == "BEARISH" and raw_ai_score <= 0.30 and intensity >= 0.60:
                final_score = round(master_score * 0.40, 4)
                direction_bias = "SHORT"
            else:
                final_score = round((self.w_math * master_score) + (self.w_ai * effective_ai_score), 4)
                direction_bias = "LONG" if effective_ai_score >= 0.55 else ("SHORT" if effective_ai_score <= 0.35 else "NEUTRAL")

            blended_list.append({
                "symbol": sym,
                "token": token,
                "master_score": round(master_score, 4),
                "ai_score": round(effective_ai_score, 4),
                "final_score": final_score,
                "catalyst_intensity": round(intensity, 4),
                "sentiment_bias": bias,
                "direction_bias": direction_bias,
                "reasoning": ai_eval.get("reasoning", "Pure mathematical momentum baseline."),
                "cohort": "HYBRID_MATH_AI"
            })

        blended_list.sort(key=lambda x: (x["final_score"], x["master_score"]), reverse=True)
        return blended_list[:15]

    def rank_pure_ai_top_20(
        self, broad_ai_evaluations: Dict[str, Dict], output_json_path: str
    ) -> List[Dict]:
        """
        Ranks broad-market catalyst stocks purely by `catalyst_intensity`,
        applies SQLite tradability & circuit filters, and writes top 20 to `ai_catalyst_ticks.json`.
        """
        missing_syms = [s for s in broad_ai_evaluations if s not in self.symbol_to_token]
        if missing_syms:
            self.resolve_token_from_angel_master(missing_syms)

        valid_outliers = []
        for sym, ai_eval in broad_ai_evaluations.items():
            intensity = float(ai_eval.get("catalyst_intensity", 0.0))
            ai_score = float(ai_eval.get("ai_score", 0.50))
            bias = str(ai_eval.get("sentiment_bias", "NEUTRAL")).upper()

            if intensity < 0.30:
                continue

            token = self.symbol_to_token.get(sym)
            if not token:
                continue

            tradability = self.check_sqlite_tradability(token)
            if tradability is None:
                continue

            master_score = self.symbol_to_math_score.get(sym, 0.0)
            direction_bias = "LONG" if bias == "BULLISH" else ("SHORT" if bias == "BEARISH" else "NEUTRAL")

            valid_outliers.append({
                "symbol": sym,
                "token": str(token),
                "master_score": round(master_score, 4),
                "ai_score": round(ai_score, 4),
                "final_score": round(ai_score, 4),
                "catalyst_intensity": round(intensity, 4),
                "sentiment_bias": bias,
                "direction_bias": direction_bias,
                "reasoning": ai_eval.get("reasoning", ""),
                "latest_price": tradability["latest_price"],
                "cohort": "PURE_AI_CATALYST"
            })

        valid_outliers.sort(key=lambda x: (x["catalyst_intensity"], abs(x["ai_score"] - 0.50)), reverse=True)
        top_20_outliers = valid_outliers[:20]

        with open(output_json_path, "w") as f:
            json.dump(top_20_outliers, f, indent=4)

        logger.info(f"Saved {len(top_20_outliers)} Pure AI Catalyst stocks to {output_json_path}")
        return top_20_outliers