"""Strategy engine with risk management, full-spectrum autopsy telemetry, and tiered Telegram alerts."""
import sys
import os
import json
import redis
import uuid
import pytz
from collections import deque
from dotenv import load_dotenv
from datetime import datetime, time

# Indicators
from src.trading.indicators.spread import BidAskSpread
from src.trading.indicators.obi import OrderBookImbalance
from src.trading.indicators.cvd import CumulativeVolumeDelta
from src.trading.indicators.wmp import WeightedMidPrice
from src.trading.indicators.atr import TickATR
from src.trading.indicators.vwap import TickVWAP

# Sandbox Components
from src.trading.portfolio import PortfolioManager
from src.trading.audit_logger import AuditLogger
from src.trading.execution_client import ExecutionEngine
from src.scripts.aws_sync import AWSSync

# Risk & ML Components
from src.trading.risk_manager import RiskManager
from src.ml.regime_inference import RegimeOracle

# Logging, Telemetry & Dispatch Components
from src.trading.audit_logger import setup_signal_logger
from src.scripts.telegram_reporter import TelegramReporter
from src.trading.autopsy_exporter import export_autopsy_to_excel
from src.utils.alert_publisher import push_alert

current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, "../../.env")
load_dotenv(dotenv_path=env_path)

# Initialize Oracle once during engine startup
oracle = RegimeOracle()


def run_end_of_day_reporting(portfolio, autopsy_records=None):
    """Saves state, exports autopsy logs, syncs to AWS S3, and dispatches Telegram reports[cite: 5, 8]."""
    portfolio.save_state()
    print(f"[STATE SAVED] Balance: Rs.{portfolio.current_balance:.2f}")

    # 1. Export Trade Autopsy to Excel
    excel_path = None
    if autopsy_records:
        try:
            excel_path = export_autopsy_to_excel(autopsy_records)
        except Exception as e:
            print(f"[WARNING] Autopsy Excel export failed: {e}")

    # 2. Cloud Handoff to AWS S3
    try:
        print("\n[SYSTEM] Commencing Cloud Handoff to AWS S3...")
        cloud_sync = AWSSync()
        cloud_sync.upload_daily_logs()
    except Exception as e:
        print(f"[WARNING] AWS sync failed: {e}")

    # 3. Telegram Reporting Dispatch
    try:
        print("[SYSTEM] Generating End-of-Day Telegram Report...")
        reporter = TelegramReporter()
        reporter.send_report()
        reporter.send_file(reporter.csv_path)
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


def square_off_all(portfolio, engine, latest_ticks):
    """Squares off every open position using the last known market price[cite: 8]."""
    open_positions = list(portfolio.positions.keys())
    if not open_positions:
        return

    print(f"\n[SYSTEM] Initiating forced liquidation of {len(open_positions)} positions.")
    push_alert(1, "ENGINE", f"🚨 LIQUIDATION: Forcing square-off on {len(open_positions)} open positions.")

    for open_sec_id in open_positions:
        try:
            pos_data = portfolio.positions.get(open_sec_id)
            if not pos_data:
                continue

            pos_qty = pos_data['qty']
            side = pos_data['side']
            exit_action = 'SELL' if side == 'BUY' else 'BUY'
            trace_id = f"{open_sec_id}-SQUAREOFF-{uuid.uuid4().hex[:6]}"
            print(f"  [LIQUIDATION] Closing {side} on #{open_sec_id}. Trace: {trace_id}")

            cached_tick = latest_ticks.get(open_sec_id)
            if cached_tick:
                market_ltp = cached_tick.get('ltp', 0.0)
                market_bid = cached_tick.get('bid', market_ltp)
                market_ask = cached_tick.get('ask', market_ltp)
            else:
                market_ltp = pos_data.get('avg_price') or 1.0
                market_bid = market_ltp
                market_ask = market_ltp
                print(f"  [WARNING] No cached market data for #{open_sec_id}. Using entry price.")

            exit_tick = {
                'security_id': open_sec_id,
                'ltp': market_ltp,
                'price': market_ltp,
                'ask': market_ask,
                'bid': market_bid,
                'best_ask_vol': pos_qty,
                'best_bid_vol': pos_qty,
                'timestamp': datetime.now().isoformat()
            }

            engine.execute_paper_trade(exit_tick, action=exit_action, qty=pos_qty, trace_id=trace_id, bypass_margin=True)
            push_alert(3, "EXECUTION", f"🛑 SQUARE-OFF ({exit_action}): #{open_sec_id} | Qty: {pos_qty} @ ₹{market_ltp:.2f}")

            if open_sec_id in portfolio.positions:
                del portfolio.positions[open_sec_id]

        except Exception as e:
            print(f"  [ERROR] Failed squaring off {open_sec_id}: {e}")
            push_alert(1, "ENGINE", f"❌ ERROR: Failed squaring off #{open_sec_id}: {e}")


def main():
    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True
    )

    # Clear stale emergency stop flags on boot
    r.delete("EMERGENCY_STOP")

    # Initialize Math Engine
    spread = BidAskSpread()
    obi = OrderBookImbalance()
    cvd = CumulativeVolumeDelta()
    wmp = WeightedMidPrice()
    atr = TickATR(period=14, tick_chunk=50)
    vwap = TickVWAP()
    indicators = [spread, obi, cvd, wmp, atr, vwap]

    portfolio = PortfolioManager(starting_balance=100000.0, max_allocation_pct=0.20, max_loss_pct=0.02)
    logger = AuditLogger()
    engine = ExecutionEngine(portfolio, logger)

    risk_manager = RiskManager(base_risk_pct=0.10)
    signal_logger = setup_signal_logger()
    ist_tz = pytz.timezone('Asia/Kolkata')

    # Execution cooldown memory (30s prevents overtrading)
    last_trade_time = {}
    COOLDOWN_SECONDS = 30.0

    # Signal stability filter
    signal_streak = {}

    # Cache latest tick per security
    latest_ticks = {}

    # State tracking
    breakeven_locked = set()
    partial_tp_executed = set()
    pyramid_executed = set()
    velocity_buffers = {}

    # In-memory buffer for end-of-day trade autopsy
    autopsy_records = []

    # =================================================================
    # TICK SOURCE: Stream (fast replay) or Pub/Sub (live + paced replay)
    # =================================================================
    replay_mode = r.get("replay_mode")

    if replay_mode == "stream":
        print("[MODE] REPLAY via Redis Stream (fast, guaranteed delivery)")
        print("[MODE] Using tick timestamps for cutoff logic.")
        last_stream_id = "0-0"
        pubsub = None
        tick_count = r.xlen("replay_stream")
        print(f"[MODE] Stream contains {tick_count:,} ticks to process.")
    else:
        replay_mode = None
        print("[MODE] LIVE via Redis Pub/Sub")
        pubsub = r.pubsub()
        pubsub.subscribe("live_ticks")

    print("Execution System Live. Listening for ticks...\n")
    push_alert(1, "ENGINE", f"🚀 Engine online in {'REPLAY' if replay_mode else 'LIVE'} mode.")

    cutoff_time = time(15, 14, 0)
    no_new_entries_time = time(15, 0, 0)
    processed = 0

    while True:
        # =================================================================
        # 1. CUTOFF CHECK
        # =================================================================
        if not replay_mode:
            current_time = datetime.now(ist_tz).time()
            if current_time >= cutoff_time:
                print("\n[CUTOFF] 3:14 PM reached (wall clock).")
                square_off_all(portfolio, engine, latest_ticks)
                run_end_of_day_reporting(portfolio, autopsy_records)
                print("\n[TERMINATED] All positions flat. Data secured. Shutting down.")
                break

        # =================================================================
        # 2. FETCH NEXT TICK
        # =================================================================
        tick = None

        if replay_mode:
            results = r.xread({"replay_stream": last_stream_id}, count=1, block=2000)

            if not results:
                print(f"\n[REPLAY COMPLETE] Processed {processed:,} ticks.")
                print("[REPLAY] Squaring off remaining positions...")
                square_off_all(portfolio, engine, latest_ticks)
                run_end_of_day_reporting(portfolio, autopsy_records)

                r.delete("replay_mode")
                r.delete("replay_stream")
                print("[CLEANUP] Replay stream and mode flag cleared.")
                break

            _, entries = results[0]
            entry_id, entry_data = entries[0]
            last_stream_id = entry_id
            tick = json.loads(entry_data["data"])

            tick_epoch_ms = tick.get('timestamp', 0)
            current_time = datetime.fromtimestamp(tick_epoch_ms / 1000, tz=ist_tz).time()

            if current_time >= cutoff_time:
                print(f"\n[CUTOFF] 3:14 PM reached (tick timestamp). Processed {processed:,} ticks.")
                square_off_all(portfolio, engine, latest_ticks)
                run_end_of_day_reporting(portfolio, autopsy_records)

                r.delete("replay_mode")
                r.delete("replay_stream")
                print("[CLEANUP] Replay stream and mode flag cleared.")
                break

            processed += 1
            if processed % 10000 == 0:
                pct = (processed / (tick_count or 1)) * 100
                print(f"  [REPLAY] {processed:,} ticks processed ({pct:.0f}%)")

        else:
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not (message and message['type'] == 'message'):
                continue
            tick = json.loads(message['data'])
            tick_epoch_ms = tick.get('timestamp', 0)
            current_time = datetime.fromtimestamp(tick_epoch_ms / 1000, tz=ist_tz).time()

        if not tick:
            continue

        # =================================================================
        # 3. PROCESS TICK & PUSH LIVE STATE TO REDIS
        # =================================================================
        sec_id = str(tick.get('security_id'))

        # Ignore non-tradeable macro indices and anchor components[cite: 8]
        if sec_id in {"99926000", "99926009", "99926002", "2885", "1333"}:
            continue

        # Synchronize live state to Redis for Telegram Commander commands (/dashboard, /pnl)
        r.set("live_balance", portfolio.current_balance)
        r.set("live_positions", json.dumps(portfolio.positions))

        tick_ltp = tick.get('ltp', 0.0)
        latest_ticks[sec_id] = tick

        for ind in indicators:
            ind.update(tick)

        obi_score = obi.get_score(sec_id)
        cvd_score = cvd.get_score(sec_id)

        # =================================================================
        # 4. EMERGENCY KILL SWITCH CHECK (TELEGRAM /kill OVERRIDE)
        # =================================================================
        if r.get("EMERGENCY_STOP") == "1":
            print("\n[FATAL] Emergency Stop flag detected in Redis. Halting operations.")
            push_alert(1, "ENGINE", "🚨 EMERGENCY KILL SWITCH ENGAGED. Squaring off all positions immediately.")
            square_off_all(portfolio, engine, latest_ticks)
            run_end_of_day_reporting(portfolio, autopsy_records)
            r.delete("EMERGENCY_STOP")
            break

        # =================================================================
        # 5. POSITION MANAGEMENT (STRICT SL/TP, BREAKEVEN, PARTIAL SCALE, PYRAMID)
        # =================================================================
        current_pos = portfolio.positions.get(sec_id)
        if current_pos:
            entry_price = current_pos['avg_price']
            pos_qty = current_pos['qty']
            side = current_pos['side']

            SL_PERCENT = 0.015
            TP_PERCENT = 0.030
            BREAKEVEN_TRIGGER = 0.010
            PARTIAL_TP_TRIGGER = 0.015
            TAX_OFFSET_PCT = 0.002

            if side == 'BUY':
                tp_price = entry_price * (1.0 + TP_PERCENT)
                base_sl_price = entry_price * (1.0 - SL_PERCENT)

                if tick_ltp >= entry_price * (1.0 + BREAKEVEN_TRIGGER):
                    breakeven_locked.add(sec_id)

                # Partial Scale-out (+1.5% profit)
                if sec_id not in partial_tp_executed and tick_ltp >= entry_price * (1.0 + PARTIAL_TP_TRIGGER):
                    scale_qty = max(1, int(pos_qty * 0.5))
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    print(f"\n[PARTIAL TP] Long #{sec_id} scaling out {scale_qty} shares. Entry: {entry_price:.2f} Exit: {tick_ltp:.2f}. Trace: {trace_id}")
                    engine.execute_paper_trade(tick, action='SELL', qty=scale_qty, trace_id=trace_id, bypass_margin=True)
                    partial_tp_executed.add(sec_id)
                    push_alert(3, "EXECUTION", f"⚡ PARTIAL TP (Long): Scaled out {scale_qty}x #{sec_id} @ ₹{tick_ltp:.2f} (Entry: ₹{entry_price:.2f})")
                    continue

                if sec_id in breakeven_locked:
                    if sec_id in pyramid_executed:
                        sl_price = entry_price * 0.995
                    else:
                        sl_price = entry_price * (1.0 + TAX_OFFSET_PCT)
                else:
                    sl_price = base_sl_price

                # Final Take Profit or Stop-Loss Exit
                if tick_ltp >= tp_price or tick_ltp <= sl_price:
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    reason = "FINAL TAKE PROFIT" if tick_ltp >= tp_price else ("BREAKEVEN SL" if sec_id in breakeven_locked else "STOP LOSS")
                    print(f"\n[{reason}] Long #{sec_id}. Entry: {entry_price:.2f} Exit: {tick_ltp:.2f}. SL: {sl_price:.2f} TP: {tp_price:.2f}. Trace: {trace_id}")
                    engine.execute_paper_trade(tick, action='SELL', qty=pos_qty, trace_id=trace_id, bypass_margin=True)

                    if sec_id in portfolio.positions:
                        del portfolio.positions[sec_id]

                    breakeven_locked.discard(sec_id)
                    partial_tp_executed.discard(sec_id)
                    pyramid_executed.discard(sec_id)
                    push_alert(3, "EXECUTION", f"🛑 {reason} (Long): Closed #{sec_id} {pos_qty}x @ ₹{tick_ltp:.2f} (Entry: ₹{entry_price:.2f})")
                    continue

                # Pyramiding (+1.0% Profit & Strong CVD)
                if sec_id in breakeven_locked and sec_id not in pyramid_executed:
                    if cvd_score > 1500:
                        add_qty = max(1, int(pos_qty * 0.5))
                        trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                        print(f"\n[PYRAMID] Long #{sec_id} scaling IN {add_qty} shares. Entry: {entry_price:.2f} Add: {tick_ltp:.2f}. Trace: {trace_id}")
                        engine.execute_paper_trade(tick, action='BUY', qty=add_qty, trace_id=trace_id)
                        pyramid_executed.add(sec_id)
                        push_alert(3, "EXECUTION", f"🔼 PYRAMID (Long): Added {add_qty}x #{sec_id} @ ₹{tick_ltp:.2f}")
                        continue

            elif side == 'SELL':
                tp_price = entry_price * (1.0 - TP_PERCENT)
                base_sl_price = entry_price * (1.0 + SL_PERCENT)

                if tick_ltp <= entry_price * (1.0 - BREAKEVEN_TRIGGER):
                    breakeven_locked.add(sec_id)

                if sec_id not in partial_tp_executed and tick_ltp <= entry_price * (1.0 - PARTIAL_TP_TRIGGER):
                    scale_qty = max(1, int(pos_qty * 0.5))
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    print(f"\n[PARTIAL TP] Short #{sec_id} scaling out {scale_qty} shares. Entry: {entry_price:.2f} Exit: {tick_ltp:.2f}. Trace: {trace_id}")
                    engine.execute_paper_trade(tick, action='BUY', qty=scale_qty, trace_id=trace_id, bypass_margin=True)
                    partial_tp_executed.add(sec_id)
                    push_alert(3, "EXECUTION", f"⚡ PARTIAL TP (Short): Scaled out {scale_qty}x #{sec_id} @ ₹{tick_ltp:.2f} (Entry: ₹{entry_price:.2f})")
                    continue

                if sec_id in breakeven_locked:
                    if sec_id in pyramid_executed:
                        sl_price = entry_price * 1.005
                    else:
                        sl_price = entry_price * (1.0 - TAX_OFFSET_PCT)
                else:
                    sl_price = base_sl_price

                if tick_ltp <= tp_price or tick_ltp >= sl_price:
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    reason = "FINAL TAKE PROFIT" if tick_ltp <= tp_price else ("BREAKEVEN SL" if sec_id in breakeven_locked else "STOP LOSS")
                    print(f"\n[{reason}] Short #{sec_id}. Entry: {entry_price:.2f} Exit: {tick_ltp:.2f}. SL: {sl_price:.2f} TP: {tp_price:.2f}. Trace: {trace_id}")
                    engine.execute_paper_trade(tick, action='BUY', qty=pos_qty, trace_id=trace_id, bypass_margin=True)

                    if sec_id in portfolio.positions:
                        del portfolio.positions[sec_id]

                    breakeven_locked.discard(sec_id)
                    partial_tp_executed.discard(sec_id)
                    pyramid_executed.discard(sec_id)
                    push_alert(3, "EXECUTION", f"🛑 {reason} (Short): Closed #{sec_id} {pos_qty}x @ ₹{tick_ltp:.2f} (Entry: ₹{entry_price:.2f})")
                    continue

                if sec_id in breakeven_locked and sec_id not in pyramid_executed:
                    if cvd_score < -1500:
                        add_qty = max(1, int(pos_qty * 0.5))
                        trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                        print(f"\n[PYRAMID] Short #{sec_id} scaling IN {add_qty} shares. Entry: {entry_price:.2f} Add: {tick_ltp:.2f}. Trace: {trace_id}")
                        engine.execute_paper_trade(tick, action='SELL', qty=add_qty, trace_id=trace_id)
                        pyramid_executed.add(sec_id)
                        push_alert(3, "EXECUTION", f"🔼 PYRAMID (Short): Added {add_qty}x #{sec_id} @ ₹{tick_ltp:.2f}")
                        continue
        else:
            breakeven_locked.discard(sec_id)
            partial_tp_executed.discard(sec_id)
            pyramid_executed.discard(sec_id)

        # =================================================================
        # 6. ENTRY STRATEGY LOGIC & AUTOPSY LOGGING
        # =================================================================
        current_atr = atr.get_score(sec_id)
        if current_atr == 0.0:
            continue

        tick_bid = tick.get('bid', tick_ltp)
        tick_ask = tick.get('ask', tick_ltp)
        natural_spread = tick_ask - tick_bid

        # Spread limit relative to volatility
        if natural_spread > (0.08 * current_atr):
            continue

        # Strictly block new entries after 3:00 PM
        if current_time >= no_new_entries_time:
            continue

        current_vwap = vwap.get_score(sec_id)
        spread_score = spread.get_score(sec_id)
        MIN_POSITION_VALUE = 5000.0

        tick_ts = tick.get('timestamp', 0) / 1000.0

        if sec_id not in velocity_buffers:
            velocity_buffers[sec_id] = deque()

        v_buffer = velocity_buffers[sec_id]
        v_buffer.append((tick_ts * 1000.0, obi_score, cvd_score))

        current_ts_ms = tick_ts * 1000.0
        while v_buffer and (current_ts_ms - v_buffer[0][0]) > 3000.0:
            v_buffer.popleft()

        if not v_buffer or (current_ts_ms - v_buffer[0][0]) < 2000.0:
            continue

        old_ts, old_obi, old_cvd = v_buffer[0]
        delta_obi = obi_score - old_obi
        delta_cvd = cvd_score - old_cvd

        vwap_buffer = current_vwap * 0.002
        upper_vwap_band = current_vwap + vwap_buffer
        lower_vwap_band = current_vwap - vwap_buffer

        # Calibrated institutional entry thresholds (reduced fakeouts)
        OBI_THRESHOLD = 0.25
        CVD_THRESHOLD = 400.0

        is_bearish = (tick_ltp > upper_vwap_band) and (delta_obi > OBI_THRESHOLD) and (delta_cvd < -CVD_THRESHOLD)
        is_bullish = (tick_ltp < lower_vwap_band) and (delta_obi < -OBI_THRESHOLD) and (delta_cvd > CVD_THRESHOLD)

        if is_bearish:
            current_signal = "SELL"
        elif is_bullish:
            current_signal = "BUY"
        else:
            signal_streak[sec_id] = (None, 0)
            continue

        prev_signal, prev_count = signal_streak.get(sec_id, (None, 0))
        if prev_signal == current_signal:
            signal_streak[sec_id] = (current_signal, prev_count + 1)
        else:
            signal_streak[sec_id] = (current_signal, 1)

        streak_count = signal_streak[sec_id][1]
        if streak_count < 3:
            continue

        # ─── AUTOPSY SNAPSHOT: Math triggered a trade setup ───
        ts_ist_str = datetime.fromtimestamp(tick_epoch_ms / 1000, tz=ist_tz).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

        autopsy_entry = {
            "timestamp_ist": ts_ist_str,
            "security_id": sec_id,
            "action": current_signal,
            "ltp": tick_ltp,
            "bid": tick_bid,
            "ask": tick_ask,
            "spread": round(natural_spread, 4),
            "atr": round(current_atr, 4),
            "vwap": round(current_vwap, 2),
            "delta_obi": round(delta_obi, 4),
            "delta_cvd": round(delta_cvd, 2),
            "confluence_score": 0.0,
            "streak_count": streak_count,
            "ml_regime": "FAIL_OPEN",
            "prob_bull": 0.0,
            "prob_bear": 0.0,
            "prob_chop": 0.0,
            "ml_edge_pct": 0.0,
            "target_qty": 0,
            "status": "PENDING",
            "trace_id": "NONE"
        }

        # 1. Existing Position Check
        if current_pos:
            autopsy_entry["status"] = "BLOCKED_ACTIVE_POSITION"
            autopsy_records.append(autopsy_entry)
            continue

        # 2. Trade Cooldown Check
        if tick_ts - last_trade_time.get(sec_id, 0.0) < COOLDOWN_SECONDS:
            autopsy_entry["status"] = "BLOCKED_COOLDOWN"
            autopsy_records.append(autopsy_entry)
            continue

        # ─── MACRO REGIME CIRCUIT BREAKER (ACTIVE FROM 09:15 OPENING BELL) ───
        macro_regime = None
        try:
            macro_regime = oracle.get_regime(tick_epoch_ms)
            eval_meta = getattr(oracle, 'last_eval', {})
            autopsy_entry["ml_regime"] = eval_meta.get("regime_name", str(macro_regime))
            autopsy_entry["prob_bull"] = round(eval_meta.get("prob_bull", 0.0) * 100, 1)
            autopsy_entry["prob_bear"] = round(eval_meta.get("prob_bear", 0.0) * 100, 1)
            autopsy_entry["prob_chop"] = round(eval_meta.get("prob_chop", 0.0) * 100, 1)
            autopsy_entry["ml_edge_pct"] = round(eval_meta.get("edge", 0.0) * 100, 1)
        except Exception as e:
            print(f"[ORACLE EXCEPTION] ML Fault: {e}. Falling back to Pure Math.")
            macro_regime = None
            autopsy_entry["ml_regime"] = "EXCEPTION_BYPASS"

        # Check ML Veto Conditions
        if macro_regime is not None:
            if macro_regime == 0:
                autopsy_entry["status"] = "VETO_CHOP"
                autopsy_records.append(autopsy_entry)
                continue

            if current_signal == "BUY" and macro_regime == -1:
                autopsy_entry["status"] = "VETO_BEAR_REGIME"
                autopsy_records.append(autopsy_entry)
                continue

            if current_signal == "SELL" and macro_regime == 1:
                autopsy_entry["status"] = "VETO_BULL_REGIME"
                autopsy_records.append(autopsy_entry)
                continue

        # ─── DYNAMIC KELLY SCALING & POSITION SIZING ───
        cvd_edge = min(1.0, max(0.0, (abs(delta_cvd) - CVD_THRESHOLD) / 600.0))
        obi_edge = min(1.0, max(0.0, (abs(delta_obi) - OBI_THRESHOLD) / 0.25))
        spread_edge = max(0.0, 1.0 - (natural_spread / (0.08 * current_atr)))

        confluence_score = (cvd_edge + obi_edge + spread_edge) / 3.0
        autopsy_entry["confluence_score"] = round(confluence_score, 3)

        calculated_target = 10000 + (confluence_score * 15000.0)
        target_capital = min(calculated_target, 20000.0)

        margin_used = sum((pos['qty'] * pos['avg_price']) / 5.0 for pos in portfolio.positions.values())
        free_cash = portfolio.current_balance - margin_used

        if free_cash < (MIN_POSITION_VALUE / 5.0):
            autopsy_entry["status"] = "REJECTED_MARGIN_LIMIT"
            autopsy_records.append(autopsy_entry)
            signal_logger.info(
                f"Trace: BLOCKED-MARGIN | Sec: {sec_id} | Action: {current_signal} | "
                f"Confluence: {confluence_score:.2f} | Status: REJECTED_PORTFOLIO_LIMITS"
            )
            continue

        ideal_qty = int(target_capital / tick_ltp)
        available_vol = tick.get('best_bid_vol', 0) if current_signal == "SELL" else tick.get('best_ask_vol', 0)
        qty = min(ideal_qty, available_vol, int((free_cash * 5.0) / tick_ltp))

        if qty > 0 and (qty * tick_ltp) >= 4900.0:
            trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
            status = engine.execute_paper_trade(tick, action=current_signal, qty=qty, trace_id=trace_id)

            autopsy_entry["target_qty"] = qty
            autopsy_entry["trace_id"] = trace_id
            autopsy_entry["status"] = "EXECUTED" if status == "EXECUTED" else f"FAILED_{status}"
            autopsy_records.append(autopsy_entry)

            signal_logger.info(
                f"Trace: {trace_id} | Sec: {sec_id} | Action: {current_signal} | "
                f"Confluence: {confluence_score:.2f} | Cap: Rs.{target_capital:.2f} | "
                f"Qty: {qty} | Streak: {streak_count} | Status: {status}"
            )

            if status == "EXECUTED":
                last_trade_time[sec_id] = tick_ts
                push_alert(
                    3,
                    "EXECUTION",
                    f"🟢 ENTRY: {current_signal} {qty}x #{sec_id} @ ₹{tick_ltp:.2f}\n"
                    f"Confluence: {confluence_score:.2f} | Trace: {trace_id}"
                )
        else:
            autopsy_entry["status"] = "REJECTED_VOLUME_OR_MIN_VAL"
            autopsy_records.append(autopsy_entry)


if __name__ == "__main__":
    main()