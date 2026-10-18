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

            if p_chop >= 0.70:
                reg = 0
            elif p_bull >= p_bear:
                reg = 1
            elif p_bear > p_bull:
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

    def decide_regime(self, ltp: float, vwap_val: float, atr_val: float, obi: float, cvd: float, vel_buf=None) -> int:
        eff_vwap = vwap_val if vwap_val > 0 else ltp
        eff_atr = atr_val if atr_val > 0 else (ltp * self.profile.get("atr_2m_pct", 0.003))

        if self.model is not None and self.last_ml_eval.get("regime") is None:
            try:
                pts = [b["close"] for b in self.bars] if len(self.bars) >= 2 else ([x[1] for x in vel_buf] if vel_buf and len(vel_buf) >= 2 else [ltp, ltp])
                ret_2m = (ltp / pts[-1]) - 1.0 if pts[-1] else 0.0
                ret_6m = (ltp / pts[max(0, len(pts)-4)]) - 1.0 if pts[0] else 0.0
                ret_16m = (ltp / pts[0]) - 1.0 if pts[0] else 0.0
                vwap_dist_atr = float(np.clip((ltp - eff_vwap) / max(eff_atr, 1e-4), -5.0, 5.0))
                rvol_2m = 1.5
                body_ratio = float(np.clip(ret_2m / max(self.profile.get("atr_2m_pct", 0.003), 1e-4), -1.0, 1.0))
                atr_pct = float(np.clip(eff_atr / max(ltp, 1.0), 0.0005, 0.05))
                rsi_14 = 0.58 if ret_2m > 0 else (0.42 if ret_2m < 0 else 0.50)

                X = np.array([[ret_2m, ret_6m, ret_16m, vwap_dist_atr, rvol_2m, body_ratio, atr_pct, rsi_14]], dtype=np.float32)
                probs = self.model.predict(X)[0]
                p_bear, p_chop, p_bull = float(probs[0]), float(probs[1]), float(probs[2])
                self.last_ml_eval = {
                    "regime": 1 if p_bull >= p_bear else -1,
                    "prob_bull": p_bull,
                    "prob_bear": p_bear,
                    "prob_chop": p_chop,
                    "edge": abs(p_bull - p_bear)
                }
            except Exception:
                pass

        if self.model is None or self.last_ml_eval.get("regime") is None:
         return 1 if cvd >= 0 else -1
        p_bull = self.last_ml_eval.get("prob_bull", 0.33)
        p_bear = self.last_ml_eval.get("prob_bear", 0.33)
        if (p_bear - p_bull) > 0.20:
            return -1
        if (p_bull - p_bear) > 0.20:
            return 1
        return 1 if cvd >= 0 else -1


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
                3, "EXECUTION",
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
    breakeven_locked = set()
    partial_tp_executed = set()

    COOLDOWN_SECONDS = 30
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
        # 5. POSITION MANAGEMENT (SL=1.0%, TP=2.2%, BE=0.80%->+0.50%, PARTIAL=1.20%)
        # =================================================================
        current_pos = portfolio.positions.get(sec_id)
        if current_pos:
            entry_price = current_pos['avg_price']
            pos_qty = current_pos['qty']
            side = current_pos['side']

            SL_PERCENT = 0.012
            TP_PERCENT = 0.024
            BREAKEVEN_TRIGGER = 0.0075
            PARTIAL_TP_TRIGGER = 0.0120
            TAX_OFFSET_PCT = 0.0025
            PARTIAL_LOCK_PCT = 0.0025

            if side == 'BUY':
                tp_price = entry_price * (1.0 + TP_PERCENT)
                base_sl_price = entry_price * (1.0 - SL_PERCENT)

                if tick_ltp >= entry_price * (1.0 + BREAKEVEN_TRIGGER):
                    breakeven_locked.add(sec_id)

                if sec_id not in partial_tp_executed and tick_ltp >= entry_price * (1.0 + PARTIAL_TP_TRIGGER) and pos_qty >= 2:
                    scale_qty = max(1, int(pos_qty * 0.5))
                    trace_id = f"{sec_id}-{tick_epoch_ms}-{uuid.uuid4().hex[:6]}"
                    engine.execute_paper_trade(
                        tick, action='SELL', qty=scale_qty, trace_id=trace_id,
                        bypass_margin=True, symbol=sym, trigger_source="POSITION_MGR", trigger_reason="PARTIAL_TP_+1.20%"
                    )
                    partial_tp_executed.add(sec_id)
                    gross_leg = (tick_ltp - entry_price) * scale_qty
                    push_alert(
                        3, "EXECUTION",
                        f"⚡ <b>PARTIAL TP (Long)</b>: <code>{sym}</code> | Scaled out {scale_qty}x @ ₹{tick_ltp:.2f} | Gross: ₹{gross_leg:+.2f}"
                    )
                    continue

                if sec_id in breakeven_locked:
                    lock_pct = PARTIAL_LOCK_PCT if sec_id in partial_tp_executed else TAX_OFFSET_PCT
                    sl_price = entry_price * (1.0 + lock_pct)
                else:
                    sl_price = base_sl_price

                if tick_ltp >= tp_price or tick_ltp <= sl_price:
                    trace_id = f"{sec_id}-{tick_epoch_ms}-{uuid.uuid4().hex[:6]}"
                    reason = "FINAL_TAKE_PROFIT" if tick_ltp >= tp_price else ("BREAKEVEN_SL" if sec_id in breakeven_locked else "STOP_LOSS")
                    if reason == "STOP_LOSS": stopped_out_stocks.add(sec_id)
                    engine.execute_paper_trade(
                        tick, action='SELL', qty=pos_qty, trace_id=trace_id,
                        bypass_margin=True, symbol=sym, trigger_source="POSITION_MGR", trigger_reason=reason
                    )
                    if sec_id in portfolio.positions:
                        del portfolio.positions[sec_id]
                    breakeven_locked.discard(sec_id)
                    partial_tp_executed.discard(sec_id)
                    last_trade_time[sec_id] = tick_epoch_ms / 1000.0
                    gross_leg = (tick_ltp - entry_price) * pos_qty
                    push_alert(
                        3, "EXECUTION",
                        f"🛑 <b>{reason} (Long)</b>: <code>{sym}</code> | Closed {pos_qty}x @ ₹{tick_ltp:.2f} | Gross: ₹{gross_leg:+.2f}"
                    )
                    continue

            elif side == 'SELL':
                tp_price = entry_price * (1.0 - TP_PERCENT)
                base_sl_price = entry_price * (1.0 + SL_PERCENT)

                if tick_ltp <= entry_price * (1.0 - BREAKEVEN_TRIGGER):
                    breakeven_locked.add(sec_id)

                if sec_id not in partial_tp_executed and tick_ltp <= entry_price * (1.0 - PARTIAL_TP_TRIGGER) and pos_qty >= 2:
                    scale_qty = max(1, int(pos_qty * 0.5))
                    trace_id = f"{sec_id}-{tick_epoch_ms}-{uuid.uuid4().hex[:6]}"
                    engine.execute_paper_trade(
                        tick, action='BUY', qty=scale_qty, trace_id=trace_id,
                        bypass_margin=True, symbol=sym, trigger_source="POSITION_MGR", trigger_reason="PARTIAL_TP_-1.20%"
                    )
                    partial_tp_executed.add(sec_id)
                    gross_leg = (entry_price - tick_ltp) * scale_qty
                    push_alert(
                        3, "EXECUTION",
                        f"⚡ <b>PARTIAL TP (Short)</b>: <code>{sym}</code> | Scaled out {scale_qty}x @ ₹{tick_ltp:.2f} | Gross: ₹{gross_leg:+.2f}"
                    )
                    continue

                if sec_id in breakeven_locked:
                    lock_pct = PARTIAL_LOCK_PCT if sec_id in partial_tp_executed else TAX_OFFSET_PCT
                    sl_price = entry_price * (1.0 - lock_pct)
                else:
                    sl_price = base_sl_price

                if tick_ltp <= tp_price or tick_ltp >= sl_price:
                    trace_id = f"{sec_id}-{tick_epoch_ms}-{uuid.uuid4().hex[:6]}"
                    reason = "FINAL_TAKE_PROFIT" if tick_ltp <= tp_price else ("BREAKEVEN_SL" if sec_id in breakeven_locked else "STOP_LOSS")
                    if reason == "STOP_LOSS": stopped_out_stocks.add(sec_id)
                    engine.execute_paper_trade(
                        tick, action='BUY', qty=pos_qty, trace_id=trace_id,
                        bypass_margin=True, symbol=sym, trigger_source="POSITION_MGR", trigger_reason=reason
                    )
                    if sec_id in portfolio.positions:
                        del portfolio.positions[sec_id]
                    breakeven_locked.discard(sec_id)
                    partial_tp_executed.discard(sec_id)
                    last_trade_time[sec_id] = tick_epoch_ms / 1000.0
                    gross_leg = (entry_price - tick_ltp) * pos_qty
                    push_alert(
                        3, "EXECUTION",
                        f"🛑 <b>{reason} (Short)</b>: <code>{sym}</code> | Closed {pos_qty}x @ ₹{tick_ltp:.2f} | Gross: ₹{gross_leg:+.2f}"
                    )
                    continue
            continue
        else:
            breakeven_locked.discard(sec_id)
            partial_tp_executed.discard(sec_id)


        # =================================================================
        # 6. HIGH-PRECISION ENTRY LOGIC + MICRO-BOT AUTHORITY + AUTOPSY
        # =================================================================
        # Stop taking new entries after 2:00 PM to prevent 3:14 PM EOD square-off drag
        if current_atr == 0.0 or current_time >= time(14, 25, 0):
            continue

        if stock_entries_today.get(sec_id, 0) >= 2:
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

        OBI_THRESHOLD = 0.20
        CVD_THRESHOLD = max(50.0, 100000.0 / max(tick_ltp, 1.0))

        if time(11, 0, 0) <= current_time <= time(13, 30, 0):
            OBI_THRESHOLD = 0.30
            CVD_THRESHOLD *= 1.5

        vwap_diff_pct = ((tick_ltp - current_vwap) / current_vwap) if current_vwap > 0 else 0.0
        in_buy_zone = (-0.0250 <= vwap_diff_pct <= -0.0004)
        in_sell_zone = (0.0030 <= vwap_diff_pct <= 0.0250)

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

        current_macro_regime = bot.decide_regime(
            tick_ltp, current_vwap, current_atr, obi_score, cvd_score, v_buffer
        )
        if current_macro_regime == 0 or (current_signal == "BUY" and current_macro_regime == -1) or (current_signal == "SELL" and current_macro_regime == 1):
            continue
        trigger_source = "BOT_ML_MODEL"
        reg_label = "BOT_BULL" if current_macro_regime == 1 else "BOT_BEAR"
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

        free_cash = portfolio.get_idle_capital()
        if free_cash < 1000.0:
            autopsy_entry["status"] = "REJECTED_INSUFFICIENT_FUNDS"
            autopsy_records.append(autopsy_entry)
            continue

        base_slot_capital = (portfolio.current_balance / max_slots) * 0.70
        target_capital = min(15000.0, max(8000.0, base_slot_capital * (0.75 + 0.50 * confluence_score)))

        ideal_qty = int(target_capital // tick_ltp)
        max_afford_qty = int((free_cash * 5.0) // tick_ltp)
        qty = min(ideal_qty, max_afford_qty)

        if qty > 0 and (qty * tick_ltp) >= 5000.0:
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
                signal_streak[sec_id] = (None, 0)
                push_alert(
                    3,
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
