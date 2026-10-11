"""Strategy engine with risk management for live and replay trading."""
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

# Risk Components
from src.trading.risk_manager import RiskManager
from src.ml.regime_inference import RegimeOracle

# Log Components
from src.trading.audit_logger import setup_signal_logger
from src.scripts.telegram_reporter import TelegramReporter

current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, "../../.env")
load_dotenv(dotenv_path=env_path)
# Initialize Oracle once during engine startup
oracle = RegimeOracle()

def run_end_of_day_reporting(portfolio):
    """Saves state, syncs to AWS, sends Telegram reports. Called on every exit."""
    portfolio.save_state()
    print(f"[STATE SAVED] Balance: Rs.{portfolio.current_balance:.2f}")

    try:
        print("\n[SYSTEM] Commencing Cloud Handoff to AWS S3...")
        cloud_sync = AWSSync()
        cloud_sync.upload_daily_logs()
    except Exception as e:
        print(f"[WARNING] AWS sync failed: {e}")

    try:
        print("[SYSTEM] Generating End-of-Day Telegram Report...")
        reporter = TelegramReporter()
        reporter.send_report()
        reporter.send_file(reporter.csv_path)
        log_path = os.path.join(reporter.base_dir, "logs", f"signals_{reporter.today_str}.log")
        reporter.send_file(log_path)

        db_dump = reporter.backup_postgres()
        if db_dump:
            reporter.send_file(db_dump)
        print("[SYSTEM] Telegram dispatch complete.")
    except Exception as e:
        print(f"[WARNING] Telegram reporting failed: {e}")


def square_off_all(portfolio, engine, latest_ticks):
    """Squares off every open position using the last known market price."""
    # List is used to avoid mutating the dictionary while iterating
    open_positions = list(portfolio.positions.keys())
    if not open_positions:
        return

    print(f"\n[SYSTEM] Initiating forced liquidation of {len(open_positions)} positions.")
    for open_sec_id in open_positions:
        try:
            # Re-fetch pos_data in case it was modified
            pos_data = portfolio.positions.get(open_sec_id)
            if not pos_data:
                continue
                
            pos_qty = pos_data['qty']
            side = pos_data['side']
            exit_action = 'SELL' if side == 'BUY' else 'BUY'
            trace_id = f"{open_sec_id}-SQUAREOFF-{uuid.uuid4().hex[:6]}"
            print(f"  [LIQUIDATION] Closing {side} on #{open_sec_id}. Trace: {trace_id}")

            # Use last known market price, not entry price
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
            
            # Execute bypasses margin limits on square off
            engine.execute_paper_trade(exit_tick, action=exit_action, qty=pos_qty, trace_id=trace_id)
            
            # Explicitly force delete to prevent phantom positions
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

    # Execution cooldown memory — 30s prevents overtrading (was 5s, caused 98 trades/day)
    last_trade_time = {}
    COOLDOWN_SECONDS = 30.0

    # Signal stability filter
    signal_streak = {}   # {sec_id: ("BUY"|"SELL", consecutive_count)}

    # Cache the latest tick per security for accurate exit pricing
    latest_ticks = {}

    # State tracking for Trailing Stop-Loss, Partial Scaling, Pyramiding, and Velocity
    breakeven_locked = set()
    partial_tp_executed = set()
    pyramid_executed = set()
    velocity_buffers = {}  # {sec_id: deque([(timestamp, obi, cvd), ...])}

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

    cutoff_time = time(15, 14, 0)
    no_new_entries_time = time(15, 0, 0)
    processed = 0

    while True:
        # =================================================================
        # 1. CUTOFF CHECK (live mode uses wall clock, replay uses tick time)
        # =================================================================
        if not replay_mode:
            current_time = datetime.now(ist_tz).time()
            if current_time >= cutoff_time:
                print("\n[CUTOFF] 3:14 PM reached (wall clock).")
                square_off_all(portfolio, engine, latest_ticks)
                run_end_of_day_reporting(portfolio)
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
                run_end_of_day_reporting(portfolio)

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
                run_end_of_day_reporting(portfolio)

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
        # 3. PROCESS TICK
        # =================================================================
        sec_id = str(tick.get('security_id'))
        
        # ─── CRITICAL FIX: IGNORE NON-TRADEABLE MACRO INDICES ───
        # 99926000 (Nifty 50), 99926009 (Bank Nifty), 99926002 (Nifty IT)
        if sec_id in {"99926000", "99926009", "99926002","2885","1333"}:
            continue  # The ML Engine handles these; Strategy Engine ignores them
            
        tick_ltp = tick.get('ltp', 0.0)
        latest_ticks[sec_id] = tick

        for ind in indicators:
            ind.update(tick)

        obi_score = obi.get_score(sec_id)
        cvd_score = cvd.get_score(sec_id)

        # =================================================================
        # 4. POSITION MANAGEMENT (STRICT 1.5% SL / 3.0% TP + Scale-Out + Pyramiding)
        # =================================================================
        current_pos = portfolio.positions.get(sec_id)
        if current_pos:
            entry_price = current_pos['avg_price']
            pos_qty = current_pos['qty']
            side = current_pos['side']

            SL_PERCENT = 0.015           # 1.5% Base Stop Loss
            TP_PERCENT = 0.030           # 3.0% Final Take Profit Target
            BREAKEVEN_TRIGGER = 0.010    # +1.0% Unrealized profit activates lock & pyramid
            PARTIAL_TP_TRIGGER = 0.015   # +1.5% Unrealized profit triggers 50% scale-out
            TAX_OFFSET_PCT = 0.002       # +0.2% Offset to cover STT & Txn fees

            # CRITICAL FIX 2: Compute Breakeven SL based on original entry, not floating avg_price 
            # if we have pyramided. (Simplified: Use avg_price but add a larger buffer if pyramided)
            
            if side == 'BUY':
                tp_price = entry_price * (1.0 + TP_PERCENT)
                base_sl_price = entry_price * (1.0 - SL_PERCENT)

                # Lock breakeven
                if tick_ltp >= entry_price * (1.0 + BREAKEVEN_TRIGGER):
                    breakeven_locked.add(sec_id)

                # Check Partial TP (1.5%) - Only execute once
                if sec_id not in partial_tp_executed and tick_ltp >= entry_price * (1.0 + PARTIAL_TP_TRIGGER):
                    scale_qty = max(1, int(pos_qty * 0.5))
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    print(f"\n[PARTIAL TP] Long #{sec_id} scaling out {scale_qty} shares. Entry: {entry_price:.2f} Exit: {tick_ltp}. Trace: {trace_id}")
                    # CRITICAL FIX 1: Overwrite margin limit checks on exits
                    engine.execute_paper_trade(tick, action='SELL', qty=scale_qty, trace_id=trace_id, bypass_margin=True)
                    partial_tp_executed.add(sec_id)
                    continue  

                # Compute dynamic Stop-Loss
                if sec_id in breakeven_locked:
                    # Give a wider breathing room if we added size
                    if sec_id in pyramid_executed:
                        sl_price = entry_price * 0.995 # Allow 0.5% retracement on scaled size
                    else:
                        sl_price = entry_price * (1.0 + TAX_OFFSET_PCT)
                else:
                    sl_price = base_sl_price

                # Check Final TP or SL
                if tick_ltp >= tp_price or tick_ltp <= sl_price:
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    reason = "FINAL TAKE PROFIT" if tick_ltp >= tp_price else ("BREAKEVEN SL" if sec_id in breakeven_locked else "STOP LOSS")
                    print(f"\n[{reason}] Long #{sec_id}. Entry: {entry_price:.2f} Exit: {tick_ltp}. SL: {sl_price:.2f} TP: {tp_price:.2f}. Trace: {trace_id}")
                    # CRITICAL FIX 1: Bypass margin and explicitly delete
                    engine.execute_paper_trade(tick, action='SELL', qty=pos_qty, trace_id=trace_id, bypass_margin=True)
                    
                    if sec_id in portfolio.positions:
                        del portfolio.positions[sec_id]
                        
                    breakeven_locked.discard(sec_id)
                    partial_tp_executed.discard(sec_id)
                    pyramid_executed.discard(sec_id)
                    continue
                    
                # Dynamic Pyramiding (+1.0% Profit & CVD Expanding) - Evaluated LAST to avoid SL conflict
                if sec_id in breakeven_locked and sec_id not in pyramid_executed:
                    if cvd_score > 1500:
                        add_qty = max(1, int(pos_qty * 0.5))
                        trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                        print(f"\n[PYRAMID] Long #{sec_id} scaling IN {add_qty} shares. Entry: {entry_price:.2f} Add: {tick_ltp}. Trace: {trace_id}")
                        engine.execute_paper_trade(tick, action='BUY', qty=add_qty, trace_id=trace_id)
                        pyramid_executed.add(sec_id)
                        continue 

            elif side == 'SELL':
                tp_price = entry_price * (1.0 - TP_PERCENT)
                base_sl_price = entry_price * (1.0 + SL_PERCENT)

                if tick_ltp <= entry_price * (1.0 - BREAKEVEN_TRIGGER):
                    breakeven_locked.add(sec_id)

                if sec_id not in partial_tp_executed and tick_ltp <= entry_price * (1.0 - PARTIAL_TP_TRIGGER):
                    scale_qty = max(1, int(pos_qty * 0.5))
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    print(f"\n[PARTIAL TP] Short #{sec_id} scaling out {scale_qty} shares. Entry: {entry_price:.2f} Exit: {tick_ltp}. Trace: {trace_id}")
                    engine.execute_paper_trade(tick, action='BUY', qty=scale_qty, trace_id=trace_id, bypass_margin=True)
                    partial_tp_executed.add(sec_id)
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
                    print(f"\n[{reason}] Short #{sec_id}. Entry: {entry_price:.2f} Exit: {tick_ltp}. SL: {sl_price:.2f} TP: {tp_price:.2f}. Trace: {trace_id}")
                    engine.execute_paper_trade(tick, action='BUY', qty=pos_qty, trace_id=trace_id, bypass_margin=True)
                    
                    if sec_id in portfolio.positions:
                        del portfolio.positions[sec_id]
                        
                    breakeven_locked.discard(sec_id)
                    partial_tp_executed.discard(sec_id)
                    pyramid_executed.discard(sec_id)
                    continue

                if sec_id in breakeven_locked and sec_id not in pyramid_executed:
                    if cvd_score < -1500:
                        add_qty = max(1, int(pos_qty * 0.5))
                        trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                        print(f"\n[PYRAMID] Short #{sec_id} scaling IN {add_qty} shares. Entry: {entry_price:.2f} Add: {tick_ltp}. Trace: {trace_id}")
                        engine.execute_paper_trade(tick, action='SELL', qty=add_qty, trace_id=trace_id)
                        pyramid_executed.add(sec_id)
                        continue
        else:
            breakeven_locked.discard(sec_id)
            partial_tp_executed.discard(sec_id)
            pyramid_executed.discard(sec_id)
                
        # =================================================================
        # 5. ENTRY STRATEGY LOGIC
        # =================================================================

        current_atr = atr.get_score(sec_id)
        if current_atr == 0.0:
            continue

        tick_bid = tick.get('bid', tick_ltp)
        tick_ask = tick.get('ask', tick_ltp)
        natural_spread = tick_ask - tick_bid
        
        if natural_spread > (0.08 * current_atr):
            continue

        if current_time >= no_new_entries_time:
            continue

        # CRITICAL FIX 3: Lunchtime Chop Freeze Disabled per request
        # if time(11, 15, 0) <= current_time <= time(13, 30, 0):
        #    continue

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

        is_bearish = (tick_ltp > upper_vwap_band) and (delta_obi > 0.15) and (delta_cvd < -200.0)
        is_bullish = (tick_ltp < lower_vwap_band) and (delta_obi < -0.15) and (delta_cvd > 200.0)

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

        if current_pos:
            continue

        if tick_ts - last_trade_time.get(sec_id, 0.0) < COOLDOWN_SECONDS:
            continue
        
        # ─── MACRO REGIME CIRCUIT BREAKER (FAIL-OPEN ARCHITECTURE) ───
        if current_time >= time(9, 55, 0):
            try:
                macro_regime = oracle.get_regime(tick_epoch_ms)
            except Exception as e:
                # If LightGBM crashes, memory faults, or Pandas breaks, catch it here.
                print(f"[ORACLE EXCEPTION] ML Fault: {e}. Falling back to Pure Math.")
                macro_regime = None 
            
            # Only apply filters if the ML successfully returned a valid integer (1, -1, 0)
            if macro_regime is not None:
                # If the market is in Chop (0), veto trade to prevent tax churn
                if macro_regime == 0:
                    continue

                # If taking a BUY signal, but macro regime is Bearish (-1), block it
                if current_signal == "BUY" and macro_regime == -1:
                    continue

                # If taking a SELL signal, but macro regime is Bullish (1), block it
                if current_signal == "SELL" and macro_regime == 1:
                    continue
        
        # ─── DYNAMIC KELLY SCALING CAP FIX ───
        cvd_edge = min(1.0, max(0.0, (abs(delta_cvd) - 200.0) / 700.0))
        obi_edge = min(1.0, max(0.0, (abs(delta_obi) - 0.15) / 0.25))
        spread_edge = max(0.0, 1.0 - (natural_spread / (0.08 * current_atr)))
        
        confluence_score = (cvd_edge + obi_edge + spread_edge) / 3.0
        
        # CRITICAL FIX 4: Hard cap the target capital at 15000 so it doesn't drain the wallet instantly
        calculated_target = 10000 + (confluence_score * 15000.0)
        target_capital = min(calculated_target, 20000.0)
        
        # Calculate Margin Limit
        margin_used = sum((pos['qty'] * pos['avg_price']) / 5.0 for pos in portfolio.positions.values()) # Assuming 5x leverage
        free_cash = portfolio.current_balance - margin_used
        
        # Require minimum Rs. 4900 free cash to take any new position
        if free_cash < (MIN_POSITION_VALUE / 5.0):
             signal_logger.info(
                f"Trace: BLOCKED-MARGIN | Sec: {sec_id} | Action: {current_signal} | "
                f"Confluence: {confluence_score:.2f} | Status: REJECTED_PORTFOLIO_LIMITS"
            )
             continue
             
        ideal_qty = int(target_capital / tick_ltp)
        available_vol = tick.get('best_bid_vol', 0) if current_signal == "SELL" else tick.get('best_ask_vol', 0)
        
        # Limit the quantity by 5x leverage buying power
        qty = min(ideal_qty, available_vol, int((free_cash * 5.0) / tick_ltp))

        if qty > 0 and (qty * tick_ltp) >= 4900.0:
            trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
            # Bypass margin is False for entries
            status = engine.execute_paper_trade(tick, action=current_signal, qty=qty, trace_id=trace_id)
            
            signal_logger.info(
                f"Trace: {trace_id} | Sec: {sec_id} | Action: {current_signal} | "
                f"Confluence: {confluence_score:.2f} | Cap: Rs.{target_capital:.2f} | "
                f"Qty: {qty} | Streak: {streak_count} | Status: {status}"
            )
            
            if status == "EXECUTED":
                last_trade_time[sec_id] = tick_ts

if __name__ == "__main__":
    main()