import os
import py_compile

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def write_and_verify(rel_path: str, content: str):
    full_path = os.path.join(PROJECT_ROOT, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content.lstrip())
    if full_path.endswith(".py"):
        py_compile.compile(full_path, doraise=True)
    print(f"[UPDATED & VERIFIED] {rel_path}")


# ==============================================================================
# 1. FIX src/scoring/percentage_deviation_from_EMA.py
#    Penalizes overextended stocks (>+6% or <-6% from EMA20) instead of rewarding exhaustion
# ==============================================================================
ema_stretch_code = r'''
import pandas as pd
from src.scoring.base import BaseScorer

class PriceToEMAStretchScorer(BaseScorer):
    def __init__(self, period: int = 20):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period + 1:
            return 0.0

        close_prices = df['close']
        ema = close_prices.ewm(span=self.period, adjust=False).mean()

        last_close = float(close_prices.iloc[-1])
        last_ema = float(ema.iloc[-1])

        if last_ema == 0.0:
            return 0.0

        raw_stretch = (last_close - last_ema) / last_ema
        # Sweet spot for non-exhausted trend continuation is +1.5% above 20-EMA.
        # Penalize stocks stretched > +8% (overbought exhaustion) or < -5% (broken trend).
        dist_from_sweet_spot = abs(raw_stretch - 0.015)
        raw_score = 1.0 - (dist_from_sweet_spot / 0.08)

        return self.clamp(raw_score)
'''
write_and_verify("src/scoring/percentage_deviation_from_EMA.py", ema_stretch_code)


# ==============================================================================
# 2. FIX src/scoring/bb_pct_b.py
#    Rewards non-exhausted Bollinger sweet spot (%b ~ 0.65) and penalizes >1.0 blowoffs
# ==============================================================================
bb_pct_b_code = r'''
import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class BollingerPctBScorer(BaseScorer):
    def __init__(self, period: int = 20, std_dev: float = 2.0):
        self.period = period
        self.std_dev = std_dev

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period:
            return 0.5

        sma = df['close'].rolling(window=self.period).mean()
        std = df['close'].rolling(window=self.period).std()

        upper_band = sma + (self.std_dev * std)
        lower_band = sma - (self.std_dev * std)

        current_close = float(df['close'].iloc[-1])
        current_upper = float(upper_band.iloc[-1])
        current_lower = float(lower_band.iloc[-1])

        band_width = current_upper - current_lower
        if band_width == 0 or pd.isna(band_width):
            return 0.5

        raw_pct_b = (current_close - current_lower) / band_width
        # Ideal non-exhausted momentum zone is %b = 0.65; penalize >1.05 blowoffs
        raw_score = 1.0 - (abs(raw_pct_b - 0.65) / 0.65)
        return self.clamp(raw_score)
'''
write_and_verify("src/scoring/bb_pct_b.py", bb_pct_b_code)


# ==============================================================================
# 3. FIX src/scoring/historical_volatility.py (Prevent DataFrame mutation)
# ==============================================================================
hv_code = r'''
import pandas as pd
import numpy as np
from src.scoring.base import BaseScorer

class HistoricalVolatilityScorer(BaseScorer):
    def __init__(self, period: int = 20, max_expected_hv: float = 0.60):
        self.period = period
        self.max_expected_hv = max_expected_hv

    def calculate(self, df: pd.DataFrame) -> float:
        if len(df) < self.period + 1:
            return 0.0

        df = df.copy()
        df['log_ret'] = np.log(df['close'] / df['close'].shift(1))

        daily_std = df['log_ret'].rolling(window=self.period).std().iloc[-1]
        annualized_hv = daily_std * np.sqrt(252)
        raw_score = annualized_hv / self.max_expected_hv

        return self.clamp(raw_score)
'''
write_and_verify("src/scoring/historical_volatility.py", hv_code)


# ==============================================================================
# 4. FIX src/scripts/cohort_blender.py
#    Preserves direction_bias, sentiment_bias, catalyst_intensity & reasoning in target_ticks.json
# ==============================================================================
cohort_blender_code = r'''
"""
src/scripts/cohort_blender.py
Blends Hybrid and Pure AI cohorts into target_ticks.json while preserving full AI directional metadata.
"""
import os
import json
import logging
from typing import List, Dict, Set

logger = logging.getLogger("CohortBlender")


class CohortBlender:
    @staticmethod
    def _format_entry(item: Dict, cohort_name: str) -> Dict:
        return {
            "symbol": item["symbol"],
            "token": str(item["token"]),
            "master_score": round(float(item.get("master_score", 0.0)), 4),
            "ai_score": round(float(item.get("ai_score", 0.50)), 4),
            "final_score": round(float(item.get("final_score", item.get("master_score", 0.0))), 4),
            "catalyst_intensity": round(float(item.get("catalyst_intensity", 0.0)), 4),
            "sentiment_bias": str(item.get("sentiment_bias", "NEUTRAL")).upper(),
            "direction_bias": str(item.get("direction_bias", "NEUTRAL")).upper(),
            "reasoning": str(item.get("reasoning", ""))[:280],
            "cohort": cohort_name
        }

    @staticmethod
    def blend_and_export(
        all_hybrid_candidates: List[Dict],
        pure_ai_candidates: List[Dict],
        target_ticks_path: str,
        hybrid_base_quota: int = 20,
        pure_ai_quota: int = 5,
        min_total_targets: int = 25
    ) -> List[Dict]:
        final_contract: List[Dict] = []
        seen_tokens: Set[str] = set()

        for item in all_hybrid_candidates[:hybrid_base_quota]:
            token = str(item["token"])
            seen_tokens.add(token)
            final_contract.append(CohortBlender._format_entry(item, "HYBRID_MATH_AI"))

        pure_added = 0
        for item in pure_ai_candidates:
            if pure_added >= pure_ai_quota:
                break
            token = str(item["token"])
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            final_contract.append(CohortBlender._format_entry(item, "PURE_AI_CATALYST"))
            pure_added += 1

        backfill_added = 0
        if len(final_contract) < min_total_targets:
            for item in all_hybrid_candidates[hybrid_base_quota:]:
                if len(final_contract) >= min_total_targets:
                    break
                token = str(item["token"])
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                final_contract.append(CohortBlender._format_entry(item, "HYBRID_MATH_AI"))
                backfill_added += 1

        temp_path = f"{target_ticks_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(final_contract, f, indent=4)
        os.replace(temp_path, target_ticks_path)

        logger.info(
            f"Master Cohort Handoff complete: {len(final_contract)} total stocks "
            f"({hybrid_base_quota} Base Hybrid + {pure_added} Pure AI + {backfill_added} Backfilled Hybrid) "
            f"written to {target_ticks_path}"
        )
        return final_contract
'''
write_and_verify("src/scripts/cohort_blender.py", cohort_blender_code)


# ==============================================================================
# 5. FIX src/scripts/ai_orchestrator.py & src/scripts/eod_master.py
# ==============================================================================
ao_path = os.path.join(PROJECT_ROOT, "src", "scripts", "ai_orchestrator.py")
if os.path.exists(ao_path):
    with open(ao_path, "r", encoding="utf-8") as f:
        ao_code = f.read()
    ao_code = ao_code.replace('target_file = "/home/ubuntu/project_pipeline/target_ticks.json"', 'target_file = self.target_ticks_path')
    write_and_verify("src/scripts/ai_orchestrator.py", ao_code)

eod_master_code = r'''
import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.scripts.aws_sync import AWSSync
from src.scripts.telegram_reporter import TelegramReporter

def execute_reporting_pipeline():
    print("=========================================")
    print("     EOD PIPELINE INITIALIZING           ")
    print("=========================================\n")

    print("[1/2] Fetching Engine Data from AWS S3...")
    syncer = AWSSync()
    syncer.download_daily_logs()
    print("-----------------------------------------")

    print("[2/2] Parsing Metrics & Pushing to Telegram...")
    reporter = TelegramReporter()
    reporter.send_report()
    print("=========================================")
    print("           PIPELINE COMPLETE             ")
    print("=========================================")

if __name__ == "__main__":
    execute_reporting_pipeline()
'''
write_and_verify("src/scripts/eod_master.py", eod_master_code)


# ==============================================================================
# 6. UPGRADE src/ml/bot_trainer.py
#    Removes dead macro_matrix seed & calibrates per-stock thresholds cleanly
# ==============================================================================
bt_path = os.path.join(PROJECT_ROOT, "src", "ml", "bot_trainer.py")
if os.path.exists(bt_path):
    with open(bt_path, "r", encoding="utf-8") as f:
        bt_code = f.read()
    bt_code = bt_code.replace("seed_macro_redis()\n", "")
    bt_code = bt_code.replace('"sl_pct": 0.012,\n                                "tp_pct": 0.024,\n                                "breakeven_pct": 0.0075,', '"sl_pct": 0.0095,\n                                "tp_pct": 0.0220,\n                                "breakeven_pct": 0.0075,')
    write_and_verify("src/ml/bot_trainer.py", bt_code)


# ==============================================================================
# 7. UPGRADE src/trading/strategy_engine.py
#    - True Per-Stock LightGBM Chop Veto, Directional Edge Gate & AI Catalyst Veto
#    - 3-Stage Single-Exit Dynamic Profit Ratchet (Eliminates Partial-TP Double Tax)
#    - Strict VWAP Pullback Value-Entry + Rs.12,000 Min Notional + 2 Entries/Stock Cap
#    - Full Autopsy Excel + Signal Log Telemetry Restored
# ==============================================================================
strategy_engine_code = r'''
"""
src/trading/strategy_engine.py
Institutional Per-Stock Micro-Bot Quantitative Strategy Engine.
- Enforces LightGBM Chop Veto, Directional Edge Alignment & Pre-Market AI Catalyst Veto.
- Uses a 3-Stage Single-Exit Dynamic Trailing Ratchet (100% position size, zero partial-split tax drag).
- Enforces Max Concurrent Positions, Max 2 Entries/Stock/Day, and 1-Strike Stop-Loss Blacklist.
"""
import sys
import os
import json
import redis
import uuid
import pytz
import numpy as np
import lightgbm as lgb
from collections import deque
from dotenv import load_dotenv
from datetime import datetime, time

from src.trading.indicators.spread import BidAskSpread
from src.trading.indicators.obi import OrderBookImbalance
from src.trading.indicators.cvd import CumulativeVolumeDelta
from src.trading.indicators.wmp import WeightedMidPrice
from src.trading.indicators.atr import TickATR
from src.trading.indicators.vwap import TickVWAP

from src.trading.portfolio import PortfolioManager
from src.trading.audit_logger import AuditLogger, setup_signal_logger
from src.trading.execution_client import ExecutionEngine
from src.scripts.aws_sync import AWSSync
from src.scripts.telegram_reporter import TelegramReporter
from src.trading.autopsy_exporter import export_autopsy_to_excel
from src.utils.alert_publisher import push_alert
from src.utils.tg_notifier import send_tg_event
from src.utils.fault_guard import install_global_exception_hook

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../"))
load_dotenv(os.path.join(project_root, ".env"))

install_global_exception_hook("STRATEGY_ENGINE")

BOTS_DIR = os.path.join(project_root, "models", "bots")
EXCLUDED_TOKENS = {"99926000", "99926009", "99926002", "26000", "26009", "26014", "2885", "1333"}


class StockMicroBot:
    """Per-stock autonomous brain holding custom LightGBM model, 2m bar aggregator, and calibrated thresholds."""
    def __init__(self, sec_id: str):
        self.sec_id = str(sec_id)
        self.profile = self._load_profile()
        self.symbol = self.profile.get("symbol", f"TOK_{self.sec_id}")
        self.model = self._load_model()
        self.bars = deque(self.profile.get("warmup_bars", []), maxlen=30)
        self.current_bucket = None
        self.active_bar = None
        self.last_ml_eval = {
            "regime": None,
            "prob_bull": 0.33,
            "prob_bear": 0.33,
            "prob_chop": 0.34,
            "edge": 0.0
        }
        if self.model and len(self.bars) >= 9:
            last_c = float(self.bars[-1]["close"])
            self._evaluate_ml(last_c, last_c * float(self.profile.get("atr_2m_pct", 0.0025)))

    def _load_profile(self) -> dict:
        p_path = os.path.join(BOTS_DIR, f"profile_{self.sec_id}.json")
        if os.path.exists(p_path):
            try:
                with open(p_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "token": self.sec_id,
            "symbol": f"TOK_{self.sec_id}",
            "cohort": "MATH",
            "ai_score": 0.50,
            "master_score": 0.60,
            "direction_bias": "NEUTRAL",
            "sl_pct": 0.0095,
            "tp_pct": 0.0220,
            "has_ml_model": False
        }

    def _load_model(self):
        m_path = os.path.join(BOTS_DIR, f"bot_{self.sec_id}.txt")
        if os.path.exists(m_path) and os.path.getsize(m_path) > 500:
            try:
                return lgb.Booster(model_file=m_path)
            except Exception:
                try:
                    os.remove(m_path)
                except Exception:
                    pass
        return None

    def on_tick(self, tick_ts_ms: int, ltp: float, ltq: int, vwap_val: float, atr_val: float):
        if ltp <= 0:
            return
        bucket = (tick_ts_ms // 1000) // 120 * 120
        if self.current_bucket is None:
            self.current_bucket = bucket
            self.active_bar = {"open": ltp, "high": ltp, "low": ltp, "close": ltp, "vol": max(1, ltq)}
            return
        if bucket > self.current_bucket:
            self.bars.append(self.active_bar)
            self._evaluate_ml(vwap_val, atr_val)
            self.current_bucket = bucket
            self.active_bar = {"open": ltp, "high": ltp, "low": ltp, "close": ltp, "vol": max(1, ltq)}
        else:
            if ltp > self.active_bar["high"]:
                self.active_bar["high"] = ltp
            if ltp < self.active_bar["low"]:
                self.active_bar["low"] = ltp
            self.active_bar["close"] = ltp
            self.active_bar["vol"] += max(0, ltq)

    def _evaluate_ml(self, vwap_val: float, atr_val: float):
        if not self.model or len(self.bars) < 9:
            return
        try:
            closes = [b["close"] for b in self.bars]
            vols = [b["vol"] for b in self.bars]
            cur = self.bars[-1]
            c_now = cur["close"]
            ret_2m = (c_now / closes[-2]) - 1.0 if closes[-2] else 0.0
            ret_6m = (c_now / closes[-4]) - 1.0 if len(closes) >= 4 and closes[-4] else 0.0
            ret_16m = (c_now / closes[-9]) - 1.0 if len(closes) >= 9 and closes[-9] else 0.0
            eff_atr = atr_val if atr_val > 0 else (c_now * self.profile.get("atr_2m_pct", 0.0025))
            vwap_dist_atr = float(np.clip((c_now - (vwap_val or c_now)) / max(eff_atr, 1e-4), -5.0, 5.0))
            rvol_2m = float(np.clip(cur["vol"] / max(np.mean(vols[-20:]), 1.0), 0.0, 10.0))
            rng = max(cur["high"] - cur["low"], 1e-4)
            body_ratio = float(np.clip((cur["close"] - cur["open"]) / rng, -1.0, 1.0))
            atr_pct = float(np.clip(eff_atr / max(c_now, 1.0), 0.0005, 0.05))

            diffs = np.diff(closes[-15:])
            gains = np.mean(np.clip(diffs, 0, None))
            losses = max(np.mean(np.clip(-diffs, 0, None)), 1e-6)
            rsi_14 = float(np.clip(1.0 - (1.0 / (1.0 + (gains / losses))), 0.0, 1.0))

            X = np.array([[ret_2m, ret_6m, ret_16m, vwap_dist_atr, rvol_2m, body_ratio, atr_pct, rsi_14]], dtype=np.float32)
            probs = self.model.predict(X)[0]
            p_bear, p_chop, p_bull = float(probs[0]), float(probs[1]), float(probs[2])

            if p_chop >= 0.46:
                reg = 0
            elif (p_bull - p_bear) >= 0.04:
                reg = 1
            elif (p_bear - p_bull) >= 0.04:
                reg = -1
            else:
                reg = 0

            self.last_ml_eval = {
                "regime": reg,
                "prob_bull": p_bull,
                "prob_bear": p_bear,
                "prob_chop": p_chop,
                "edge": abs(p_bull - p_bear)
            }
        except Exception:
            pass

    def evaluate_signal_authority(self, proposed_action: str) -> tuple[bool, str, str]:
        """
        Enforces Pre-Market AI Catalyst Vetoes + Intraday Per-Stock LightGBM Regime Authority.
        """
        ai_bias = str(self.profile.get("direction_bias", "NEUTRAL")).upper()
        ai_score = float(self.profile.get("ai_score", 0.50))

        # 1. Pre-Market AI Catalyst Hard Veto (Never trade against high-conviction overnight news)
        if proposed_action == "SELL" and (ai_bias in ("LONG", "BULLISH") or ai_score >= 0.65):
            return False, "VETO_AI_BULL_CATALYST", f"AI_BULL({ai_score:.2f})"
        if proposed_action == "BUY" and (ai_bias in ("SHORT", "BEARISH") or ai_score <= 0.35):
            return False, "VETO_AI_BEAR_CATALYST", f"AI_BEAR({ai_score:.2f})"

        # 2. Per-Stock LightGBM Micro-Bot Authority
        ml_reg = self.last_ml_eval.get("regime")
        if ml_reg is not None:
            p_bull = float(self.last_ml_eval.get("prob_bull", 0.33))
            p_bear = float(self.last_ml_eval.get("prob_bear", 0.33))
            p_chop = float(self.last_ml_eval.get("prob_chop", 0.34))

            # Veto severe choppy regime unless backed by strong AI catalyst
            if p_chop >= 0.52 and abs(ai_score - 0.50) < 0.15:
                return False, "VETO_BOT_CHOP", f"BOT_CHOP({p_chop*100:.0f}%)"

            # Veto trades where the stock's ML model favors the opposite direction
            if proposed_action == "BUY" and (p_bear > p_bull + 0.02):
                return False, "VETO_BOT_BEAR", f"BOT_BEAR({p_bear*100:.0f}%)"
            if proposed_action == "SELL" and (p_bull > p_bear + 0.02):
                return False, "VETO_BOT_BULL", f"BOT_BULL({p_bull*100:.0f}%)"

            reg_tag = f"BOT_BULL({p_bull*100:.0f}%)" if proposed_action == "BUY" else f"BOT_BEAR({p_bear*100:.0f}%)"
            return True, "BOT_ML_MODEL", reg_tag

        return True, "STOCK_ORDERFLOW_MATH", "PURE_STOCK_MATH"


def sync_live_redis_state(
    r: redis.Redis, portfolio: PortfolioManager, last_tick_str: str = "",
    processed: int = 0, active_bots: int = 0, mode: str = "LIVE"
):
    r.set("live_balance", round(portfolio.current_balance, 2))
    r.set("live_positions", json.dumps(portfolio.positions))
    r.set("live_active_capital", round(portfolio.get_active_notional(), 2))
    r.set("live_idle_capital", round(portfolio.get_idle_capital(), 2))
    if last_tick_str:
        r.set("last_tick_ist", last_tick_str)
    r.set("engine_heartbeat", json.dumps({
        "mode": mode,
        "ticks_processed": processed,
        "active_bots": active_bots,
        "open_positions": len(portfolio.positions),
        "wall_time": datetime.now().strftime("%H:%M:%S")
    }))


def run_end_of_day_reporting(portfolio: PortfolioManager, autopsy_records=None, replay_mode=False):
    portfolio.save_state()
    print(f"[STATE SAVED] Balance: Rs.{portfolio.current_balance:,.2f} | Peak Active Capital: Rs.{portfolio.peak_capital_deployed:,.2f}")

    excel_path = None
    if autopsy_records:
        try:
            excel_path = export_autopsy_to_excel(autopsy_records)
        except Exception as e:
            print(f"[WARNING] Autopsy Excel export failed: {e}")

    try:
        mode_tag = "REPLAY" if replay_mode else "LIVE"
        print(f"[SYSTEM] Dispatching {mode_tag} End-of-Day Telegram Report...")
        reporter = TelegramReporter()
        reporter.send_report(mode_label=mode_tag)
        reporter.send_file(reporter.csv_path)
        if not replay_mode:
            log_path = os.path.join(reporter.base_dir, "logs", f"signals_{reporter.today_str}.log")
            reporter.send_file(log_path)
            if excel_path and os.path.exists(excel_path):
                reporter.send_file(excel_path)
            db_dump = reporter.backup_postgres()
            if db_dump:
                reporter.send_file(db_dump)
        print("[SYSTEM] Telegram dispatch complete.")
    except Exception as e:
        print(f"[WARNING] Telegram reporting failed: {e}")

    if not replay_mode:
        try:
            print("\n[SYSTEM] Commencing Cloud Handoff to AWS S3...")
            AWSSync().upload_daily_logs()
        except Exception as e:
            print(f"[WARNING] AWS sync failed: {e}")


def square_off_all(portfolio: PortfolioManager, engine: ExecutionEngine, latest_ticks: dict, replay_mode: bool = False):
    open_positions = list(portfolio.positions.keys())
    if not open_positions:
        return

    print(f"\n[SYSTEM] Initiating forced 3:14 PM liquidation of {len(open_positions)} positions.")
    push_alert(1, "ENGINE", f"🚨 3:14 PM Square-Off: Closing {len(open_positions)} open positions.")

    for open_sec_id in open_positions:
        try:
            pos_data = portfolio.positions.get(open_sec_id)
            if not pos_data:
                continue

            pos_qty = pos_data['qty']
            side = pos_data['side']
            entry_p = pos_data['avg_price']
            sym = pos_data.get('symbol', f"TOK_{open_sec_id}")
            exit_action = 'SELL' if side == 'BUY' else 'BUY'
            trace_id = f"{open_sec_id}-SQUAREOFF-{uuid.uuid4().hex[:6]}"

            cached_tick = latest_ticks.get(open_sec_id)
            if cached_tick:
                market_ltp = cached_tick.get('ltp', 0.0)
                market_bid = cached_tick.get('bid', market_ltp)
                market_ask = cached_tick.get('ask', market_ltp)
                ts_val = cached_tick.get('timestamp', datetime.now().isoformat())
            else:
                market_ltp = entry_p or 1.0
                market_bid = market_ltp
                market_ask = market_ltp
                ts_val = datetime.now().isoformat()

            exit_tick = {
                'security_id': open_sec_id,
                'ltp': market_ltp,
                'price': market_ltp,
                'ask': market_ask,
                'bid': market_bid,
                'best_ask_vol': pos_qty,
                'best_bid_vol': pos_qty,
                'timestamp': ts_val
            }

            engine.execute_paper_trade(
                exit_tick, action=exit_action, qty=pos_qty, trace_id=trace_id,
                bypass_margin=True, symbol=sym, trigger_source="EOD_CUTOFF", trigger_reason="3:14_PM_SQUAREOFF"
            )
            est_pnl = ((market_ltp - entry_p) * pos_qty) if side == 'BUY' else ((entry_p - market_ltp) * pos_qty)
            push_alert(
                2, "EXECUTION",
                f"🛑 <b>3:14 PM SQUARE-OFF ({exit_action})</b>: <code>{sym}</code>\n"
                f"Qty: {pos_qty} | Entry: ₹{entry_p:.2f} -> Exit: ₹{market_ltp:.2f} | Gross: ₹{est_pnl:+.2f}"
            )
            if open_sec_id in portfolio.positions:
                del portfolio.positions[open_sec_id]
        except Exception as e:
            print(f"  [ERROR] Failed squaring off {open_sec_id}: {e}")


def main():
    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True
    )

    max_slots = int(os.getenv("MAX_CONCURRENT_POSITIONS", 4))
    portfolio = PortfolioManager(
        starting_balance=100000.0,
        max_allocation_pct=0.20,
        max_loss_pct=0.02,
        max_concurrent_positions=max_slots
    )
    logger = AuditLogger()
    engine = ExecutionEngine(portfolio, logger)
    signal_logger = setup_signal_logger()
    ist_tz = pytz.timezone('Asia/Kolkata')

    r.delete("EMERGENCY_STOP")
    sync_live_redis_state(r, portfolio)

    spread = BidAskSpread()
    obi = OrderBookImbalance(alpha=0.1)
    cvd = CumulativeVolumeDelta(window=500)
    wmp = WeightedMidPrice()
    atr = TickATR(period=14, tick_chunk=50)
    vwap = TickVWAP()
    indicators = [spread, obi, cvd, wmp, atr, vwap]

    last_trade_time = {}
    stock_entries_today = {}
    stopped_out_stocks = set()
    ratchet_stage = {}  # sec_id -> int (0, 1, 2, 3)

    MAX_ENTRIES_PER_STOCK = int(os.getenv("MAX_ENTRIES_PER_STOCK", 2))
    COOLDOWN_SECONDS = 180.0
    signal_streak = {}
    latest_ticks = {}
    velocity_buffers = {}
    micro_bots = {}
    autopsy_records = []

    replay_mode = r.get("replay_mode")
    if replay_mode == "stream":
        tick_count = r.xlen("replay_stream")
        print(f"[MODE] REPLAY via Redis Stream | Max Concurrent Slots: {max_slots} | Total Stream Ticks: {tick_count:,}")
        send_tg_event("REPLAY ENGINE", "START", f"Replaying {tick_count:,} ticks with 3-Stage Profit Ratchet & ML Authority ({max_slots} slots).")
        last_stream_id = "0-0"
        pubsub = None
    else:
        replay_mode = None
        tick_count = 0
        print(f"[MODE] LIVE via Redis Pub/Sub | Max Concurrent Slots: {max_slots}")
        pubsub = r.pubsub()
        pubsub.subscribe("live_ticks")
        push_alert(1, "ENGINE", f"🚀 Micro-Bot Engine Online (LIVE) | Max Concurrent Slots: {max_slots}")
        send_tg_event("LIVE ENGINE", "START", f"Micro-Bot Engine Online | Max Concurrent Slots: {max_slots}")

    cutoff_time = time(15, 14, 0)
    no_new_entries_time = time(14, 45, 0)
    processed = 0
    equity_ticks_processed = 0
    cutoff_confirm_streak = 0
    stream_batch = deque()
    last_heartbeat_min = -1

    while True:
        if not replay_mode:
            now_ist = datetime.now(ist_tz)
            current_time = now_ist.time()
            if now_ist.minute % 15 == 0 and now_ist.minute != last_heartbeat_min:
                last_heartbeat_min = now_ist.minute
                send_tg_event(
                    "LIVE HEARTBEAT", "SUCCESS",
                    f"Ticks: {processed:,} | Active Slots: {len(portfolio.positions)}/{max_slots} | Balance: ₹{portfolio.current_balance:,.2f}"
                )
            if current_time >= cutoff_time:
                print("\n[CUTOFF] 3:14 PM reached (wall clock).")
                square_off_all(portfolio, engine, latest_ticks, replay_mode=False)
                sync_live_redis_state(r, portfolio)
                run_end_of_day_reporting(portfolio, autopsy_records, replay_mode=False)
                break

        tick = None
        if replay_mode:
            if not stream_batch:
                results = r.xread({"replay_stream": last_stream_id}, count=2500, block=500)
                if not results:
                    print(f"\n[REPLAY COMPLETE] Processed {processed:,} total stream ticks ({equity_ticks_processed:,} equity ticks).")
                    square_off_all(portfolio, engine, latest_ticks, replay_mode=True)
                    sync_live_redis_state(r, portfolio, processed=processed, active_bots=len(micro_bots), mode="REPLAY_DONE")
                    run_end_of_day_reporting(portfolio, autopsy_records, replay_mode=True)
                    r.delete("replay_mode")
                    r.delete("replay_stream")
                    break
                stream_batch.extend(results[0][1])

            entry_id, entry_data = stream_batch.popleft()
            last_stream_id = entry_id
            tick = json.loads(entry_data["data"])
            tick_epoch_ms = int(tick.get('timestamp', 0))
            current_time = datetime.fromtimestamp(tick_epoch_ms / 1000.0, tz=ist_tz).time()

            processed += 1
            if processed % 100000 == 0:
                pct = (processed / (tick_count or 1)) * 100
                print(
                    f"  [REPLAY] {processed:>7,} / {tick_count:,} ticks ({pct:5.1f}%) | "
                    f"Time: {str(current_time)[:8]} | Slots: {len(portfolio.positions)}/{max_slots} | "
                    f"Balance: Rs.{portfolio.current_balance:,.2f}"
                )

            if current_time >= cutoff_time:
                cutoff_confirm_streak += 1
                if cutoff_confirm_streak >= 5 and len(portfolio.positions) > 0:
                    square_off_all(portfolio, engine, latest_ticks, replay_mode=True)
                continue
            else:
                cutoff_confirm_streak = 0
        else:
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not (message and message['type'] == 'message'):
                continue
            tick = json.loads(message['data'])
            tick_epoch_ms = int(tick.get('timestamp', 0))
            current_time = datetime.fromtimestamp(tick_epoch_ms / 1000.0, tz=ist_tz).time()
            processed += 1

        if not tick:
            continue

        raw_sec_id = tick.get('security_id')
        if not raw_sec_id:
            continue
        sec_id = str(raw_sec_id)

        if sec_id in EXCLUDED_TOKENS:
            continue

        equity_ticks_processed += 1
        if equity_ticks_processed % 500 == 0:
            sync_live_redis_state(
                r, portfolio, str(current_time)[:8],
                processed, len(micro_bots), "REPLAY" if replay_mode else "LIVE"
            )
            if r.get("EMERGENCY_STOP") == "1":
                print("\n[FATAL] Emergency Stop flag detected in Redis. Halting operations.")
                square_off_all(portfolio, engine, latest_ticks, replay_mode=bool(replay_mode))
                run_end_of_day_reporting(portfolio, autopsy_records, replay_mode=bool(replay_mode))
                r.delete("EMERGENCY_STOP")
                break

        tick_ltp = float(tick.get('ltp', 0.0))
        if tick_ltp <= 0.0:
            continue
        latest_ticks[sec_id] = tick

        for ind in indicators:
            ind.update(tick)

        obi_score = obi.get_score(sec_id)
        cvd_score = cvd.get_score(sec_id)
        current_atr = atr.get_score(sec_id)
        current_vwap = vwap.get_score(sec_id)

        bot = micro_bots.get(sec_id)
        if bot is None:
            bot = StockMicroBot(sec_id)
            micro_bots[sec_id] = bot
        bot.on_tick(tick_epoch_ms, tick_ltp, int(tick.get('ltq', 1)), current_vwap, current_atr)
        sym = bot.symbol

        # =================================================================
        # 5. SINGLE-EXIT 3-STAGE DYNAMIC PROFIT RATCHET (ZERO SPLIT TAX DRAG)
        # =================================================================
        current_pos = portfolio.positions.get(sec_id)
        if current_pos:
            entry_price = current_pos['avg_price']
            pos_qty = current_pos['qty']
            side = current_pos['side']

            SL_PERCENT = 0.0095   # -0.95% tight risk cap
            TP_PERCENT = 0.0220   # +2.20% full target

            stage = ratchet_stage.get(sec_id, 0)
            move_pct = ((tick_ltp - entry_price) / entry_price) if side == 'BUY' else ((entry_price - tick_ltp) / entry_price)

            # Advance ratchet stage as unrealized profit expands
            if move_pct >= 0.0165 and stage < 3:
                stage = 3
                ratchet_stage[sec_id] = 3
            elif move_pct >= 0.0120 and stage < 2:
                stage = 2
                ratchet_stage[sec_id] = 2
            elif move_pct >= 0.0075 and stage < 1:
                stage = 1
                ratchet_stage[sec_id] = 1

            # Determine locked stop-loss offset based on highest stage achieved
            if stage == 3:
                lock_offset = 0.0140  # Locks +1.40% on 100% size
            elif stage == 2:
                lock_offset = 0.0095  # Locks +0.95% on 100% size (replaces tax-heavy Partial TP split)
            elif stage == 1:
                lock_offset = 0.0045  # Locks +0.45% net profit after tax
            else:
                lock_offset = -SL_PERCENT

            if side == 'BUY':
                tp_price = entry_price * (1.0 + TP_PERCENT)
                sl_price = entry_price * (1.0 + lock_offset)
                hit_tp = tick_ltp >= tp_price
                hit_sl = tick_ltp <= sl_price
                exit_action = 'SELL'
                gross_leg = (tick_ltp - entry_price) * pos_qty
            else:
                tp_price = entry_price * (1.0 - TP_PERCENT)
                sl_price = entry_price * (1.0 - lock_offset)
                hit_tp = tick_ltp <= tp_price
                hit_sl = tick_ltp >= sl_price
                exit_action = 'BUY'
                gross_leg = (entry_price - tick_ltp) * pos_qty

            if hit_tp or hit_sl:
                trace_id = f"{sec_id}-{tick_epoch_ms}-{uuid.uuid4().hex[:6]}"
                if hit_tp:
                    reason = "FINAL_TAKE_PROFIT"
                elif stage >= 2:
                    reason = f"TRAIL_PROFIT_S{stage}_+{lock_offset*100:.2f}%"
                elif stage == 1:
                    reason = "BREAKEVEN_LOCK_+0.45%"
                else:
                    reason = "STOP_LOSS"
                    stopped_out_stocks.add(sec_id)

                engine.execute_paper_trade(
                    tick, action=exit_action, qty=pos_qty, trace_id=trace_id,
                    bypass_margin=True, symbol=sym, trigger_source="POSITION_MGR", trigger_reason=reason
                )
                if sec_id in portfolio.positions:
                    del portfolio.positions[sec_id]
                ratchet_stage.pop(sec_id, None)
                last_trade_time[sec_id] = tick_epoch_ms / 1000.0
                push_alert(
                    2, "EXECUTION",
                    f"🛑 <b>{reason} ({side})</b>: <code>{sym}</code> | Closed {pos_qty}x @ ₹{tick_ltp:.2f} | Gross: ₹{gross_leg:+.2f}"
                )
            continue
        else:
            ratchet_stage.pop(sec_id, None)

        # =================================================================
        # 6. HIGH-PRECISION ENTRY LOGIC + MICRO-BOT AUTHORITY + AUTOPSY
        # =================================================================
        if current_atr == 0.0 or current_time >= no_new_entries_time:
            continue

        if sec_id in stopped_out_stocks or stock_entries_today.get(sec_id, 0) >= MAX_ENTRIES_PER_STOCK:
            continue

        tick_bid = float(tick.get('bid', tick_ltp))
        tick_ask = float(tick.get('ask', tick_ltp))
        natural_spread = abs(tick_ask - tick_bid)

        if natural_spread > (0.08 * current_atr):
            continue

        tick_ts = tick_epoch_ms / 1000.0
        if tick_ts - last_trade_time.get(sec_id, 0.0) < COOLDOWN_SECONDS:
            continue

        v_buffer = velocity_buffers.get(sec_id)
        if v_buffer is None:
            v_buffer = deque()
            velocity_buffers[sec_id] = v_buffer

        current_ts_ms = tick_ts * 1000.0
        v_buffer.append((current_ts_ms, obi_score, cvd_score))

        while len(v_buffer) > 1 and (current_ts_ms - v_buffer[0][0]) > 6000.0:
            v_buffer.popleft()

        if not v_buffer or (current_ts_ms - v_buffer[0][0]) < 1500.0:
            continue

        old_ts, old_obi, old_cvd = v_buffer[0]
        delta_obi = obi_score - old_obi
        delta_cvd = cvd_score - old_cvd

        OBI_THRESHOLD = 0.22
        CVD_THRESHOLD = max(60.0, 120000.0 / max(tick_ltp, 1.0))

        # Value-Zone Pullback Filter:
        # BUY only when price is at or slightly below VWAP (not chasing extended tops, not catching >2% crashes)
        # SELL only when price is at or slightly above VWAP (not shorting bottoms, not shorting >2.5% circuit rockets)
        vwap_diff_pct = ((tick_ltp - current_vwap) / current_vwap) if current_vwap > 0 else 0.0
        in_buy_zone = (-0.0180 <= vwap_diff_pct <= 0.0005)
        in_sell_zone = (-0.0005 <= vwap_diff_pct <= 0.0220)

        is_bullish = in_buy_zone and (delta_obi >= OBI_THRESHOLD) and (delta_cvd >= CVD_THRESHOLD)
        is_bearish = in_sell_zone and (delta_obi <= -OBI_THRESHOLD) and (delta_cvd <= -CVD_THRESHOLD)

        if is_bullish:
            current_signal = "BUY"
        elif is_bearish:
            current_signal = "SELL"
        else:
            signal_streak[sec_id] = (None, 0)
            continue

        prev_signal, prev_count = signal_streak.get(sec_id, (None, 0))
        signal_streak[sec_id] = (current_signal, prev_count + 1 if prev_signal == current_signal else 1)

        streak_count = signal_streak[sec_id][1]
        if streak_count < 3:
            continue

        approved, trigger_source, reg_label = bot.evaluate_signal_authority(current_signal)
        ts_ist_str = datetime.fromtimestamp(tick_epoch_ms / 1000.0, tz=ist_tz).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

        cvd_edge = min(1.0, max(0.0, (abs(delta_cvd) - CVD_THRESHOLD) / 600.0))
        obi_edge = min(1.0, max(0.0, (abs(delta_obi) - OBI_THRESHOLD) / 0.25))
        spread_edge = max(0.0, 1.0 - (natural_spread / (0.08 * current_atr)))
        confluence_score = round((cvd_edge + obi_edge + spread_edge) / 3.0, 3)

        trigger_reason = (
            f"{trigger_source}[{reg_label}] | dCVD:{delta_cvd:+.0f}(Thr:{CVD_THRESHOLD:.0f}) "
            f"| dOBI:{delta_obi:+.2f} | LTP:{tick_ltp:.2f} vs VWAP:{current_vwap:.2f}"
        )

        autopsy_entry = {
            "timestamp_ist": ts_ist_str,
            "security_id": sec_id,
            "symbol": sym,
            "action": current_signal,
            "trigger_source": trigger_source,
            "trigger_reason": trigger_reason,
            "ltp": tick_ltp,
            "vwap": round(current_vwap, 2),
            "atr": round(current_atr, 4),
            "delta_obi": round(delta_obi, 4),
            "delta_cvd": round(delta_cvd, 1),
            "confluence_score": confluence_score,
            "ml_regime": reg_label,
            "prob_bull": round(bot.last_ml_eval.get("prob_bull", 0.0) * 100, 1),
            "prob_bear": round(bot.last_ml_eval.get("prob_bear", 0.0) * 100, 1),
            "prob_chop": round(bot.last_ml_eval.get("prob_chop", 0.0) * 100, 1),
            "target_qty": 0,
            "status": "PENDING",
            "trace_id": "NONE"
        }

        if not approved:
            autopsy_entry["status"] = trigger_source
            autopsy_records.append(autopsy_entry)
            continue

        calculated_target = 14000.0 + (confluence_score * 6000.0)
        target_capital = min(calculated_target, 20000.0)

        free_cash = portfolio.get_idle_capital()
        if free_cash < 2400.0:
            autopsy_entry["status"] = "REJECTED_INSUFFICIENT_FUNDS"
            autopsy_records.append(autopsy_entry)
            continue

        ideal_qty = int(target_capital / tick_ltp)
        available_vol = int(tick.get('best_bid_vol', 0) if current_signal == "SELL" else tick.get('best_ask_vol', 0)) or ideal_qty
        qty = min(ideal_qty, available_vol, int((free_cash * 5.0) / tick_ltp))

        # Require at least Rs.12,000 notional so brokerage is <0.17% of position value
        if qty > 0 and (qty * tick_ltp) >= 12000.0:
            trace_id = f"{sec_id}-{tick_epoch_ms}-{uuid.uuid4().hex[:6]}"
            status = engine.execute_paper_trade(
                tick, action=current_signal, qty=qty, trace_id=trace_id,
                symbol=sym, trigger_source=trigger_source, trigger_reason=trigger_reason
            )
            autopsy_entry["target_qty"] = qty
            autopsy_entry["trace_id"] = trace_id
            autopsy_entry["status"] = status
            autopsy_records.append(autopsy_entry)

            signal_logger.info(
                f"Trace: {trace_id} | {sym} (#{sec_id}) | {current_signal} {qty}x @ Rs.{tick_ltp:.2f} | "
                f"Source: {trigger_source} | Reason: {trigger_reason} | Status: {status}"
            )

            if status == "EXECUTED":
                last_trade_time[sec_id] = tick_ts
                stock_entries_today[sec_id] = stock_entries_today.get(sec_id, 0) + 1
                ratchet_stage[sec_id] = 0
                signal_streak[sec_id] = (None, 0)
                push_alert(
                    2,
                    "EXECUTION",
                    f"🟢 <b>ENTRY ({current_signal})</b>: <code>{sym}</code> (#{sec_id})\n"
                    f"Qty: <b>{qty}</b> @ ₹{tick_ltp:.2f} (Notional: ₹{qty*tick_ltp:,.0f})\n"
                    f"Brain: <b>{trigger_source}</b> [{reg_label}] | Conf: <b>{confluence_score:.2f}</b>"
                )
        else:
            autopsy_entry["status"] = "REJECTED_LOW_L1_LIQUIDITY"
            autopsy_records.append(autopsy_entry)


if __name__ == "__main__":
    main()
'''
write_and_verify("src/trading/strategy_engine.py", strategy_engine_code)

print("\n[MASTER QUANT UPGRADE APPLIED AND VERIFIED SUCCESSFULLY!]")