"""Strategy engine with risk management for live trading."""
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
    
    pubsub = r.pubsub()
    pubsub.subscribe("live_ticks")
    risk_manager = RiskManager(base_risk_pct=0.10)
    signal_logger = setup_signal_logger()
    
    # PHASE 2: Initialize execution cooldown memory to prevent async clustering
    last_trade_time = {}
    COOLDOWN_SECONDS = 5.0

    print("Signal Logger initialized. Silently logging to /logs directory...")
    print("Execution System Live. Listening for ticks and searching for setups...")
    
    while True:
        # =================================================================
        # 1. THE 3:14 PM HARD KILL-SWITCH
        # =================================================================
        current_time = datetime.now().time()
        cutoff_time = time(15, 14, 0) 
        
        if current_time >= cutoff_time:
            open_positions = list(portfolio.positions.items())
            
            if open_positions:
                print("\n[SYSTEM ALERT] 3:14 PM Cutoff Reached. Initiating forced liquidation.")
                for open_sec_id, pos_data in open_positions:
                    try:
                        pos_qty = pos_data['qty']
                        side = pos_data['side']
                        exit_action = 'SELL' if side == 'BUY' else 'BUY'
                        trace_id = f"{open_sec_id}-SQUAREOFF-{uuid.uuid4().hex[:6]}"
                        print(f"[LIQUIDATION] Force closing {side} position on #{open_sec_id}. Trace: {trace_id}")
                        
                        price = pos_data.get('avg_price') or 1.0
                        
                        dummy_tick = {
                            'security_id': open_sec_id, 
                            'ltp': price,
                            'price': price,
                            'ask': price,
                            'bid': price,
                            'best_ask_vol': pos_qty,
                            'best_bid_vol': pos_qty,
                            'timestamp': datetime.now().isoformat()
                        }
                        engine.execute_paper_trade(dummy_tick, action=exit_action, qty=pos_qty, trace_id=trace_id)
                    except Exception as e:
                        print(f"[ERROR] Failed squaring off position {open_sec_id}. Reason: {e}")
            
            portfolio.save_state()
            print(f"[STATE SAVED] Final Portfolio Balance: ₹{portfolio.current_balance:.2f} written to disk.")
            
            print("\n[SYSTEM] Commencing Cloud Handoff to AWS S3...")
            cloud_sync = AWSSync()
            cloud_sync.upload_daily_logs()

            print("\n[SYSTEM] Generating End-of-Day Telegram Report and DB Backup...")
            reporter = TelegramReporter()
            reporter.send_report()
            reporter.send_file(reporter.csv_path)
            log_path = os.path.join(reporter.base_dir, "logs", f"signals_{reporter.today_str}.log")
            reporter.send_file(log_path)
            
            db_dump = reporter.backup_postgres()
            if db_dump:
                reporter.send_file(db_dump)

            print("\n[SYSTEM TERMINATED] All intraday positions flat and data secured. Shutting down.")
            break 
        
        # =================================================================
        # 2. NON-BLOCKING REDIS FETCH
        # =================================================================
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        
        if message and message['type'] == 'message':
            tick = json.loads(message['data'])
            sec_id = str(tick.get('security_id'))
            tick_ltp = tick.get('ltp', 0.0)

            # =================================================================
            # 3. UPDATE INDICATOR MATH
            # =================================================================
            for ind in indicators:
                ind.update(tick)
            
            obi_score = obi.get_score(sec_id)
            cvd_score = cvd.get_score(sec_id)
            
            # =================================================================
            # 4. POSITION MANAGEMENT (DYNAMIC ATR SL/TP BANDS)
            # =================================================================
            current_pos = portfolio.positions.get(sec_id)
            if current_pos:
                entry_price = current_pos['avg_price']
                pos_qty = current_pos['qty']
                side = current_pos['side']
                
                raw_atr = atr.get_score(sec_id)
                atr_val = raw_atr if raw_atr > 0 else 1.0
                
                # SL FIX: Floor must be the maximum of 0.25% OR the actual bid-ask spread
                natural_spread = tick.get('ask', 0.0) - tick.get('bid', 0.0)
                min_sl_distance = max(tick_ltp * 0.0025, natural_spread) 
                
                if side == 'BUY':
                    tp_price = entry_price + (atr_val * 3.0)
                    sl_distance = max(atr_val * 1.5, min_sl_distance)
                    sl_price = entry_price - sl_distance
                    
                    if tick_ltp >= tp_price or tick_ltp <= sl_price:
                        trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                        reason = "TAKE PROFIT" if tick_ltp >= tp_price else "STOP LOSS"
                        print(f"\n[{reason}] Long on #{sec_id}. Exiting. Trace: {trace_id}")
                        engine.execute_paper_trade(tick, action='SELL', qty=pos_qty, trace_id=trace_id)
                        continue
                        
                elif side == 'SELL':
                    tp_price = entry_price - (atr_val * 3.0)
                    sl_distance = max(atr_val * 1.5, min_sl_distance)
                    sl_price = entry_price + sl_distance
                    
                    if tick_ltp <= tp_price or tick_ltp >= sl_price:
                        trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                        reason = "TAKE PROFIT" if tick_ltp <= tp_price else "STOP LOSS"
                        print(f"\n[{reason}] Short on #{sec_id}. Exiting. Trace: {trace_id}")
                        engine.execute_paper_trade(tick, action='BUY', qty=pos_qty, trace_id=trace_id)
                        continue

            # =================================================================
            # 5. ENTRY STRATEGY LOGIC 
            # =================================================================
            
            if atr.get_score(sec_id) == 0.0:
                continue 

            current_vwap = vwap.get_score(sec_id)
            MIN_POSITION_VALUE = 5000.0  # Hardcoded tax survival barrier

            # MACRO FILTER: Only SELL if price is historically weak (Below VWAP)
            if obi_score < -0.4 and cvd_score > 1000 and tick_ltp < current_vwap:
                
                if current_pos and current_pos['side'] == 'BUY':
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-REVSQ-{uuid.uuid4().hex[:6]}"
                    print(f"\n[SIGNAL REVERSAL] Long on #{sec_id}, but SELL signal fired. Squaring off. Trace: {trace_id}")
                    engine.execute_paper_trade(tick, action='SELL', qty=current_pos['qty'], trace_id=trace_id)
                    continue 
                
                current_ts = datetime.now().timestamp()
                if current_ts - last_trade_time.get(sec_id, 0.0) < COOLDOWN_SECONDS:
                    continue
                
                confidence = risk_manager.calculate_confidence(obi_score, cvd_score, spread.get_score(sec_id))
                margin_used = sum(pos['qty'] * pos['avg_price'] for pos in portfolio.positions.values())
                free_cash = portfolio.current_balance - margin_used
                qty = risk_manager.calculate_position_size(confidence, free_cash, tick_ltp, tick.get('best_bid_vol', 0))
                
                # THE HARD GATE: Ensure position value beats the flat fee
                if qty > 0 and (qty * tick_ltp) >= MIN_POSITION_VALUE:
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    status = engine.execute_paper_trade(tick, action='SELL', qty=qty, trace_id=trace_id)
                    signal_logger.info(f"Trace: {trace_id} | Sec: {sec_id} | Action: SELL | Conf: {confidence:.2f} | Qty: {qty} | Status: {status}")
                    if status == "EXECUTED":
                        last_trade_time[sec_id] = current_ts
                
            # MACRO FILTER: Only BUY if price is historically strong (Above VWAP)
            elif obi_score > 0.4 and cvd_score < -1000 and tick_ltp > current_vwap:
                
                if current_pos and current_pos['side'] == 'SELL':
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-REVSQ-{uuid.uuid4().hex[:6]}"
                    print(f"\n[SIGNAL REVERSAL] Short on #{sec_id}, but BUY signal fired. Squaring off. Trace: {trace_id}")
                    engine.execute_paper_trade(tick, action='BUY', qty=current_pos['qty'], trace_id=trace_id)
                    continue

                current_ts = datetime.now().timestamp()
                if current_ts - last_trade_time.get(sec_id, 0.0) < COOLDOWN_SECONDS:
                    continue

                confidence = risk_manager.calculate_confidence(obi_score, cvd_score, spread.get_score(sec_id))
                margin_used = sum(pos['qty'] * pos['avg_price'] for pos in portfolio.positions.values())
                free_cash = portfolio.current_balance - margin_used
                qty = risk_manager.calculate_position_size(confidence, free_cash, tick_ltp, tick.get('best_ask_vol', 0))
                
                # THE HARD GATE: Ensure position value beats the flat fee
                if qty > 0 and (qty * tick_ltp) >= MIN_POSITION_VALUE:
                    trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
                    status = engine.execute_paper_trade(tick, action='BUY', qty=qty, trace_id=trace_id)
                    signal_logger.info(f"Trace: {trace_id} | Sec: {sec_id} | Action: BUY | Conf: {confidence:.2f} | Qty: {qty} | Status: {status}")
                    if status == "EXECUTED":
                        last_trade_time[sec_id] = current_ts

if __name__ == "__main__":
    main()
# """Strategy engine with risk management for live trading."""
# import sys
# import os
# import json
# import redis
# import uuid
# from dotenv import load_dotenv
# from datetime import datetime, time

# # Indicators
# from src.trading.indicators.spread import BidAskSpread
# from src.trading.indicators.obi import OrderBookImbalance
# from src.trading.indicators.cvd import CumulativeVolumeDelta
# from src.trading.indicators.wmp import WeightedMidPrice
# from src.trading.indicators.atr import TickATR

# # Sandbox Components
# from src.trading.portfolio import PortfolioManager
# from src.trading.audit_logger import AuditLogger
# from src.trading.execution_client import ExecutionEngine
# from src.scripts.aws_sync import AWSSync

# #risk components
# from src.trading.risk_manager import RiskManager

# #Log components
# from src.trading.audit_logger import setup_signal_logger
# from src.scripts.telegram_reporter import TelegramReporter


# current_dir = os.path.dirname(os.path.abspath(__file__))
# env_path = os.path.join(current_dir, "../../.env")
# load_dotenv(dotenv_path=env_path)

# def main():
#     r = redis.Redis(
#         host=os.getenv("REDIS_HOST", "localhost"),
#         port=int(os.getenv("REDIS_PORT", 6379)),
#         decode_responses=True
#     )
    
#     # Initialize Math Engine
#     spread = BidAskSpread()
#     obi = OrderBookImbalance()
#     cvd = CumulativeVolumeDelta()
#     wmp = WeightedMidPrice()
#     atr = TickATR(period=14, tick_chunk=50) # Added ATR
#     indicators = [spread, obi, cvd, wmp, atr] # Added to list
    
#     # Initialize Sandbox (Starting with ₹1,00,000 | 10% max allocation | 2% max daily loss)
#     portfolio = PortfolioManager(starting_balance=100000.0, max_allocation_pct=0.10, max_loss_pct=0.02)
#     logger = AuditLogger()
#     engine = ExecutionEngine(portfolio, logger)
    
#     pubsub = r.pubsub()
#     pubsub.subscribe("live_ticks")
#     risk_manager = RiskManager(base_risk_pct = 0.10)
#     # Initialize the Signal Logger
#     signal_logger = setup_signal_logger()
#     print("Signal Logger initialized. Silently logging to /logs directory...")
#     print("Execution System Live. Listening for ticks and searching for setups...")
    
#     # NEW ARCHITECTURE: Non-blocking event loop
#     while True:
#         # =================================================================
#         # 1. THE 3:14 PM HARD KILL-SWITCH (EVALUATED EVERY SECOND)
#         # =================================================================
#         current_time = datetime.now().time()
#         cutoff_time = time(18, 14, 0) # 15:14:00 (3:14 PM)
        
#         if current_time >= cutoff_time:
#             open_positions = list(portfolio.positions.items())
            
#             if open_positions:
#                 print("\n[SYSTEM ALERT] 3:14 PM Cutoff Reached. Initiating forced liquidation.")
#                 for open_sec_id, pos_data in open_positions:
#                     try:
#                         pos_qty = pos_data['qty']
#                         side = pos_data['side']
#                         exit_action = 'SELL' if side == 'BUY' else 'BUY'
#                         trace_id = f"{open_sec_id}-SQUAREOFF-{uuid.uuid4().hex[:6]}"
#                         print(f"[LIQUIDATION] Force closing {side} position on #{open_sec_id}. Trace: {trace_id}")
                        
#                         # Extract a safe fallback price
#                         price = pos_data.get('avg_price') or 1.0
                        
#                         # Fire a dummy tick with complete fallback keys for the execution client
#                         dummy_tick = {
#                             'security_id': open_sec_id, 
#                             'ltp': price,
#                             'price': price,
#                             'ask': price,
#                             'bid': price,
#                             'best_ask_vol': pos_qty,
#                             'best_bid_vol': pos_qty,
#                             'timestamp': datetime.now().isoformat()
#                         }
#                         engine.execute_paper_trade(dummy_tick, action=exit_action, qty=pos_qty, trace_id=trace_id)
#                     except Exception as e:
#                         print(f"[ERROR] Failed squaring off position {open_sec_id}. Reason: {e}")
            
#             portfolio.save_state()
#             print(f"[STATE SAVED] Final Portfolio Balance: ₹{portfolio.current_balance:.2f} written to disk.")
            
#             # THE CLOUD & TELEGRAM HANDOFF
#             print("\n[SYSTEM] Commencing Cloud Handoff to AWS S3...")
#             from src.scripts.aws_sync import AWSSync
#             cloud_sync = AWSSync()
#             cloud_sync.upload_daily_logs()

#             print("\n[SYSTEM] Generating End-of-Day Telegram Report and DB Backup...")
#             from src.scripts.telegram_reporter import TelegramReporter
#             reporter = TelegramReporter()
#             reporter.send_report()
#             reporter.send_file(reporter.csv_path)
#             log_path = os.path.join(reporter.base_dir, "logs", f"signals_{reporter.today_str}.log")
#             reporter.send_file(log_path)
            
#             db_dump = reporter.backup_postgres()
#             if db_dump:
#                 reporter.send_file(db_dump)

#             print("\n[SYSTEM TERMINATED] All intraday positions flat and data secured. Shutting down.")
#             break  # Break the while loop
        
#         # =================================================================
#         # 2. NON-BLOCKING REDIS FETCH (1 Second Timeout)
#         # =================================================================
#         message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        
#         if message and message['type'] == 'message':
#             tick = json.loads(message['data'])
#             # Cast sec_id to string immediately to align with the dictionary keys
#             sec_id = str(tick.get('security_id'))
#             tick_ltp = tick.get('ltp', 0.0)

#             # =================================================================
#             # 3. UPDATE INDICATOR MATH
#             # =================================================================
#             for ind in indicators:
#                 ind.update(tick)
            
#             # Request scores for this specific security ID
#             obi_score = obi.get_score(sec_id)
#             cvd_score = cvd.get_score(sec_id)
            
#             # =================================================================
#             # 4. POSITION MANAGEMENT (DYNAMIC ATR SL/TP BANDS)
#             # =================================================================
#             current_pos = portfolio.positions.get(sec_id)
#             if current_pos:
#                 entry_price = current_pos['avg_price']
#                 pos_qty = current_pos['qty']
#                 side = current_pos['side']
                
#                 # Fetch isolated ATR for SL/TP
#                 atr_val = atr.get_score(sec_id) if atr.get_score(sec_id) > 0 else 1.0
                
#                 if side == 'BUY':
#                     tp_price = entry_price + (atr_val * 3.0)
#                     sl_price = entry_price - (atr_val * 1.5)
                    
#                     if tick_ltp >= tp_price or tick_ltp <= sl_price:
#                         trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
#                         reason = "TAKE PROFIT" if tick_ltp >= tp_price else "STOP LOSS"
#                         print(f"\n[{reason}] Long on #{sec_id}. Exiting. Trace: {trace_id}")
#                         engine.execute_paper_trade(tick, action='SELL', qty=pos_qty, trace_id=trace_id)
#                         continue
                        
#                 elif side == 'SELL':
#                     tp_price = entry_price - (atr_val * 3.0)
#                     sl_price = entry_price + (atr_val * 1.5)
                    
#                     if tick_ltp <= tp_price or tick_ltp >= sl_price:
#                         trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
#                         reason = "TAKE PROFIT" if tick_ltp <= tp_price else "STOP LOSS"
#                         print(f"\n[{reason}] Short on #{sec_id}. Exiting. Trace: {trace_id}")
#                         engine.execute_paper_trade(tick, action='BUY', qty=pos_qty, trace_id=trace_id)
#                         continue

#             # =================================================================
#             # 5. ENTRY STRATEGY LOGIC 
#             # =================================================================
#             if obi_score < -0.4 and cvd_score > 1000:
#                 confidence = risk_manager.calculate_confidence(obi_score, cvd_score, spread.get_score(sec_id))
#                 margin_used = sum(pos['qty'] * pos['avg_price'] for pos in portfolio.positions.values())
#                 free_cash = portfolio.current_balance - margin_used
#                 qty = risk_manager.calculate_position_size(confidence, free_cash, tick_ltp, tick.get('best_bid_vol', 0))
                
#                 if qty > 0:
#                     trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
#                     status = engine.execute_paper_trade(tick, action='SELL', qty=qty, trace_id=trace_id)
#                     signal_logger.info(f"Trace: {trace_id} | Sec: {sec_id} | Action: SELL | Conf: {confidence:.2f} | Qty: {qty} | Status: {status}")
                
#             elif obi_score > 0.4 and cvd_score < -1000:
#                 confidence = risk_manager.calculate_confidence(obi_score, cvd_score, spread.get_score(sec_id))
#                 margin_used = sum(pos['qty'] * pos['avg_price'] for pos in portfolio.positions.values())
#                 free_cash = portfolio.current_balance - margin_used
#                 qty = risk_manager.calculate_position_size(confidence, free_cash, tick_ltp, tick.get('best_ask_vol', 0))
                
#                 if qty > 0:
#                     trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
#                     status = engine.execute_paper_trade(tick, action='BUY', qty=qty, trace_id=trace_id)
#                     signal_logger.info(f"Trace: {trace_id} | Sec: {sec_id} | Action: BUY | Conf: {confidence:.2f} | Qty: {qty} | Status: {status}")

# if __name__ == "__main__":
#     main()

    
# """Strategy engine with risk management for live trading."""
# import sys
# import os
# import json
# import redis
# import uuid
# from dotenv import load_dotenv
# from datetime import datetime, time

# # Indicators
# from src.trading.indicators.spread import BidAskSpread
# from src.trading.indicators.obi import OrderBookImbalance
# from src.trading.indicators.cvd import CumulativeVolumeDelta
# from src.trading.indicators.wmp import WeightedMidPrice
# from src.trading.indicators.atr import TickATR

# # Sandbox Components
# from src.trading.portfolio import PortfolioManager
# from src.trading.audit_logger import AuditLogger
# from src.trading.execution_client import ExecutionEngine
# from src.trading.execution_client import ExecutionEngine
# from src.scripts.aws_sync import AWSSync

# #risk components
# from src.trading.risk_manager import RiskManager

# #Log components
# from src.trading.audit_logger import setup_signal_logger
# from src.scripts.telegram_reporter import TelegramReporter
# from src.scripts.aws_sync import AWSSync


# current_dir = os.path.dirname(os.path.abspath(__file__))
# env_path = os.path.join(current_dir, "../../.env")
# load_dotenv(dotenv_path=env_path)

# def main():
#     r = redis.Redis(
#         host=os.getenv("REDIS_HOST", "localhost"),
#         port=int(os.getenv("REDIS_PORT", 6379)),
#         decode_responses=True
#     )
    
#     # Initialize Math Engine
#     spread = BidAskSpread()
#     obi = OrderBookImbalance()
#     cvd = CumulativeVolumeDelta()
#     wmp = WeightedMidPrice()
#     atr = TickATR(period=14, tick_chunk=50) # Added ATR
#     indicators = [spread, obi, cvd, wmp, atr] # Added to list
    
#     # Initialize Sandbox (Starting with ₹1,00,000 | 10% max allocation | 2% max daily loss)
#     portfolio = PortfolioManager(starting_balance=100000.0, max_allocation_pct=0.10, max_loss_pct=0.02)
#     logger = AuditLogger()
#     engine = ExecutionEngine(portfolio, logger)
    
#     pubsub = r.pubsub()
#     pubsub.subscribe("live_ticks")
#     risk_manager = RiskManager(base_risk_pct = 0.10)
#     # Initialize the Signal Logger
#     signal_logger = setup_signal_logger()
#     print("Signal Logger initialized. Silently logging to /logs directory...")
#     print("Execution System Live. Listening for ticks and searching for setups...")
    
#     # NEW ARCHITECTURE: Non-blocking event loop
#     while True:
#         # =================================================================
#         # 1. THE 3:14 PM HARD KILL-SWITCH (EVALUATED EVERY SECOND)
#         # =================================================================
#         current_time = datetime.now().time()
#         cutoff_time = time(15, 14, 0) # 15:14:00 (3:14 PM)
        
#         if current_time >= cutoff_time:
#             open_positions = list(portfolio.positions.items())
            
#             if open_positions:
#                 print("\n[SYSTEM ALERT] 3:14 PM Cutoff Reached. Initiating forced liquidation.")
#                 for open_sec_id, pos_data in open_positions:
#                     try:
#                         pos_qty = pos_data['qty']
#                         side = pos_data['side']
#                         exit_action = 'SELL' if side == 'BUY' else 'BUY'
#                         trace_id = f"{open_sec_id}-SQUAREOFF-{uuid.uuid4().hex[:6]}"
#                         print(f"[LIQUIDATION] Force closing {side} position on #{open_sec_id}. Trace: {trace_id}")
                        
#                         # Extract a safe fallback price
#                         price = pos_data.get('avg_price') or 1.0
                        
#                         # Fire a dummy tick with complete fallback keys for the execution client
#                         dummy_tick = {
#                             'security_id': open_sec_id, 
#                             'ltp': price,
#                             'price': price,
#                             'ask': price,
#                             'bid': price,
#                             'best_ask_vol': pos_qty,
#                             'best_bid_vol': pos_qty,
#                             'timestamp': datetime.now().isoformat()
#                         }
#                         engine.execute_paper_trade(dummy_tick, action=exit_action, qty=pos_qty, trace_id=trace_id)
#                     except Exception as e:
#                         print(f"[ERROR] Failed squaring off position {open_sec_id}. Reason: {e}")
            
#             portfolio.save_state()
#             print(f"[STATE SAVED] Final Portfolio Balance: ₹{portfolio.current_balance:.2f} written to disk.")
            
#             # THE CLOUD & TELEGRAM HANDOFF
#             print("\n[SYSTEM] Commencing Cloud Handoff to AWS S3...")
#             from src.scripts.aws_sync import AWSSync
#             cloud_sync = AWSSync()
#             cloud_sync.upload_daily_logs()

#             print("\n[SYSTEM] Generating End-of-Day Telegram Report and DB Backup...")
#             from src.scripts.telegram_reporter import TelegramReporter
#             reporter = TelegramReporter()
#             reporter.send_report()
#             reporter.send_file(reporter.csv_path)
#             log_path = os.path.join(reporter.base_dir, "logs", f"signals_{reporter.today_str}.log")
#             reporter.send_file(log_path)
            
#             db_dump = reporter.backup_postgres()
#             if db_dump:
#                 reporter.send_file(db_dump)

#             print("\n[SYSTEM TERMINATED] All intraday positions flat and data secured. Shutting down.")
#             break  # Break the while loop
        
#         # =================================================================
#         # 2. NON-BLOCKING REDIS FETCH (1 Second Timeout)
#         # =================================================================
#         message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        
#         if message and message['type'] == 'message':
#             tick = json.loads(message['data'])
#             sec_id = tick.get('security_id')
#             tick_ltp = tick.get('ltp', 0.0)

#             # =================================================================
#             # 3. UPDATE INDICATOR MATH
#             # =================================================================
#             for ind in indicators:
#                 ind.update(tick)
            
#             obi_score = obi.get_score()
#             cvd_score = cvd.get_score()
            
#             # =================================================================
#             # 4. POSITION MANAGEMENT (DYNAMIC ATR SL/TP BANDS)
#             # =================================================================
#             current_pos = portfolio.positions.get(sec_id)
#             if current_pos:
#                 entry_price = current_pos['avg_price']
#                 pos_qty = current_pos['qty']
#                 side = current_pos['side']
                
#                 atr_val = atr.get_score() if atr.get_score() > 0 else 1.0
                
#                 if side == 'BUY':
#                     tp_price = entry_price + (atr_val * 3.0)
#                     sl_price = entry_price - (atr_val * 1.5)
                    
#                     if tick_ltp >= tp_price or tick_ltp <= sl_price:
#                         trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
#                         reason = "TAKE PROFIT" if tick_ltp >= tp_price else "STOP LOSS"
#                         print(f"\n[{reason}] Long on #{sec_id}. Exiting. Trace: {trace_id}")
#                         engine.execute_paper_trade(tick, action='SELL', qty=pos_qty, trace_id=trace_id)
#                         continue
                        
#                 elif side == 'SELL':
#                     tp_price = entry_price - (atr_val * 3.0)
#                     sl_price = entry_price + (atr_val * 1.5)
                    
#                     if tick_ltp <= tp_price or tick_ltp >= sl_price:
#                         trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
#                         reason = "TAKE PROFIT" if tick_ltp <= tp_price else "STOP LOSS"
#                         print(f"\n[{reason}] Short on #{sec_id}. Exiting. Trace: {trace_id}")
#                         engine.execute_paper_trade(tick, action='BUY', qty=pos_qty, trace_id=trace_id)
#                         continue

#             # =================================================================
#             # 5. ENTRY STRATEGY LOGIC 
#             # =================================================================
#             if obi_score < -0.4 and cvd_score > 1000:
#                 confidence = risk_manager.calculate_confidence(obi_score, cvd_score, spread.get_score())
#                 margin_used = sum(pos['qty'] * pos['avg_price'] for pos in portfolio.positions.values())
#                 free_cash = portfolio.current_balance - margin_used
#                 qty = risk_manager.calculate_position_size(confidence, free_cash, tick_ltp, tick.get('best_bid_vol', 0))
                
#                 if qty > 0:
#                     trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
#                     status = engine.execute_paper_trade(tick, action='SELL', qty=qty, trace_id=trace_id)
#                     signal_logger.info(f"Trace: {trace_id} | Sec: {sec_id} | Action: SELL | Conf: {confidence:.2f} | Qty: {qty} | Status: {status}")
                
#             elif obi_score > 0.4 and cvd_score < -1000:
#                 confidence = risk_manager.calculate_confidence(obi_score, cvd_score, spread.get_score())
#                 margin_used = sum(pos['qty'] * pos['avg_price'] for pos in portfolio.positions.values())
#                 free_cash = portfolio.current_balance - margin_used
#                 qty = risk_manager.calculate_position_size(confidence, free_cash, tick_ltp, tick.get('best_ask_vol', 0))
                
#                 if qty > 0:
#                     trace_id = f"{sec_id}-{tick.get('timestamp')}-{uuid.uuid4().hex[:6]}"
#                     status = engine.execute_paper_trade(tick, action='BUY', qty=qty, trace_id=trace_id)
#                     signal_logger.info(f"Trace: {trace_id} | Sec: {sec_id} | Action: BUY | Conf: {confidence:.2f} | Qty: {qty} | Status: {status}")
# if __name__ == "__main__":
#     main()





