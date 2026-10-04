"""Strategy engine with risk management for live and replay trading."""
import sys
import os
import json
import redis
import uuid
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

# Log Components
from src.trading.audit_logger import setup_signal_logger
from src.scripts.telegram_reporter import TelegramReporter

current_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(current_dir, "../../.env")
load_dotenv(dotenv_path=env_path)


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
    open_positions = list(portfolio.positions.items())
    if not open_positions:
        return

    print(f"\n[SYSTEM] Initiating forced liquidation of {len(open_positions)} positions.")
    for open_sec_id, pos_data in open_positions:
        try:
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
            engine.execute_paper_trade(exit_tick, action=exit_action, qty=pos_qty, trace_id=trace_id)
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

    portfolio = PortfolioManager(starting_balance=100000.0, max_allocation_pct=0.10, max_loss_pct=0.02)
    logger = AuditLogger()
    engine = ExecutionEngine(portfolio, logger)

    risk_manager = RiskManager(base_risk_pct=0.10)
    signal_logger = setup_signal_logger()

    # Execution cooldown memory — 30s prevents overtrading (was 5s, caused 98 trades/day)
    last_trade_time = {}
    COOLDOWN_SECONDS = 30.0

    # Signal stability filter — require N consecutive ticks confirming the same direction
    # before entering. Prevents whipsaw from flickering OBI/CVD.
    signal_streak = {}   # {sec_id: ("BUY"|"SELL", consecutive_count)}
    MIN_SIGNAL_STREAK = 3

    # Cache the latest tick per security for accurate exit pricing
    latest_ticks = {}

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
            current_time = datetime.now().time()
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
            # STREAM MODE: Read next tick from Redis Stream (guaranteed, no drops)
            results = r.xread({"replay_stream": last_stream_id}, count=1, block=2000)

            if not results:
                # Stream exhausted — all ticks processed
                print(f"\n[REPLAY COMPLETE] Processed {processed:,} ticks.")
                print("[REPLAY] Squaring off remaining positions...")
                square_off_all(portfolio, engine, latest_ticks)
                run_end_of_day_reporting(portfolio)

                # Cleanup: remove replay mode flag and stream
                r.delete("replay_mode")
                r.delete("replay_stream")
                print("[CLEANUP] Replay stream and mode flag cleared.")
                break

            _, entries = results[0]
            entry_id, entry_data = entries[0]
            last_stream_id = entry_id
            tick = json.loads(entry_data["data"])

            # In replay mode, use the TICK's timestamp for cutoff, not wall clock
            tick_epoch_ms = tick.get('timestamp', 0)
            current_time = datetime.fromtimestamp(tick_epoch_ms / 1000).time()

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
            # PUB/SUB MODE: Non-blocking read (live + paced replay)
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if not (message and message['type'] == 'message'):
                continue
            tick = json.loads(message['data'])

        if not tick:
            continue

        # =================================================================
        # 3. PROCESS TICK (identical for both modes from here)
        # =================================================================
        sec_id = str(tick.get('security_id'))
        tick_ltp = tick.get('ltp', 0.0)

        # Cache this tick as latest market data for this security
        latest_ticks[sec_id] = tick

        # Update all indicators
        for ind in indicators:
            ind.update(tick)

        obi_score = obi.get_score(sec_id)
        cvd_score = cvd.get_score(sec_id)

        # =================================================================
        # 4. POSITION MANAGEMENT (ATR-BASED SL/TP — STANDARD INTRADAY)
        # =================================================================
        current_pos = portfolio.positions.get(sec_id)
        if current_pos:
            entry_price = current_pos['avg_price']
            pos_qty = current_pos['qty']
            side = current_pos['side']

            raw_atr = atr.get_score(sec_id)
            # ATR floor: 1% of price (was 0.5%). Prevents micro-stops on thinly-traded stocks.
            min_atr = tick_ltp * 0.01
            atr_val = raw_atr if raw_atr > min_atr else min_atr

            # Absolute SL floor: 1% of price OR the natural bid-ask spread, whichever is larger.
            # This ensures the SL is never inside normal market noise.
            natural_spread = tick.get('ask', 0.0) - tick.get('bid', 0.0)
            min_sl_distance = max(tick_ltp * 0.01, natural_spread)

            if side == 'BUY':
                # TP at 4x ATR, SL at 2x ATR → 1:2 risk-reward ratio
                tp_price = entry_price + (atr_val * 4.0)
                sl_distance = max(atr_val * 2.0, min_sl_distance)
                sl_price = entry_price - sl_distance

                if tick_ltp >= tp_price or tick_ltp <= sl_price:
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    reason = "TAKE PROFIT" if tick_ltp >= tp_price else "STOP LOSS"
                    print(f"\n[{reason}] Long #{sec_id}. Entry: {entry_price:.2f} Exit: {tick_ltp}. SL: {sl_price:.2f} TP: {tp_price:.2f}. Trace: {trace_id}")
                    engine.execute_paper_trade(tick, action='SELL', qty=pos_qty, trace_id=trace_id)
                    continue

            elif side == 'SELL':
                tp_price = entry_price - (atr_val * 4.0)
                sl_distance = max(atr_val * 2.0, min_sl_distance)
                sl_price = entry_price + sl_distance

                if tick_ltp <= tp_price or tick_ltp >= sl_price:
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    reason = "TAKE PROFIT" if tick_ltp <= tp_price else "STOP LOSS"
                    print(f"\n[{reason}] Short #{sec_id}. Entry: {entry_price:.2f} Exit: {tick_ltp}. SL: {sl_price:.2f} TP: {tp_price:.2f}. Trace: {trace_id}")
                    engine.execute_paper_trade(tick, action='BUY', qty=pos_qty, trace_id=trace_id)
                    continue

        # =================================================================
        # 5. ENTRY STRATEGY LOGIC
        # =================================================================

        # Block new entries if ATR hasn't warmed up yet
        if atr.get_score(sec_id) == 0.0:
            continue

        # Block new entries after 3:00 PM (uses tick time in replay, wall clock in live)
        if current_time >= no_new_entries_time:
            continue

        current_vwap = vwap.get_score(sec_id)
        spread_score = spread.get_score(sec_id)
        MIN_POSITION_VALUE = 5000.0

        # Use tick timestamp for cooldown — works correctly in both live and replay
        tick_ts = tick.get('timestamp', 0) / 1000.0  # epoch ms → seconds

        # ─── DETERMINE SIGNAL DIRECTION (MICRO-STRUCTURE DIVERGENCE) ───
        # Stop chasing exhaustion breakouts. Enter on pullbacks to the VWAP when 
        # aggressive market orders (CVD) diverge from a spoofed limit book (OBI).
        
        # 1. VWAP Proximity: Only trade if the price is within 0.5% of the VWAP. 
        # If it is further away, the move is already overextended.
        near_vwap_bullish = current_vwap < tick_ltp < (current_vwap * 1.005)
        near_vwap_bearish = (current_vwap * 0.995) < tick_ltp < current_vwap

        # 2. The Divergence Logic
        # BUY: Trend is UP, Price is near VWAP. Aggressive buyers are stepping in (CVD > 800),
        # but the limit book looks bearish (OBI < 0.0). Smart money is silently absorbing limit sells.
        is_bullish = near_vwap_bullish and cvd_score > 800 and obi_score < 0.0

        # SELL: Trend is DOWN, Price is near VWAP. Aggressive sellers are dumping (CVD < -800),
        # but the limit book looks bullish (OBI > 0.0). Smart money is distributing into limit buys.
        is_bearish = near_vwap_bearish and cvd_score < -800 and obi_score > 0.0

        # ─── SIGNAL STREAK FILTER ───
        # Track consecutive ticks confirming the same direction.
        # Only act when MIN_SIGNAL_STREAK consecutive ticks agree.
        if is_bearish:
            current_signal = "SELL"
        elif is_bullish:
            current_signal = "BUY"
        else:
            # No signal — reset streak for this security
            signal_streak[sec_id] = (None, 0)
            continue

        prev_signal, prev_count = signal_streak.get(sec_id, (None, 0))
        if prev_signal == current_signal:
            signal_streak[sec_id] = (current_signal, prev_count + 1)
        else:
            signal_streak[sec_id] = (current_signal, 1)

        streak_count = signal_streak[sec_id][1]
        if streak_count < MIN_SIGNAL_STREAK:
            continue  # Signal not stable yet — wait for confirmation

        # ─── REVERSAL: Close opposing position, then WAIT ───
        # Don't immediately flip — just close and let the cooldown enforce patience.
        if current_pos:
            if current_signal == "SELL" and current_pos['side'] == 'BUY':
                trace_id = f"{sec_id}-{tick.get('timestamp')}-REVSQ-{uuid.uuid4().hex[:6]}"
                print(f"\n[REVERSAL] Closing Long #{sec_id}. Trace: {trace_id}")
                engine.execute_paper_trade(tick, action='SELL', qty=current_pos['qty'], trace_id=trace_id)
                last_trade_time[sec_id] = tick_ts  # Start cooldown after close
                continue
            elif current_signal == "BUY" and current_pos['side'] == 'SELL':
                trace_id = f"{sec_id}-{tick.get('timestamp')}-REVSQ-{uuid.uuid4().hex[:6]}"
                print(f"\n[REVERSAL] Closing Short #{sec_id}. Trace: {trace_id}")
                engine.execute_paper_trade(tick, action='BUY', qty=current_pos['qty'], trace_id=trace_id)
                last_trade_time[sec_id] = tick_ts  # Start cooldown after close
                continue
            else:
                # Already have a position in the same direction — skip
                continue

        # ─── COOLDOWN CHECK (using tick timestamp) ───
        if tick_ts - last_trade_time.get(sec_id, 0.0) < COOLDOWN_SECONDS:
            continue

        # ─── POSITION SIZING AND EXECUTION ───
        confidence = risk_manager.calculate_confidence(obi_score, cvd_score, spread_score)
        margin_used = sum(pos['qty'] * pos['avg_price'] for pos in portfolio.positions.values())
        free_cash = portfolio.current_balance - margin_used

        if current_signal == "SELL":
            qty = risk_manager.calculate_position_size(confidence, free_cash, tick_ltp, tick.get('best_bid_vol', 0))
        else:
            qty = risk_manager.calculate_position_size(confidence, free_cash, tick_ltp, tick.get('best_ask_vol', 0))

        if qty > 0 and (qty * tick_ltp) >= MIN_POSITION_VALUE:
            trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
            status = engine.execute_paper_trade(tick, action=current_signal, qty=qty, trace_id=trace_id)
            signal_logger.info(f"Trace: {trace_id} | Sec: {sec_id} | Action: {current_signal} | Conf: {confidence:.2f} | Qty: {qty} | Streak: {streak_count} | Status: {status}")
            if status == "EXECUTED":
                last_trade_time[sec_id] = tick_ts

if __name__ == "__main__":
    main()
